"""QR rendering. segno is pure Python, so the container needs no system libs."""

from __future__ import annotations

import segno

# XSSI/HTML safety: only used on URLs we build ourselves from the request base URL.
_UNSAFE = ('"', "<", ">")


def svg(data: str, scale: int = 5, border: int = 2, dark: str = "#0f2f22") -> str:
    """Inline SVG string for template embedding (no <img>, no extra route)."""
    if any(ch in data for ch in _UNSAFE):
        raise ValueError("refusing to encode unsafe characters")
    qr = segno.make(data, error="m")
    # svg_inline() already forces xmldecl=False and svgns=True internally.
    return qr.svg_inline(scale=scale, border=border, dark=dark, light=None)


def png(data: str, scale: int = 8, border: int = 2) -> bytes:
    import io

    qr = segno.make(data, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border, dark="#0f2f22")
    return buf.getvalue()
