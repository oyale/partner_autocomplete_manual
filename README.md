# Partner Autocomplete Manual (No IAP)

Odoo 18 addon: autocomplete the company name, VAT, phone, address and logo of a
contact from its own website, without Odoo IAP.

See the [addon README](partner_autocomplete_manual/README.rst) for the full
feature list, extraction rules and security notes.

## Install

```sh
pip install odoo-addon-partner-autocomplete-manual
```

## Releasing

1. Bump the version in `partner_autocomplete_manual/__manifest__.py`.
2. Commit and tag:

   ```sh
   git tag 18.0.1.0.0
   git push --tags
   ```

Pushing a `18.0.*` tag triggers the release workflow: it builds the wheel with
[whool](https://github.com/acsone/whool), publishes it to PyPI
(`odoo-addon-partner-autocomplete-manual`) and creates a GitHub release.
