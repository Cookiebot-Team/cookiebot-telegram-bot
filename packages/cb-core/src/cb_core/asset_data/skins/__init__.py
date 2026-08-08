"""Per-skin asset overrides — `core_botskins`' asset-pack mechanism.

One subdirectory per skin, mirroring the layout of `asset_data` itself:
`skins/bombot/doomlist/silence_scammer.jpg` overrides
`doomlist/silence_scammer.jpg` for the `bombot` skin only. A skin ships only
what it rebrands and inherits the rest — see `cb_core/skins.py:asset`.

Empty today on purpose: the mechanism is what this feature owed, and no brand
has supplied artwork yet. Adding one is a directory, not a code change.
"""
