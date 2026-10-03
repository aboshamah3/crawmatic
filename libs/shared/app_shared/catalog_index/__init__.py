"""The catalog index: a fleet-wide copy of public KSA storefront
listings used to propose competitor matches.

* ``keys``: the pure key and text helpers (one definition for the loader
  and the lookup).
* ``lookup``: the staged lookup over the active generation.

Tables and their load contract: ``app_shared.models.catalog_index``.
Loader: ``scripts/load_catalog_index.py``.
"""
