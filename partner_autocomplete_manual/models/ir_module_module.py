# Copyright 2025 OCA Contributors
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class IrModuleModule(models.Model):
    _inherit = "ir.module.module"

    def _check(self):
        """Warn when partner_autocomplete (IAP) coexists with this module: both
        enrich res.partner by domain.

        This cannot be expressed in the manifest: Odoo 18 only accepts auto_install
        triggers listed in depends, and a negative dependency does not exist.
        """
        res = super()._check()
        ours = self.filtered(lambda m: m.name == "partner_autocomplete_manual")
        if not ours or ours.state not in ("installed", "to install", "to upgrade"):
            return res
        # partner_autocomplete (IAP) is NOT a dependency of this module, so look
        # it up globally, not through dependencies_id (which only lists base/base_vat).
        iap = self.env["ir.module.module"].search_count(
            [("name", "=", "partner_autocomplete"), ("state", "=", "installed")]
        )
        if iap:
            _logger.warning(
                "partner_autocomplete_manual: partner_autocomplete (IAP) is "
                "installed and both enrich res.partner by domain. Uninstall one."
            )
        return res
