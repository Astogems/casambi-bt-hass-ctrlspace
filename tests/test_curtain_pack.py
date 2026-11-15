"""Tests for the curtain state packing helpers.

The helpers live in a module without Home Assistant or CasambiBt imports, so it
is loaded straight from its file instead of through the integration package
(whose __init__ imports Home Assistant).
"""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

import pytest

_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "casambi_bt"
    / "curtain_profiles.py"
)
_SPEC = importlib.util.spec_from_file_location("curtain_profiles", _PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
cp = importlib.util.module_from_spec(_SPEC)
sys.modules["curtain_profiles"] = cp
_SPEC.loader.exec_module(cp)

P = cp.CURTAIN_PROFILES[36404]
P_INV = replace(P, inverted=True)


def test_pack_position_only() -> None:
    """Raw 54 lands in bits 3-9."""
    assert cp.pack_state(P, 54, None) == bytes.fromhex("b001")


def test_pack_stop() -> None:
    """The stop bit is bit 0."""
    assert cp.pack_state(P, 54, P.stop_bit) == bytes.fromhex("b101")


@pytest.mark.parametrize(("bit", "low_byte"), [("open_bit", 0x02), ("close_bit", 0x04)])
def test_pack_open_close(bit: str, low_byte: int) -> None:
    """Open and close are bits 1 and 2."""
    assert cp.pack_state(P, 0, getattr(P, bit)) == bytes([low_byte, 0x00])


def test_pack_masks_position() -> None:
    """Out-of-range raw positions can't spill into other bits."""
    assert cp.pack_state(P, 0xFF, None) == cp.pack_state(P, 0x7F, None)
    assert cp.pack_state(P, 127, None) == bytes.fromhex("f803")


@pytest.mark.parametrize(("pct", "raw"), [(0, 0), (42, 53), (43, 55), (100, 127)])
def test_pct_to_raw(pct: int, raw: int) -> None:
    """Percent maps linearly onto 0..127."""
    assert cp.pct_to_raw(P, pct) == raw


@pytest.mark.parametrize(("pct", "raw"), [(0, 127), (42, 74), (43, 72), (100, 0)])
def test_pct_to_raw_inverted(pct: int, raw: int) -> None:
    """Inverted motors map 0 % (closed) to raw 127."""
    assert cp.pct_to_raw(P_INV, pct) == raw


def test_pct_to_raw_clamps() -> None:
    """Values outside 0..100 are clamped."""
    assert cp.pct_to_raw(P, -5) == 0
    assert cp.pct_to_raw(P, 150) == 127


@pytest.mark.parametrize("profile", [P, P_INV])
@pytest.mark.parametrize("pct", [0, 42, 43, 100])
def test_round_trip(profile: object, pct: int) -> None:
    """Percent -> raw -> library slider -> percent is lossless at 7 bits."""
    raw = cp.pct_to_raw(profile, pct)
    slider = raw << (8 - 7)  # what the library stores in state.slider
    assert cp.slider_to_pct(profile, slider) == pct


def test_decode_app_example() -> None:
    """The Casambi app showed 42.5 % for raw 54 (library slider 108)."""
    assert cp.slider_to_raw(P, 108) == 54
    assert cp.slider_to_pct(P, 108) == 43
    assert cp.slider_to_pct(P_INV, 108) == 57
