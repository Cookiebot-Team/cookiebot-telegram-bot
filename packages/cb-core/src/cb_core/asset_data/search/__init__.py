"""`cb_core.image_search`'s blocklist — v1's `Bot/Static/avoid_search.txt`,
vendored byte-for-byte.

A package rather than a bare directory for the same reason every other
`asset_data` subpackage is one: `setuptools.packages.find` only ships what it
recognises as a package, and `importlib.resources` is what reads the file
once this is an installed wheel rather than a checkout.
"""
