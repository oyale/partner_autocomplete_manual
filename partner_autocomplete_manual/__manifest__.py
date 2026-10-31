# Copyright 2025 OCA Contributors
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
{
    "name": "Partner Autocomplete Manual (No IAP)",
    "summary": "Autocomplete company name, VAT, phone, address and logo from the "
    "contact's website, without Odoo IAP.",
    "version": "18.0.1.0.1",
    "category": "Contacts",
    "author": "Pelayo García",
    "maintainers": ["oyale"],
    "website": "https://github.com/oyale/partner_autocomplete_manual",
    "license": "LGPL-3",
    "development_status": "Beta",
    "depends": [
        "base",
        "base_vat",
    ],
    "external_dependencies": {
        "python": ["requests", "lxml", "Pillow"],
    },
    "data": [
        "data/ir_cron.xml",
        "views/res_partner_views.xml",
    ],
    "installable": True,
    "application": False,
    # Cannot express the exclusion from partner_autocomplete (IAP) here: Odoo 18
    # requires the auto_install trigger to be listed in depends, and a negative
    # dependency does not exist. See models/ir_module_module.py _check and README.
    "auto_install": False,
}
