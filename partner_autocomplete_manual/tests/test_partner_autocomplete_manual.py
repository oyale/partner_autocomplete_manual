# Copyright 2025 OCA Contributors
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64
import io
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from lxml import html
from odoo.addons.partner_autocomplete_manual.models import res_partner
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from PIL import Image


def make_png(size):
    """Real PNG of the given size: squares for logos, wide ones for banners."""
    buffer = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


# Big enough for LOGO_MIN_SIDE: a 1x1 is rejected as "too small" on purpose
PNG_ICON = make_png((192, 192))

MOCK_HOME_HTML = b"""<!DOCTYPE html>
<html lang="es">
<head>
    <title>Acme Industrial | Soluciones Tecnologicas</title>
    <meta property="og:site_name" content="Acme Industrial" />
    <link rel="icon" href="/favicon.ico" />
    <link rel="apple-touch-icon" href="/touch.png" />
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Acme Industrial",
        "telephone": "+34 912 345 678",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "Calle de Alcal\\u00e1 100",
            "postalCode": "28009",
            "addressLocality": "Madrid",
            "addressCountry": "ES"
        }
    }
    </script>
</head>
<body>
    <footer>
        <a href="/aviso-legal">Aviso Legal</a>
    </footer>
</body>
</html>
"""

MOCK_LEGAL_HTML = b"""<!DOCTYPE html>
<html lang="es">
<body>
    <h1>Aviso Legal</h1>
    <p>Titular: ACME INDUSTRIAL S.L. con NIF B12345674 inscrita en el Registro
    Mercantil de Madrid.</p>
</body>
</html>
"""


class TestPartnerDomainAutocomplete(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Partner = self.env["res.partner"]
        # No network in tests: acme.es does not resolve inside the container and
        # _public_ip_from_hostname would return None. The SSRF test uses the
        # original resolver (self.real_public_ip).
        self.real_public_ip = res_partner._public_ip_from_hostname
        fake_addr = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", None))
        patcher = patch.object(
            res_partner, "_public_ip_from_hostname", return_value=[fake_addr]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _mock_response(url, html_body=MOCK_HOME_HTML, binary=None, content_type=None):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.url = url
        if binary is not None:
            content = binary
            resp.headers = {"Content-Type": content_type or "image/png"}
        else:
            content = html_body
            resp.headers = {"Content-Type": content_type or "text/html; charset=utf-8"}
        resp.content = content
        resp.iter_content = lambda chunk_size=8192: (
            content[i : i + chunk_size] for i in range(0, len(content), chunk_size)
        )
        resp.close = MagicMock()
        return resp

    def test_01_extract_clean_domain_from_website(self):
        """The domain comes only from the website, cleaned of scheme/path/www."""
        partner = self.Partner.create(
            {"name": "x", "website": "https://www.acme-industrial.es/contacto"}
        )
        self.assertEqual(partner._extract_clean_domain(), "acme-industrial.es")
        self.assertFalse(self.Partner.create({"name": "y"})._extract_clean_domain())
        # an email alone no longer yields a domain
        self.assertFalse(
            self.Partner.create(
                {"name": "z", "email": "info@acme-industrial.es"}
            )._extract_clean_domain()
        )

    def test_02c_private_and_loopback_urls_rejected(self):
        """The logo_url comes from an untrusted page: no internal hosts."""
        # scheme-level gate (real _is_public_url, no DNS needed for these)
        for url in ("file:///etc/passwd", "ftp://acme.es/x.png", ""):
            with self.subTest(url=url):
                self.assertFalse(
                    res_partner.ResPartner._is_public_url(self.Partner, url), url
                )
        # IP-level gate: numeric/loopback/private hosts resolve without DNS, so
        # the real resolver is used directly here
        for host in ("127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "::1"):
            with self.subTest(host=host):
                self.assertIsNone(self.real_public_ip(host), host)

    def test_02d_legal_page_must_be_same_host(self):
        """The substring comparison used to accept acme.com.evil.net."""
        tree = html.fromstring(
            '<a href="https://acme.com.evil.net/aviso-legal">aviso legal</a>'
            '<a href="/aviso/legal">aviso legal</a>'
        )
        url = self.Partner._find_legal_page_url(tree, "https://www.acme.com/")
        self.assertEqual(url, "https://www.acme.com/aviso/legal")

    @patch("odoo.addons.partner_autocomplete_manual.models.res_partner.requests.get")
    def test_02e_redirect_to_private_host_not_followed(self, mock_get):
        """A public page 302'ing to internal metadata must not be fetched."""
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", None))]
        redirect = MagicMock()
        redirect.status_code = 302
        redirect.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
        redirect.url = "https://acme.es/"
        redirect.close = MagicMock()
        mock_get.return_value = redirect

        def resolve(host):
            return fake if host == "acme.es" else None

        with patch.object(res_partner, "_public_ip_from_hostname", side_effect=resolve):
            tree, _final = self.Partner._fetch_html_tree("https://acme.es/")

        self.assertIsNone(tree)
        self.assertEqual(mock_get.call_count, 1)  # the redirect was never fetched

    @patch("odoo.addons.partner_autocomplete_manual.models.res_partner.requests.get")
    def test_03_enrich_partner_from_json_ld_and_legal_page(self, mock_get):
        """JSON-LD, Legal Notice navigation, VAT and province mapping."""

        def side_effect(url, *args, **kwargs):
            if "aviso-legal" in url:
                return self._mock_response(url, MOCK_LEGAL_HTML)
            if url.endswith("/touch.png"):
                return self._mock_response(url, binary=PNG_ICON)
            return self._mock_response(url)

        mock_get.side_effect = side_effect

        partner = self.Partner.create(
            {
                "name": "acme-industrial.es",
                "website": "https://acme-industrial.es",
                "email": "info@acme-industrial.es",
                "street": "Address already set",
            }
        )
        partner.action_enrich_from_domain()

        self.assertEqual(partner.name, "Acme Industrial")
        self.assertEqual(partner.vat, "ESB12345674")
        self.assertEqual(partner.phone, "+34 912 345 678")
        # existing data is never overwritten
        self.assertEqual(partner.street, "Address already set")
        self.assertEqual(partner.zip, "28009")
        self.assertEqual(partner.city, "Madrid")
        self.assertEqual(partner.country_id.code, "ES")
        self.assertTrue(partner.image_1920)

    def test_04_user_error_when_no_valid_domain(self):
        """Button on a contact without a website raises a clear error."""
        partner = self.Partner.create(
            {"name": "Test Free", "email": "usuario@gmail.com"}
        )
        with self.assertRaises(UserError):
            partner.action_enrich_from_domain()

    def test_04b_single_contact_is_enriched_synchronously(self):
        """One contact is enriched at once (no wait), and marked done."""
        with patch.object(
            type(self.Partner),
            "_enrich_partner_from_web",
            return_value=True,
        ) as enrich:
            partner = self.Partner.create(
                {"name": "acme.es", "website": "https://acme.es"}
            )
            partner.action_enrich_from_domain()

        self.assertEqual(enrich.call_count, 1)
        self.assertEqual(partner.enrich_status, "done")
        self.assertFalse(partner.enrich_error)

    def test_04c_bulk_is_queued_and_drained_by_cron(self):
        """N contacts are only flagged; the cron enriches them and clears the queue."""
        partners = self.Partner.create(
            [
                {"name": "a", "website": "https://a.es"},
                {"name": "b", "website": "https://b.es"},
            ]
        )
        with patch.object(type(self.Partner), "_enrich_partner_from_web") as enrich:
            self.Partner.browse(partners.ids).action_enrich_from_domain()
            # nothing is fetched in the request
            self.assertFalse(enrich.called)
            self.assertEqual(set(partners.mapped("enrich_status")), {"pending"})

            self.Partner._cron_enrich_pending(limit=20)

        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(set(partners.mapped("enrich_status")), {"done"})

    def test_04d_cron_marks_failures_and_keeps_going(self):
        """A failing contact is marked error; the rest of the batch still runs."""
        partners = self.Partner.create(
            [
                {"name": "ok", "website": "https://ok.es"},
                {"name": "bad", "website": "https://bad.es"},
            ]
        )
        partners.write({"enrich_status": "pending"})

        def fail_on_bad(partner, domain):
            if domain == "bad.es":
                raise RuntimeError("boom")

        with (
            patch.object(type(self.Partner), "_enrich_partner_from_web", fail_on_bad),
            patch.object(res_partner.time, "sleep"),
            # the cron logs the failure; keep it out of the CI checklog scan
            patch.object(res_partner, "_logger"),
        ):
            self.Partner._cron_enrich_pending(limit=20)

        self.assertEqual(partners[0].enrich_status, "done")
        self.assertEqual(partners[1].enrich_status, "error")
        self.assertIn("boom", partners[1].enrich_error)

    def test_04e_cron_throttles_repeated_hosts(self):
        """The cron sleeps between contacts and before re-hitting a host."""
        partners = self.Partner.create(
            [
                {"name": "a", "website": "https://same.es"},
                {"name": "b", "website": "https://same.es"},
            ]
        )
        partners.write({"enrich_status": "pending"})
        res_partner._LAST_HIT_PER_HOST.clear()

        with (
            patch.object(type(self.Partner), "_enrich_partner_from_web"),
            patch.object(res_partner.time, "sleep") as sleep,
            patch.dict(res_partner._LAST_HIT_PER_HOST, {}, clear=True),
        ):
            self.Partner._cron_enrich_pending(limit=20)

        # 1 pause between partners + 1 per-host wait before the second hit
        self.assertEqual(sleep.call_count, 2)
        first, second = (c.args[0] for c in sleep.call_args_list)
        self.assertAlmostEqual(first, res_partner.THROTTLE_BETWEEN_PARTNERS)
        # the per-host wait is the remaining slice, so slightly less than the max
        self.assertGreater(second, 0)
        self.assertLessEqual(second, res_partner.THROTTLE_PER_DOMAIN)

    def test_04f_retry_after_only_honoured_in_the_cron(self):
        """429 Retry-After sleeps only with async_enrich; the user path never waits."""
        throttled = MagicMock()
        throttled.status_code = 429
        throttled.headers = {"Retry-After": "5"}
        throttled.close = MagicMock()

        with (
            patch.object(type(self.Partner), "_pinned_get", return_value=throttled),
            patch.object(res_partner.time, "sleep") as sleep,
        ):
            self.Partner._safe_get("https://acme.es", 4, {})
            self.assertFalse(sleep.called)

            self.Partner.with_context(async_enrich=True)._safe_get(
                "https://acme.es", 4, {}
            )
            sleep.assert_called_once_with(5)

    def test_05_vat_country_follows_domain_language(self):
        """A VAT without prefix is Spanish on .cat/.gal/.eus and Portuguese on .pt."""
        es_urls = (
            "https://www.acme.cat/inici",
            "https://acme.gal",
            "https://acme.eus",
            "https://acme.es",
        )
        for url in es_urls:
            with self.subTest(url=url):
                self.assertEqual(
                    self.Partner._guess_vat_country("B12345674", (url,)), "ES"
                )
        self.assertEqual(
            self.Partner._guess_vat_country("501964843", ("https://acme.pt",)), "PT"
        )
        # an explicit prefix beats the domain
        self.assertEqual(
            self.Partner._guess_vat_country("PT501964843", ("https://acme.cat",)), "PT"
        )
        self.assertEqual(
            self.Partner._normalize_and_validate_vat("B12345674", "ES"),
            "ESB12345674",
        )
        self.assertFalse(self.Partner._normalize_and_validate_vat("B12345670", "ES"))

    @patch("odoo.addons.partner_autocomplete_manual.models.res_partner.requests.get")
    def test_06_broken_logo_does_not_abort_enrich(self, mock_get):
        """A logo that is not an image is dropped; the rest of the data is saved."""

        def side_effect(url, *args, **kwargs):
            if url.endswith((".png", ".ico")):
                return self._mock_response(url, binary=b"not really a png")
            return self._mock_response(url)

        mock_get.side_effect = side_effect
        partner = self.Partner.create(
            {
                "name": "acme-industrial.es",
                "website": "https://acme.es",
                "email": "info@acme.es",
            }
        )
        partner.action_enrich_from_domain()

        self.assertFalse(partner.image_1920)
        self.assertEqual(partner.city, "Madrid")

    @patch("odoo.addons.partner_autocomplete_manual.models.res_partner.requests.get")
    def test_07_favicon_from_page_head_no_third_party(self, mock_get):
        """The logo comes from the homepage <head> (apple-touch-icon), no 3rd party."""
        requested = []

        def side_effect(url, *args, **kwargs):
            requested.append(url)
            if url.endswith("/touch.png"):
                return self._mock_response(url, binary=PNG_ICON)
            return self._mock_response(url)

        mock_get.side_effect = side_effect
        partner = self.Partner.create(
            {
                "name": "acme-industrial.es",
                "website": "https://acme.es",
                "email": "info@acme.es",
            }
        )
        partner.action_enrich_from_domain()

        self.assertTrue(partner.image_1920)
        self.assertIn("https://acme.es/touch.png", requested)
        foreign = [url for url in requested if urlparse(url).hostname != "acme.es"]
        self.assertFalse(foreign, foreign)

    def test_07b_ico_favicon_converted_to_png(self):
        """image_1920 does not decode .ico: the favicon is converted to PNG."""
        buffer = io.BytesIO()
        Image.new("RGBA", (192, 192), (255, 0, 0, 255)).save(buffer, format="ICO")
        with patch.object(res_partner.requests, "get") as mock_get:
            mock_get.return_value = self._mock_response(
                "https://acme.es/favicon.ico",
                binary=buffer.getvalue(),
                content_type="image/x-icon",
            )
            logo = self.Partner._fetch_logo("https://acme.es/favicon.ico")
        self.assertTrue(logo)
        png = base64.b64decode(logo)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        # and Odoo accepts it as image_1920
        partner = self.Partner.create({"name": "ico", "image_1920": logo})
        self.assertTrue(partner.image_1920)

    @staticmethod
    def _image_bytes(size, fmt="PNG"):
        buffer = io.BytesIO()
        Image.new("RGBA", size, (0, 0, 0, 255)).save(buffer, format=fmt)
        return buffer.getvalue()

    def _fetch_with(
        self, content, content_type="image/png", url="https://acme.es/i.png"
    ):
        with patch.object(res_partner.requests, "get") as mock_get:
            mock_get.return_value = self._mock_response(
                url, binary=content, content_type=content_type
            )
            return self.Partner._fetch_logo(url)

    def test_07e_logo_quality_filter(self):
        """Tiny icons and banners are rejected: image_1920 is square, both look bad.

        Real cases: calidoscoop.coop publishes a 16x16 favicon, la-merienda.org
        puts a 984x520 banner in JSON-LD "logo".
        """
        too_small = ((16, 16), (32, 32), (192, 52))
        for size in too_small:
            with self.subTest(size=size):
                self.assertFalse(self._fetch_with(make_png(size)), size)
        # LOGO_MAX_RATIO is 3: only clearly elongated images count as banners
        banners = ((992, 196), (1000, 260), (1500, 300))
        for size in banners:
            with self.subTest(size=size):
                self.assertFalse(self._fetch_with(make_png(size)), size)
        # a square logo and its 192x192 WordPress variant are fine
        for size in ((192, 192), (180, 180), (64, 64)):
            with self.subTest(size=size):
                self.assertTrue(self._fetch_with(make_png(size)), size)

    def test_07f_favicon_prefers_largest_declared_size(self):
        """WordPress declares the same icon at -32x32 and -192x192: use the big one."""
        tree = html.fromstring(
            "<head>"
            '<link rel="icon" href="/i-32x32.png">'
            '<link rel="icon" href="/i-192x192.png">'
            '<link rel="apple-touch-icon" href="/touch-180x180.png">'
            "</head>"
        )
        urls = self.Partner._icon_candidates(tree, "https://acme.es/")
        self.assertEqual(urls[0], "https://acme.es/touch-180x180.png")
        self.assertEqual(urls[1], "https://acme.es/i-192x192.png")
        self.assertEqual(urls[2], "https://acme.es/i-32x32.png")

    def test_07g_bad_logo_falls_through_to_next_candidate(self):
        """A JSON-LD banner is rejected, so the favicon chain still gets its turn."""
        tree = html.fromstring(
            '<head><link rel="apple-touch-icon" href="/touch.png"></head>'
        )
        calls = []

        def fake_fetch(self, url, *_args, **_kw):
            calls.append(url)
            if url.endswith("touch.png"):
                return base64.b64encode(make_png((192, 192)))
            return False  # the JSON-LD banner, rejected by the quality filter

        with patch.object(res_partner.ResPartner, "_fetch_logo", fake_fetch):
            logo = self.Partner._fetch_logo("https://acme.es/banner.png") or (
                self.Partner._fetch_favicon(tree, "https://acme.es/")
            )
        self.assertTrue(logo)
        self.assertIn("https://acme.es/touch.png", calls)

    def test_07c_spanish_translation_loaded(self):
        """i18n/es.po must actually reach the code-translation catalog.

        Odoo 18 only loads entries carrying "module:", "odoo-python" and a
        "code:" occurrence; getting any of the three wrong silently falls back
        to English. This test catches that.
        """
        from odoo.tools import translate

        translated = translate.get_translation(
            "partner_autocomplete_manual",
            "es_ES",
            "Enriched %s of %s contacts.",
            (3, 5),
        )
        self.assertEqual(translated, "Se han enriquecido 3 de 5 contactos.")
        self.assertEqual(
            translate.get_translation(
                "partner_autocomplete_manual", "es_ES", "Web Autocomplete", None
            ),
            "Autocompletado Web",
        )
        # English and unsupported languages keep the source string
        self.assertEqual(
            translate.get_translation(
                "partner_autocomplete_manual", "en_US", "Web Autocomplete", None
            ),
            "Web Autocomplete",
        )

    def test_07h_view_and_action_strings_in_po(self):
        """UI labels are translatable: they must be in i18n/es.po with a
        model_terms: reference, or the button stays English for Spanish users."""
        po = (Path(res_partner.__file__).parent.parent / "i18n" / "es.po").read_text(
            encoding="utf-8"
        )
        for src in (
            "Autocomplete",
            "from Web",
            "Autocomplete data from Web (No IAP)",
        ):
            self.assertIn(f'msgid "{src}"', po)
        self.assertIn(
            "model_terms:ir.ui.view,arch_db:partner_autocomplete_manual.view_partner_form_domain_autocomplete",
            po,
        )
        self.assertIn(
            "model:ir.actions.server,name:partner_autocomplete_manual.action_server_partner_enrich_from_domain",
            po,
        )

    def test_07d_encoded_tel_link_is_normalised(self):
        """Some sites percent-encode tel: hrefs (colectic.coop does):
        "tel:%28+34%29934425867" must come out as +34934425867, not raw."""
        tree = html.fromstring('<a href="tel:%28+34%29934425867">Truca</a>')
        info = self.Partner._extract_from_unstructured_text(tree)
        self.assertEqual(info["phone"], "+34934425867")

    def test_08_no_legal_name_guessing(self):
        """The free-text company name was dropped: "… S.L." invented names."""
        tree = html.fromstring(
            "<body><p>ACME INDUSTRIAL S.L. Tel 912 345 678</p>"
            '<a href="tel:+34912345678">llamanos</a></body>'
        )
        info = self.Partner._extract_from_unstructured_text(tree)
        self.assertNotIn("legal_name", info)
        self.assertEqual(info["phone"], "+34912345678")

    def test_09_spanish_phone_in_free_text(self):
        """Usual groupings and separators; never a DNI/NIF/IBAN/price."""
        cases = {
            # same separator in every gap
            "Llama al 912 345 678 para mas info": "+34912345678",
            "Movil: 612-345-678.": "+34612345678",
            "Fijo: 955 12 34 56": "+34955123456",
            "Fijo: 91 234 56 78": "+34912345678",
            "Fijo: 91-234-56-78": "+34912345678",
            "Movil: 6 12 34 56 78": "+34612345678",
            "Movil: 6 12 345 678": "+34612345678",
            # mixed separators: sites do it all the time
            "Fijo: 955 12-34-56": "+34955123456",
            # no separators, in groups of 3
            "Tel 912345678": "+34912345678",
            "Tel. 912345678.": "+34912345678",
            # international prefix
            "Tel. +34 699 88 77 66": "+34699887766",
            "Tel. +34699887766": "+34699887766",
            "Tel. +34-699-88-77-66": "+34699887766",
            "Tel +34 699887766": "+34699887766",
            "Contacto 0034 955 12 34 56": "+34955123456",
            "0034 955 12 34 56": "+34955123456",
            # not phones
            "NIF B12345674": False,
            "DNI 12345678Z": False,
            "DNI 91234567Z": False,
            "Ref 912 34 56 7 pedido": False,
            "Precio 612 345 6789 euros": False,
            "IBAN ES91 2100 0418 4502 0005 1332": False,
            "Precio 800 000 euros desde 2019": False,
            "Año 2019, piso 8, puerta 9": False,
            "Pedido 9123456789 no vale": False,
            "Codigo 61234567A": False,
            "ISBN 978-84-376-0494-7": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.Partner._normalize_phone(text), expected)

    def test_10_spanish_address_in_free_text(self):
        """Only an address with a postcode is accepted: no prices or dates."""
        cases = {
            "Visitanos en Calle Mayor 12, 28013 Madrid.": {
                "street": "Calle Mayor 12",
                "zip": "28013",
                "city": "Madrid",
            },
            "Avda. Diagonal 405 bis, 08037 Barcelona": {
                "street": "Avda. Diagonal 405 bis",
                "zip": "08037",
                "city": "Barcelona",
            },
            # Catalan / Valencian / Galician / Basque
            "Carrer de Mallorca 401, 08013 Barcelona": {
                "street": "Carrer de Mallorca 401",
                "zip": "08013",
                "city": "Barcelona",
            },
            "Avinguda Diagonal 405, 08037 Barcelona": {
                "street": "Avinguda Diagonal 405",
                "zip": "08037",
                "city": "Barcelona",
            },
            "Plaça de Catalunya 5, 08002 Barcelona": {
                "street": "Plaça de Catalunya 5",
                "zip": "08002",
                "city": "Barcelona",
            },
            "Passeig de Gràcia 3, 08007 Barcelona": {
                "street": "Passeig de Gràcia 3",
                "zip": "08007",
                "city": "Barcelona",
            },
            "Rúa do Franco 10, 15702 Santiago": {
                "street": "Rúa do Franco 10",
                "zip": "15702",
                "city": "Santiago",
            },
            "Kalea Nagusia 7, 48001 Bilbo": {
                "street": "Kalea Nagusia 7",
                "zip": "48001",
                "city": "Bilbo",
            },
            "Oferta: 1.200 unidades a 28013 euros": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.Partner._extract_address(text) or False, expected)
