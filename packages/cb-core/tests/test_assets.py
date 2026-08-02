"""v1 static-media port.

Backwards compatibility here means byte-for-byte equality with v1's
`Bot/Static/reclamacao/` directory, so the first class of tests below diffs the
copied files against v1's originals directly (skipped when the reference repo
isn't checked out next to this one, e.g. in a clean clone or CI without the
sibling repos — same skip idiom as `packages/cb-core/tests/test_locales.py`).
The rest exercise the public `cb_core.assets` API without touching v1 at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cb_core import assets

V1_RECLAMACAO = Path(__file__).resolve().parents[3].parent / (
    "COOKIEBOT-Telegram-Group-Bot/Bot/Static/reclamacao"
)
V2_COMPLAINT_ASSETS = Path(__file__).resolve().parents[1] / "src/cb_core/asset_data/complaint"

# v1 picks from whatever os.listdir returns; hold8.wav does not exist and must
# not be invented (D-CP-5).
_EXPECTED_FILES = (
    "hold1.wav",
    "hold2.wav",
    "hold3.wav",
    "hold4.wav",
    "hold5.wav",
    "hold6.wav",
    "hold7.wav",
    "hold9.wav",
    "milton_eng.jpg",
    "milton_pt.jpg",
)


def _require_v1() -> None:
    if not V1_RECLAMACAO.is_dir():
        pytest.skip(f"v1 reference repo not present at {V1_RECLAMACAO}")


class TestByteIdenticalToV1:
    @pytest.mark.parametrize("filename", _EXPECTED_FILES)
    def test_copied_file_matches_v1_byte_for_byte(self, filename: str) -> None:
        _require_v1()
        v1_bytes = (V1_RECLAMACAO / filename).read_bytes()
        v2_bytes = (V2_COMPLAINT_ASSETS / filename).read_bytes()
        assert v2_bytes == v1_bytes

    def test_no_extra_files_were_copied(self) -> None:
        _require_v1()
        v1_names = {p.name for p in V1_RECLAMACAO.iterdir()}
        v2_names = {p.name for p in V2_COMPLAINT_ASSETS.iterdir() if p.name != "__init__.py"}
        assert v2_names == v1_names


class TestPath:
    def test_resolves_a_file_under_asset_data(self) -> None:
        resolved = assets.path("complaint", "milton_pt.jpg")
        assert resolved.is_file()
        assert resolved.name == "milton_pt.jpg"

    def test_resolves_a_directory_under_asset_data(self) -> None:
        resolved = assets.path("complaint")
        assert resolved.is_dir()


class TestPool:
    def test_returns_exactly_the_eight_wav_files_sorted(self) -> None:
        result = assets.pool("complaint", suffix=".wav")
        assert [p.name for p in result] == [
            "hold1.wav",
            "hold2.wav",
            "hold3.wav",
            "hold4.wav",
            "hold5.wav",
            "hold6.wav",
            "hold7.wav",
            "hold9.wav",
        ]

    def test_hold8_is_absent(self) -> None:
        # D-CP-5: v1 never had hold8.wav; do not renumber the pool to fill it in.
        result = assets.pool("complaint", suffix=".wav")
        assert "hold8.wav" not in [p.name for p in result]

    def test_jpg_pool_returns_both_photos_sorted(self) -> None:
        result = assets.pool("complaint", suffix=".jpg")
        assert [p.name for p in result] == ["milton_eng.jpg", "milton_pt.jpg"]

    def test_pool_is_a_tuple(self) -> None:
        assert isinstance(assets.pool("complaint", suffix=".wav"), tuple)
