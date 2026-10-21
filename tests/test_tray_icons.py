"""The tray icon images (needs Pillow: the tray extra)."""

from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from qsv_client.tray import icons
from qsv_client.tray.model import IconState


@pytest.mark.parametrize("state", list(icons.COLORS))
def test_the_dot_shows_the_state(state: IconState) -> None:
    image = icons.image(state)
    assert image.size == (64, 64) and image.mode == "RGBA"
    radius = 64 * 0.17
    center = round(64 - radius - 1)
    color = icons.COLORS[state]
    expected = (*(int(color[i : i + 2], 16) for i in (1, 3, 5)), 255)
    assert image.getpixel((center, center)) == expected


def test_the_logo_is_untouched_away_from_the_dot() -> None:
    running, error = icons.image("running"), icons.image("error")
    assert running.getpixel((32, 20)) == error.getpixel((32, 20))
    assert running.getpixel((0, 0)) == (0, 0, 0, 0)  # transparent corner


def test_sizes_and_caching() -> None:
    assert icons.image("busy", 22).size == (22, 22)
    assert icons.image("busy", 22) is icons.image("busy", 22)
