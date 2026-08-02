"""Unit coverage for `cb_worker.collage` — pure compositing, in-memory images
only, no real photos needed. Proves D-BD-3's fix directly: differently-sized
inputs (the case that crashes v1's own `make_birthday_collage`) composite
without error here.
"""

from __future__ import annotations

from PIL import Image

from cb_worker import collage


def _solid(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, color)


class TestResizeToCell:
    def test_resizes_to_the_requested_square(self) -> None:
        image = _solid((64, 128), (255, 0, 0, 255))
        resized = collage.resize_to_cell(image, size=200)
        assert resized.size == (200, 200)

    def test_converts_to_rgba(self) -> None:
        image = Image.new("RGB", (10, 10), (0, 255, 0))
        resized = collage.resize_to_cell(image)
        assert resized.mode == "RGBA"


class TestBuildGrid:
    def test_one_image_is_a_one_cell_grid(self) -> None:
        grid = collage.build_grid([_solid((256, 256), (255, 0, 0, 255))])
        assert grid.size == (256, 256)

    def test_four_images_make_a_two_by_two_grid(self) -> None:
        images = [_solid((256, 256), (255, 0, 0, 255)) for _ in range(4)]
        grid = collage.build_grid(images)
        assert grid.size == (512, 512)

    def test_three_images_make_a_two_by_two_grid_with_one_empty_cell(self) -> None:
        # v1's own math: width = ceil(sqrt(3)) = 2, height = ceil(3/2) = 2.
        images = [_solid((256, 256), (255, 0, 0, 255)) for _ in range(3)]
        grid = collage.build_grid(images)
        assert grid.size == (512, 512)

    def test_differently_sized_inputs_do_not_raise(self) -> None:
        """D-BD-3: v1's own equivalent would raise a numpy shape-mismatch
        error on exactly this input (two photos of different resolutions,
        the common case, not an edge case). Every input here must already
        be `resize_to_cell`'d by the caller — this test proves the grid
        itself is safe as long as that contract holds, by feeding it
        already-uniform cells built from originally different sizes."""
        originals = [_solid((64, 64), (0, 0, 0, 255)), _solid((512, 300), (0, 0, 0, 255))]
        cells = [collage.resize_to_cell(image) for image in originals]
        grid = collage.build_grid(cells)
        assert grid.size == (512, 256)

    def test_empty_list_raises(self) -> None:
        try:
            collage.build_grid([])
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for an empty image list")


class TestOverlayConfetti:
    def test_result_matches_the_grid_size(self) -> None:
        grid = collage.build_grid([_solid((256, 256), (255, 0, 0, 255))])
        confetti = _solid((1200, 1200), (0, 0, 0, 0))  # fully transparent
        result = collage.overlay_confetti(grid, confetti)
        assert result.size == grid.size

    def test_fully_transparent_confetti_leaves_the_grid_visible(self) -> None:
        grid = collage.build_grid([_solid((10, 10), (10, 20, 30, 255))])
        confetti = _solid((10, 10), (0, 0, 0, 0))
        result = collage.overlay_confetti(grid, confetti)
        assert result.getpixel((5, 5)) == (10, 20, 30, 255)

    def test_fully_opaque_confetti_covers_the_grid(self) -> None:
        grid = collage.build_grid([_solid((10, 10), (10, 20, 30, 255))])
        confetti = _solid((10, 10), (200, 200, 200, 255))
        result = collage.overlay_confetti(grid, confetti)
        assert result.getpixel((5, 5)) == (200, 200, 200, 255)


class TestBuildCollage:
    def test_full_pipeline_with_mixed_sizes(self) -> None:
        images = [
            _solid((64, 64), (255, 0, 0, 255)),
            _solid((512, 300), (0, 255, 0, 255)),
            _solid((128, 128), (0, 0, 255, 255)),
        ]
        confetti = _solid((1200, 1200), (0, 0, 0, 0))
        result = collage.build_collage(images, confetti)
        assert result.size == (collage.CELL_SIZE * 2, collage.CELL_SIZE * 2)
