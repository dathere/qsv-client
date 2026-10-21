"""The tray icon: the qsv logo's hexagon, with a dot showing the daemon's state.

The only module that imports Pillow.
"""

from __future__ import annotations

import functools
from importlib import resources

from PIL import Image, ImageDraw

from .model import IconState

COLORS: dict[IconState, str] = {
    "stopped": "#9ca3af",
    "running": "#16a34a",
    "busy": "#f59e0b",
    "attention": "#db2777",  # not blue: the logo is blue, and a pairing should stand out
    "error": "#dc2626",
}


@functools.cache
def _logo() -> Image.Image:
    with resources.files(__package__).joinpath("icons/qsv.png").open("rb") as f:
        return Image.open(f).convert("RGBA")


@functools.cache
def image(state: IconState, size: int = 64) -> Image.Image:
    """The logo at ``size`` px, with a dot in the bottom-right corner (beside the hexagon's
    point, so it hides none of the logo). Cached: the result must not be modified."""
    icon = _logo().resize((size, size), Image.Resampling.LANCZOS)
    radius = size * 0.17
    center = size - radius - 1
    ImageDraw.Draw(icon).ellipse(
        (center - radius, center - radius, center + radius, center + radius),
        fill=COLORS[state],
        # a light ring keeps the dot visible on dark menu bars and on the logo's navy edge
        outline="#ffffff",
        width=max(1, size // 32),
    )
    return icon
