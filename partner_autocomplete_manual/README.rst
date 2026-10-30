Partner Autocomplete Manual (No IAP)
====================================

|badge1| |badge2|

Fill the company name, VAT, phone, address and logo of a contact from its own
website, **without the IAP services or Odoo Partner Autocomplete**. Everything
is extracted from the contact's own site.

It is a manual, self-hosted alternative to `partner_autocomplete
<https://github.com/odoo/odoo/tree/18.0/addons/partner_autocomplete>`_.

Coexistence with ``partner_autocomplete`` (IAP)
-----------------------------------------------

This module is **not meant to run alongside** the official
``partner_autocomplete``:

- ``partner_autocomplete`` auto-installs with ``iap`` in any Odoo 18 database.
- Both add domain logic on ``res.partner`` and their own widget on the form.
- With both installed the user sees two widgets and the contact is enriched
  twice from different sources.

A negative dependency cannot be expressed in the manifest: Odoo 18 requires
every ``auto_install`` trigger to be listed in ``depends``, and Odoo has no
concept of "must not be installed with". Instead
``ir.module.module._check`` logs a warning when the IAP module is installed.

What it extracts
----------------

================================  ===========================================
Field                             Source
================================  ===========================================
``name``                          ``legalName``/``name`` from JSON-LD,
                                  ``og:site_name`` or ``<title>``
``vat``                           ``vatID``/``taxID`` from JSON-LD, or the
                                  first checksum-valid VAT in the page text
``phone``                         ``telephone`` from JSON-LD, ``tel:`` links
                                  (percent-decoded), or a Spanish number in the
                                  page text
``street``, ``city``, ``zip``     ``address`` from JSON-LD
                                  (``PostalAddress``), or a Spanish address in
                                  the text (postcode required)
``country_id``, ``state_id``      country from JSON-LD/VAT, province from the
                                  Spanish postcode
``image_1920``                    ``logo`` from JSON-LD, or the favicon in
                                  ``<head>``
================================  ===========================================

Behind the homepage it also visits the Legal Notice / Privacy / Contact link on
the same domain, where the VAT and company name usually live.

Languages
~~~~~~~~~

Built for Spanish contacts, including the co-official languages:

- Legal links are recognised in Spanish (*aviso legal*), Catalan/Valencian
  (*avís legal*), Galician (*aviso legal*) and Basque (*lege oharra*), plus
  English.
- A VAT without prefix is validated as Spanish on ``.es``, ``.cat``, ``.gal``,
  ``.eus`` and ``.va`` domains. An explicit prefix (``PT…``, ``FR…``) or a
  foreign TLD (``acme.pt``) wins.
- Street types cover Spanish (``Calle``, ``Avenida``, ``Plaza``…),
  Catalan/Valencian (``Carrer``, ``Avinguda``, ``Plaça``, ``Passeig``…),
  Galician (``Rúa``, ``Praza``, ``Estrada``) and Basque (``Kalea``,
  ``Etorbidea``, ``Pasealekua``).
- The page language does not change validation rules: Catalan, Galician, Basque
  and Valencian use the same Spanish VAT.

Writing rules
~~~~~~~~~~~~~

- **Only empty fields are filled.** Existing data is never overwritten.
- A VAT that does not validate is dropped, never stored.
- A logo that cannot be decoded, is smaller than 64px or is clearly a banner
  (wider than 3:1) is dropped; the rest of the data is still saved.
- A single write per contact, so ``check_vat`` runs once.

Security
~~~~~~~~

- Logo capped at 2 MB and verified as a decodable image (``.ico`` converted to
  PNG).
- Only ``http(s)`` URLs resolving to public IPs: ``localhost``, private ranges
  and cloud metadata (``169.254.169.254``) are rejected.
- The Legal Notice link must be on the same host as the homepage (exact match,
  allowing ``www.``), not a substring.
- The bulk server action is restricted to the *Settings* group.

Usage
-----

- **Autocomplete from Web** button on the contact form.
- **Autocomplete data from Web (No IAP)** action on list and form.

Requires ``requests``, ``lxml`` and ``Pillow`` in the Odoo environment.


.. |badge1| image:: https://img.shields.io/badge/licence-LGPL--3-blue.png
    :target: http://www.gnu.org/licenses/lgpl-3.0-standalone.html
    :alt: License: LGPL-3
.. |badge2| image:: https://img.shields.io/badge/maturity-Beta-yellow.png
    :target: https://odoo-community.org/page/development-status
    :alt: Beta
