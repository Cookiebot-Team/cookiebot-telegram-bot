"""`cb_core.legacy_assets`'s generated catalogs — v1's private-bucket cutover
export, grouped back into per-prefix CSVs by `python scripts/cb.py
legacy-catalog` (`cb_worker/bucket_export/catalog.py`).

Empty in a fresh checkout: this package ships unconditionally so
`cb_core.legacy_assets` never hits an `ImportError` (its own module
docstring), but the `*.csv` files it describes are generated artifacts, not
checked in until someone runs `legacy-catalog` against a finished
`bucket-export` manifest.
"""
