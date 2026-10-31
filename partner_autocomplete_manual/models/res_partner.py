# Copyright 2025 OCA Contributors
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import base64
import io
import ipaddress
import json
import logging
import re
import socket
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from lxml import html
from odoo import _, fields, models
from odoo.exceptions import UserError
from PIL import Image

_logger = logging.getLogger(__name__)

# Bulk enrichment must not hammer other servers: a pause between contacts and a
# longer one before hitting the same host again. Only the cron throttles.
THROTTLE_BETWEEN_PARTNERS = 1.0
THROTTLE_PER_DOMAIN = 2.0
MAX_RETRY_AFTER = 30
# Last hit per host, in-process. Reset on restart, so it only softens bursts;
# the Retry-After retry is the real safety net.
_LAST_HIT_PER_HOST = {}

# Spanish phone: 9 digits starting with 6/7 (mobile) or 8/9 (landline). The regex
# only delimits the CANDIDATE; the grouping shape is checked in _normalize_phone,
# because there are too many variants to encode in a regex.
SPANISH_PHONE_REGEX = re.compile(
    # The lookbehind includes "+" and word characters so the match never starts
    # in the middle of a DNI, an IBAN or a longer number.
    r"(?<![\w+.,-])(?P<number>(?:\+34|0034)?[\s.+-]?[6789](?:[\d\s.+-]{0,3}\d)+)"
    r"(?![\w,-])"
)

# Usual groupings of a Spanish phone: 3-3-3, 3-2-2-2, 2-3-2-2, 2-2-2-3, the mobile
# forms 1-2-2-2-2 and 1-2-3-3, and the same with 12 digits once the prefix is
# stripped. Any other split ("2 2 5") is dropped: without this, any 9 digits on a
# page would match.
PHONE_GROUPINGS = (
    (3, 3, 3),
    (3, 2, 2, 2),
    (2, 3, 2, 2),
    (2, 2, 2, 3),
    (1, 2, 2, 2, 2),
    (1, 2, 3, 3),
    (3, 6),
    (2, 7),
    (9,),
)

# Spanish address: street type + number + postcode + town.
# The 5 digit postcode is required so a price or a date cannot match.
# Street types in Spanish, Catalan/Valencian, Galician and Basque.
SPANISH_ADDRESS_REGEX = re.compile(
    r"(?P<street>\b(?:"
    # Spanish
    r"C/|Calle|Cl\.|Avenida|Avda\.?|Av\.|Plaza|Pza\.|Paseo|P\.º|Pº|Camino|"
    r"Ctra\.?|Carretera|Ronda|Travesía|Pasaje|Glorieta|Urbanización|Polígono|"
    r"Barrio|"
    # Catalan / Valencian
    r"Carrer|Avinguda|Plaça|Pl\.|Passeig|Pg\.|Travessera|"
    # Galician
    r"Rúa|Rua|Praza|Estrada|"
    # Basque
    r"Kalea|Kale|Etorbidea|Pasealekua|Bidea"
    r")\s+[^,;]{1,60}?\d{1,4}[A-Za-z]?(?:\s+(?:bis|Bis|BIS))?)"
    r"\s*[,;]\s*"
    r"(?P<zip>\d{5})\s+(?P<city>[A-ZÁÉÍÓÚÑ][^,;\d]{2,40})"
)

CIF_REGEX = re.compile(r"[ABCDEFGHJKLMNPQRSUVW][\s-]?\d{7}[\s-]?[0-9A-J]")
DNI_REGEX = re.compile(r"\d{8}[\s-]?[TRWAGMYFPDXBNJZSQVHLCKE]")
NIE_REGEX = re.compile(r"[XYZ][\s-]?\d{7}[\s-]?[TRWAGMYFPDXBNJZSQVHLCKE]")
SPANISH_VAT_REGEX = re.compile(
    rf"\b(?:ES[-\s]?)?({CIF_REGEX.pattern}|{DNI_REGEX.pattern}|{NIE_REGEX.pattern})\b",
    re.IGNORECASE,
)

# Legal-page link keywords across the supported languages (es, ca/va, gl, eu, en).
LEGAL_KEYWORDS = (
    "aviso-legal",
    "aviso_legal",
    "avisolegal",
    "avis-legal",
    "avis_legal",
    "legal",
    "privacidad",
    "privacitat",
    "privacidade",
    "pribatutasun",
    "privacy",
    "condiciones",
    "condicions",
    "condicións",
    "baldintzak",
    "terms",
    "impressum",
    "contacto",
    "contacte",
    "kontaktua",
    "contact",
)

# A VAT without prefix is validated as Spanish when the domain is from l10n_es
# (this includes .cat/.gal/.eus/.va: Catalan, Galician, Basque or Valencian sites).
ES_TLDS = ("es", "cat", "gal", "eus", "va")
OTHER_COUNTRY_TLDS = {
    "pt": "PT",
    "fr": "FR",
    "de": "DE",
    "it": "IT",
    "nl": "NL",
    "be": "BE",
    "ie": "IE",
    "lu": "LU",
    "at": "AT",
    "pl": "PL",
    "se": "SE",
    "fi": "FI",
    "dk": "DK",
    "cz": "CZ",
    "ro": "RO",
    "gr": "GR",
    "hu": "HU",
    "bg": "BG",
    "hr": "HR",
    "si": "SI",
    "sk": "SK",
    "lt": "LT",
    "lv": "LV",
    "ee": "EE",
    "mt": "MT",
    "cy": "CY",
    "br": "BR",
    "mx": "MX",
    "ar": "AR",
    "cl": "CL",
    "co": "CO",
    "pe": "PE",
    "us": "US",
    "uk": "GB",
    "ch": "CH",
    "ma": "MA",
}
NIF_COUNTRY_PREFIX = re.compile(r"([A-Z]{2})(?=[A-Z0-9])")

# A 16x16 favicon looks like a scratch on a contact card: better no logo.
LOGO_MIN_SIDE = 64
# Wider than 3:1 (or taller) is a banner, not a logo: image_1920 is square.
LOGO_MAX_RATIO = 3
# WordPress publishes each icon at several sizes ("...-32x32.png",
# "...-192x192.png"): the URL says which one is the big one.
ICON_SIZE_IN_NAME = re.compile(r"[-._](\d{2,4})x(\d{2,4})\.")

# The logo fetch reuses the same headers: some CDNs reject a request without
# a User-Agent.
FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) OdooPartnerAutocomplete/18.0",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}


def _public_ip_from_hostname(hostname):
    """Return every resolved SOCK_STREAM address, or None when any is non-global.

    None when the hostname does not resolve or any address is non-global
    (loopback, private, link-local, multicast). The full addrinfo tuples are
    returned so the caller can pin the connection to these exact addresses:
    resolving once and connecting to the result closes the DNS-rebinding gap.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return None
    return infos


class ResPartner(models.Model):
    _inherit = "res.partner"

    enrich_status = fields.Selection(
        [("pending", "Pending"), ("done", "Done"), ("error", "Error")],
        string="Web autocomplete status",
        copy=False,
        readonly=True,
    )
    enrich_error = fields.Char(string="Web autocomplete error", copy=False)

    def _extract_clean_domain(self):
        """Return the clean root domain from the website, or False without one."""
        self.ensure_one()
        if not self.website:
            return False
        return (
            re.sub(r"^https?://(www\.)?", "", self.website)
            .split("/")[0]
            .strip()
            .lower()
        )

    def _is_public_url(self, url):
        """Return False for non http(s) schemes and for private or loopback hosts.

        The logo_url comes from an untrusted page: without this check the server
        could be asked for http://127.0.0.1:8069 or http://169.254.169.254.
        """
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        return bool(_public_ip_from_hostname(parsed.hostname))

    def _safe_get(self, url, timeout, headers, stream=False):
        """requests.get with the host resolved once to a validated global IP.

        Resolves the hostname once, rejects non-global addresses, and pins the
        connection to those addresses so a second (rebinding) DNS answer cannot
        point the request at an internal host. Redirects are NOT followed: the
        caller re-validates each hop. The getaddrinfo patch only intercepts the
        target host and only ever returns validated global IPs, so concurrent
        requests for other hosts are unaffected.

        With the `async_enrich` context (the cron), a 429/503 with a Retry-After
        header is retried once after sleeping. The user request never sleeps.
        """
        resp = self._pinned_get(url, timeout, headers, stream)
        if (
            resp is not None
            and resp.status_code in (429, 503)
            and self.env.context.get("async_enrich")
        ):
            retry_after = resp.headers.get("Retry-After", "")
            resp.close()
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), MAX_RETRY_AFTER))
                resp = self._pinned_get(url, timeout, headers, stream)
        return resp

    def _pinned_get(self, url, timeout, headers, stream):
        """One GET with the connection pinned to the validated host addresses."""
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        resolved = _public_ip_from_hostname(parsed.hostname)
        if not resolved:
            _logger.info("URL not allowed (private host or non http scheme): %s", url)
            return None
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        real_getaddrinfo = socket.getaddrinfo

        def pinned(host, *args, **kwargs):
            if host != parsed.hostname:
                return real_getaddrinfo(host, *args, **kwargs)
            return [
                (family, socktype, proto, canonname, (sockaddr[0], port) + sockaddr[2:])
                for family, socktype, proto, canonname, sockaddr in resolved
            ]

        socket.getaddrinfo = pinned
        try:
            return requests.get(
                url,
                timeout=timeout,
                headers=headers,
                allow_redirects=False,
                stream=stream,
            )
        except Exception as err:
            _logger.debug("Could not connect to %s: %s", url, err)
            return None
        finally:
            socket.getaddrinfo = real_getaddrinfo

    def _safe_fetch(self, url, timeout, headers, max_redirects=5, stream=False):
        """Follow redirects, re-validating every hop as public.

        Without this a public page could 302 to http://169.254.169.254 and
        requests would fetch the cloud metadata.
        """
        for _hop in range(max_redirects + 1):
            resp = self._safe_get(url, timeout, headers, stream=stream)
            if resp is None:
                return None, url
            location = resp.headers.get("Location")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                resp.close()
                url = urljoin(url, location)
                continue
            return resp, resp.url
        return None, url

    def _fetch_html_tree(self, url, timeout=4):
        """Download a URL; return (lxml tree, final URL) or (None, url) on failure."""
        resp, final_url = self._safe_fetch(url, timeout, FETCH_HEADERS)
        if resp is None:
            return None, final_url
        if resp.ok and "text/html" in resp.headers.get("Content-Type", ""):
            return html.fromstring(resp.content), resp.url
        return None, resp.url

    def _parse_json_ld(self, tree):
        """Return name/vat/phone/address/logo from the JSON-LD script tags."""
        data = {}
        scripts = tree.xpath('//script[@type="application/ld+json"]/text()')
        for raw_script in scripts:
            try:
                payload = json.loads(raw_script.strip())
            except Exception:
                _logger.debug("Could not parse JSON-LD: %s", raw_script)
                continue

            items = []
            if isinstance(payload, list):
                items.extend(payload)
            elif isinstance(payload, dict):
                if isinstance(payload.get("@graph"), list):
                    items.extend(payload["@graph"])
                else:
                    items.append(payload)

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("@type", ""))
                if not any(
                    t in item_type
                    for t in (
                        "Organization",
                        "LocalBusiness",
                        "Corporation",
                        "Store",
                        "WebSite",
                    )
                ):
                    continue

                # setdefault would let an empty first org block the others
                found = {
                    "name": item.get("legalName") or item.get("name"),
                    "vat": item.get("vatID") or item.get("taxID"),
                    "phone": item.get("telephone"),
                }

                addr = item.get("address")
                if isinstance(addr, list) and addr:
                    addr = addr[0]
                if isinstance(addr, dict):
                    country = addr.get("addressCountry")
                    if isinstance(country, dict):
                        country = country.get("name")
                    found.update(
                        street=addr.get("streetAddress"),
                        zip=addr.get("postalCode"),
                        city=addr.get("addressLocality"),
                        region=addr.get("addressRegion"),
                        country_code=country,
                    )

                logo = item.get("logo")
                if isinstance(logo, dict):
                    logo = logo.get("url")
                found["logo_url"] = logo

                for key, value in found.items():
                    if value:
                        data.setdefault(key, value)

        return {k: v.strip() if isinstance(v, str) else v for k, v in data.items() if v}

    def _guess_vat_country(self, raw_vat, urls=()):
        """Return the VAT country: the prefix it carries (ES, PT, FR...) or the TLD
        of the final domain (acme.cat -> ES: Catalan site without an explicit prefix).
        """
        clean = re.sub(r"[\s.-]", "", raw_vat).upper()
        prefix = NIF_COUNTRY_PREFIX.match(clean)
        if prefix and self.env["res.country"].search_count([("code", "=", prefix[1])]):
            return prefix[1]
        for url in urls:
            tld = urlparse(url).netloc.rsplit(".", 1)[-1].lower()
            if tld in ES_TLDS:
                return "ES"
            if tld in OTHER_COUNTRY_TLDS:
                return OTHER_COUNTRY_TLDS[tld]
        # shortcut: an unprefixed VAT on a foreign/unlisted TLD is assumed Spanish;
        # it is only kept when the ES checksum passes, otherwise dropped.
        return "ES"

    def _normalize_and_validate_vat(self, raw_vat, country_code="ES"):
        """Return the VAT as PREFIX+number, or False when it does not validate."""
        if not raw_vat:
            return False
        country_code = country_code.upper()
        clean = re.sub(r"[\s.-]", "", raw_vat).upper()
        if not clean.startswith(country_code):
            clean = f"{country_code}{clean}"
        # base_vat 18 has no check_vat_<cc> per country: simple_vat_check uses stdnum
        number = clean[len(country_code) :]
        if not self.simple_vat_check(country_code.lower(), number):
            return False
        return clean

    def _extract_from_unstructured_text(self, tree):
        """Look for VAT, phone and address in the page text.

        Only the VAT (checksum-validated) and tel: links are reliable for a phone.
        The company name is not guessed here: the "… S.L." regex produced invented
        names. It comes from JSON-LD, og:site_name or <title>.
        """
        extracted = {}
        full_text = " ".join(
            tree.xpath(
                "//body//text()[not(ancestor::script) and not(ancestor::style)"
                " and not(ancestor::noscript)]"
            )
        )

        for match in SPANISH_VAT_REGEX.finditer(full_text):
            # "ES" alone before the number is enough to call it Spanish
            candidate = self._normalize_and_validate_vat(match.group(1), "ES")
            if candidate:
                extracted["vat"] = candidate
                break

        # tel: hrefs are not always clean: some sites percent-encode them
        # ("tel:%28+34%29934425867"), so they are decoded and passed through the
        # same normaliser as the page text.
        tel_links = tree.xpath('//a[starts-with(@href, "tel:")]/@href')
        phone = next(
            (p for p in map(self._normalize_phone, map(unquote, tel_links)) if p),
            False,
        )
        if not phone:
            phone = self._normalize_phone(full_text)
        if phone:
            extracted["phone"] = phone

        address = self._extract_address(full_text)
        if address:
            extracted.update(address)

        return extracted

    @staticmethod
    def _normalize_phone(text):
        """Return the first Spanish phone in free text as +34XXXXXXXXX.

        Accepts the groupings in PHONE_GROUPINGS. Where there are several groups,
        any separator is allowed, even a different one in each gap
        ("955 12-34-56"), because sites mix them freely.
        """
        for match in SPANISH_PHONE_REGEX.finditer(text):
            raw = match.group("number")
            digits = re.sub(r"\D", "", raw)
            if digits.startswith("0034") and len(digits) == 13:
                digits = digits[4:]
                raw = raw[raw.index("0034") + 4 :]
            elif digits.startswith("34") and len(digits) == 11 and "+" in raw:
                digits = digits[2:]
                raw = raw[raw.index("+") + 3 :]
            if len(digits) != 9 or not digits.startswith(("6", "7", "8", "9")):
                continue
            if tuple(len(g) for g in re.findall(r"\d+", raw)) not in PHONE_GROUPINGS:
                continue
            return f"+34{digits}"
        return False

    @staticmethod
    def _extract_address(text):
        """Return the first address with a Spanish postcode: street, zip and town."""
        match = SPANISH_ADDRESS_REGEX.search(text)
        if not match:
            return {}
        return {
            "street": " ".join(match.group("street").split()),
            "zip": match.group("zip"),
            "city": match.group("city").rstrip(" ."),
        }

    @staticmethod
    def _host_matches(host, base_host):
        """Same host, allowing the www. hop.

        The substring comparison used to accept acme.com.evil.net for acme.com.
        """
        host = (host or "").lower().removeprefix("www.")
        base_host = (base_host or "").lower().removeprefix("www.")
        return bool(host) and host == base_host

    def _find_legal_page_url(self, tree, base_url):
        """Find the Legal Notice or Contact link inside the same domain."""
        base_host = urlparse(base_url).hostname
        for a_tag in tree.xpath("//a[@href]"):
            href = a_tag.get("href", "").strip()
            text = (a_tag.text_content() or "").strip().lower()
            combined = f"{href.lower()} {text}"
            if not any(k in combined for k in LEGAL_KEYWORDS):
                continue
            full_url = urljoin(base_url, href)
            if self._host_matches(urlparse(full_url).hostname, base_host):
                return full_url
        return False

    def _icon_candidates(self, tree, base_url):
        """Favicon URLs declared in <head>, best first, same host only.

        apple-touch-icon first (usually 180x180), then rel="icon". Ties between
        the same family are broken by the size in the file name, so WordPress
        sites give their 192x192 instead of their 32x32.
        """
        host = urlparse(base_url).hostname
        links = tree.xpath(
            "//link[contains(translate(@rel, 'ICON', 'icon'), 'icon') and @href]"
        )
        scored = []
        for link in links:
            url = urljoin(base_url, link.get("href").strip())
            if not self._host_matches(urlparse(url).hostname, host):
                continue
            size = ICON_SIZE_IN_NAME.search(urlparse(url).path)
            area = int(size.group(1)) * int(size.group(2)) if size else 0
            apple = "apple-touch-icon" in link.get("rel", "").lower()
            scored.append(((not apple, -area), url))
        return [url for _score, url in sorted(scored)]

    def _fetch_favicon(self, tree, base_url):
        """Best favicon for the homepage: apple-touch-icon, rel="icon", /favicon.ico.

        No external services. Returns the first candidate that passes
        _fetch_logo's quality filter (size and aspect ratio).
        """
        urls = self._icon_candidates(tree, base_url)
        urls.append(urljoin(base_url, "/favicon.ico"))
        for url in urls:
            logo = self._fetch_logo(url)
            if logo:
                return logo
        return False

    @staticmethod
    def _to_png(content):
        """Return PNG bytes if content is a decodable image, False otherwise.

        image_1920 raises UserError on what it cannot decode and does not accept
        .ico: an ICO is converted using its largest size.
        """
        try:
            image = Image.open(io.BytesIO(content))
            if image.format == "ICO":
                image = image.ico.getimage(
                    max(image.ico.sizes(), key=lambda size: size[0] * size[1])
                )
            image = image.convert("RGBA")
        except Exception:
            return False
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _read_bounded(resp, max_bytes, chunk_size=8192):
        """Read resp body up to max_bytes; False when it is larger."""
        chunks = []
        size = 0
        for chunk in resp.iter_content(chunk_size=chunk_size):
            size += len(chunk)
            if size > max_bytes:
                return False
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch_logo(self, url, max_bytes=2 * 1024 * 1024):
        """Return the logo as base64 PNG if it is a valid, usable image.

        Rejects what would look broken in the contact card: tiny icons
        (< LOGO_MIN_SIDE) and banners (> LOGO_MAX_RATIO), because image_1920 is
        square and pads or squeezes them. Rejecting means "try the next
        candidate" for the favicon chain and "no logo" for a JSON-LD logo.

        The body is read with a hard cap (not checked after the fact), so a
        server that keeps sending past max_bytes is dropped, not buffered.
        """
        if not url:
            return False
        resp = None
        try:
            resp, _final = self._safe_fetch(url, 3, FETCH_HEADERS, stream=True)
            if resp is None:
                return False
            content_type = resp.headers.get("Content-Type", "")
            if not (resp.ok and content_type.startswith("image/")):
                return False
            content_length = resp.headers.get("Content-Length", "")
            if content_length.isdigit() and int(content_length) > max_bytes:
                return False
            content = self._read_bounded(resp, max_bytes)
            if content is False:
                return False
            png = self._to_png(content)
            if not png:
                return False
            width, height = Image.open(io.BytesIO(png)).size
            if min(width, height) < LOGO_MIN_SIDE:
                _logger.debug("Logo too small %s (%sx%s)", url, width, height)
                return False
            if max(width, height) > LOGO_MAX_RATIO * min(width, height):
                _logger.debug("Logo is a banner %s (%sx%s)", url, width, height)
                return False
            return base64.b64encode(png)
        except Exception as err:
            _logger.debug("Logo dropped %s: %s", url, err)
            return False
        finally:
            if resp is not None:
                resp.close()

    def _apply_geo_data(self, info, vals):
        """Add country and state to vals (a single write with the rest of the data)."""
        country_code = info.get("country_code")
        vat = vals.get("vat") or self.vat
        if not country_code and vat and vat.startswith("ES"):
            country_code = "ES"

        country = self.env["res.country"]
        if country_code:
            # exact code first (ES, PT...), then full name. Never slice the name
            # to two letters: "Austria"[:2] is "AU" (Australia), "Chile" -> CH.
            country = country.search([("code", "=", country_code.upper())], limit=1)
            if not country:
                country = country.search([("name", "=ilike", country_code)], limit=1)
            if country and not (vals.get("country_id") or self.country_id):
                vals["country_id"] = country.id

        zip_code = vals.get("zip") or info.get("zip") or self.zip
        if not (zip_code and len(zip_code) == 5 and zip_code.isdigit()):
            return
        if vals.get("state_id") or self.state_id or vals.get("country_id"):
            return
        # Spanish postcode: the first two digits are the province (ISO 3166-2:ES)
        es_country = self.env.ref("base.es", raise_if_not_found=False)
        if es_country:
            state = self.env["res.country.state"].search(
                [
                    ("country_id", "=", es_country.id),
                    ("code", "=", zip_code[:2]),
                ],
                limit=1,
            )
            if state:
                vals["state_id"] = state.id
                vals["country_id"] = es_country.id

    def _enrich_partner_from_web(self, domain):
        """Extract JSON-LD and Legal Notice data for one res.partner record.

        Only empty fields are filled: existing data is never overwritten.
        Returns True when something was written, False otherwise.
        """
        self.ensure_one()
        vals = {}

        if not self.website:
            vals["website"] = f"https://{domain}"

        home_tree, final_url = self._fetch_html_tree(f"https://{domain}")
        if home_tree is None:
            return False

        info = self._parse_json_ld(home_tree)

        if not info.get("name"):
            og_site = home_tree.xpath('//meta[@property="og:site_name"]/@content')
            title = home_tree.xpath("//title/text()")
            if og_site:
                info["name"] = og_site[0].strip()
            elif title:
                info["name"] = title[0].split("|")[0].split("-")[0].strip()

        for key, value in self._extract_from_unstructured_text(home_tree).items():
            info.setdefault(key, value)

        legal_url = False
        if not info.get("vat"):
            legal_url = self._find_legal_page_url(home_tree, final_url)
            if legal_url:
                legal_tree, _url = self._fetch_html_tree(legal_url, timeout=4)
                if legal_tree is not None:
                    legal_data = {
                        **self._parse_json_ld(legal_tree),
                        **self._extract_from_unstructured_text(legal_tree),
                    }
                    for key, value in legal_data.items():
                        info.setdefault(key, value)

        candidate_name = info.get("name")
        # the name is often the bare domain (a copy-paste placeholder): replaceable
        if candidate_name and (
            not self.name
            or self.name == domain
            or self.name == self._extract_clean_domain()
        ):
            vals["name"] = candidate_name

        # an invalid VAT is dropped: check_vat would reject it on write.
        vat = None
        if info.get("vat"):
            country_code = self._guess_vat_country(info["vat"], (final_url, legal_url))
            vat = self._normalize_and_validate_vat(info["vat"], country_code)
        if vat and not self.vat:
            vals["vat"] = vat

        if info.get("phone") and not self.phone:
            vals["phone"] = info["phone"]

        for field in ("street", "city", "zip"):
            if info.get(field) and not self[field]:
                vals[field] = info[field]

        if not self.image_1920:
            # the JSON-LD logo is often an SVG or a 404: fall back to the favicon
            logo_url = info.get("logo_url")
            if logo_url and not urlparse(logo_url).scheme:
                logo_url = urljoin(final_url, logo_url)
            logo = self._fetch_logo(logo_url) or self._fetch_favicon(
                home_tree, final_url
            )
            if logo:
                vals["image_1920"] = logo

        self._apply_geo_data(info, vals)

        if vals:
            self.write(vals)
            return True
        return False

    def action_enrich_from_domain(self):
        """Enrich a single contact now; queue a bulk selection for the cron.

        A single contact keeps the immediate feedback (success / no data / error).
        A bulk selection is what actually blocks the worker, so it is only flagged
        and drained later by _cron_enrich_pending. The throttle lives in the cron:
        the user request never sleeps.
        """
        if len(self) == 1:
            domain = self._extract_clean_domain()
            if not domain:
                raise UserError(_("Set a valid website before autocompleting."))
            updated = self._enrich_partner_from_web(domain)
            # clear any queued state so the cron does not re-pick this contact
            self.write({"enrich_status": "done", "enrich_error": False})
            msg = (
                _("Data extracted successfully from the website domain.")
                if updated
                else _("No additional data found on the domain website.")
            )
            notif_type = "success" if updated else "warning"
        else:
            pending = self.filtered(lambda p: p._extract_clean_domain())
            pending.write({"enrich_status": "pending", "enrich_error": False})
            msg = _("Queued %s of %s contacts.") % (len(pending), len(self))
            notif_type = "info"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Web Autocomplete"),
                "message": msg,
                "type": notif_type,
                "sticky": False,
            },
        }

    def _cron_enrich_pending(self, limit=20):
        """Drain the pending queue in small, throttled batches (run by ir.cron).

        - `limit` caps a run, so the cron thread is never busy for long and the
          worker stays free; the rest stays pending for the next run.
        - Every partner is wrapped in its own savepoint: one failure is rolled
          back alone and the rest of the batch still gets processed.
        - Throttle between contacts and per host, so one domain is not hammered.
        """
        partners = self.with_context(async_enrich=True).search(
            [("enrich_status", "=", "pending")], limit=limit, order="id"
        )
        for index, partner in enumerate(partners):
            domain = partner._extract_clean_domain()
            if not domain:
                partner.write(
                    {"enrich_status": "error", "enrich_error": _("No website set")}
                )
                continue

            if index:
                time.sleep(THROTTLE_BETWEEN_PARTNERS)
            self._throttle_host(domain)

            try:
                with self.env.cr.savepoint():
                    partner._enrich_partner_from_web(domain)
                    partner.write({"enrich_status": "done", "enrich_error": False})
            except Exception as err:
                # warning, not exception: a failed contact is handled (marked
                # error) and retried, it must not trip OCA's checklog detector
                _logger.warning(
                    "Async enrichment of partner %s failed: %s", partner.id, err
                )
                partner.write({"enrich_status": "error", "enrich_error": str(err)})

    @staticmethod
    def _throttle_host(domain):
        """Sleep only the missing part before hitting the same host again."""
        now = time.monotonic()
        wait = THROTTLE_PER_DOMAIN - (now - _LAST_HIT_PER_HOST.get(domain, 0))
        if wait > 0:
            time.sleep(wait)
        _LAST_HIT_PER_HOST[domain] = time.monotonic()
