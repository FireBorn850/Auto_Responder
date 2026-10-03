import io
import re

from PIL import Image, ImageDraw
from qrcode import QRCode, constants

# ---------------------------------------------------------------------------
# Themes
#
# "print"  -> classic dark-on-white. Used for PNG downloads and the PDF print
#             templates: maximum contrast, scans reliably on paper with every
#             phone camera.
# "dark"   -> light modules on a transparent background, for showing the code
#             inside the app's dark UI (no glaring white square).
# "light"  -> navy modules on a transparent background, for the app's light UI.
# ---------------------------------------------------------------------------
THEMES = {
    "print": {"fg": "#0B1220", "bg": (255, 255, 255, 255), "eye": None},
    "dark": {"fg": "#F7F5F0", "bg": (0, 0, 0, 0), "eye": "#E8A83C"},
    "light": {"fg": "#0F2A4A", "bg": (0, 0, 0, 0), "eye": "#1E3A5F"},
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_SUPERSAMPLE = 4          # draw big, then downscale -> smooth anti-aliased edges
_BORDER = 2               # quiet zone, in modules


def _hex_to_rgb(value):
    m = _HEX_RE.match(value or "")
    if not m:
        return None
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(rgb):
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _resolve_colors(theme, fill_color):
    """Pick module + eye colors. A user-chosen color is honoured only when it
    has enough contrast for the theme's background (e.g. black modules are
    unreadable on the dark UI, so they fall back to the theme default)."""
    t = THEMES.get(theme, THEMES["print"])
    fg = _hex_to_rgb(t["fg"])
    eye = _hex_to_rgb(t["eye"]) if t["eye"] else None

    custom = _hex_to_rgb(fill_color)
    if custom and custom != (0, 0, 0):
        lum = _luminance(custom)
        readable = lum > 0.35 if theme == "dark" else lum < 0.55
        if readable:
            fg = custom
            eye = custom
    return fg, (eye or fg), t["bg"]


def _in_finder(r, c, n):
    """True if module (r, c) belongs to one of the three 7x7 finder patterns."""
    return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)


def _draw_finder(draw, x, y, unit, color, bg):
    """Rounded 'eye': outer ring, gap, inner rounded square."""
    outer = 7 * unit
    draw.rounded_rectangle([x, y, x + outer - 1, y + outer - 1], radius=int(unit * 2.2), fill=color)
    draw.rounded_rectangle(
        [x + unit, y + unit, x + outer - unit - 1, y + outer - unit - 1],
        radius=int(unit * 1.5), fill=bg,
    )
    draw.rounded_rectangle(
        [x + 2 * unit, y + 2 * unit, x + outer - 2 * unit - 1, y + outer - 2 * unit - 1],
        radius=int(unit * 1.0), fill=color,
    )


def generate_qr_with_logo(
    data: str,
    logo_path: str = None,
    fill_color: str = "#000000",
    size: int = 500,
    theme: str = "print",
) -> io.BytesIO:
    """
    Renders a styled QR code (rounded dots, rounded finder eyes) as a PNG
    buffer. If logo_path is given, the logo is placed in the centre on a
    rounded backing tile and high error correction is used so the code
    stays scannable.
    """
    if theme not in THEMES:
        theme = "print"

    qr = QRCode(
        error_correction=constants.ERROR_CORRECT_H if logo_path else constants.ERROR_CORRECT_Q,
        box_size=1,
        border=0,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)

    fg, eye, bg = _resolve_colors(theme, fill_color)
    fg_rgba, eye_rgba = fg + (255,), eye + (255,)

    total = n + 2 * _BORDER
    unit = max(4, (size * _SUPERSAMPLE) // total)
    canvas = total * unit
    img = Image.new("RGBA", (canvas, canvas), bg)
    draw = ImageDraw.Draw(img)

    # Reserve the centre for the logo so no half-covered dots peek out.
    logo_box = None
    if logo_path:
        span = int(n * 0.24) | 1               # odd number of modules
        start = (n - span) // 2
        logo_box = (start, start + span)

    # Data modules as slightly inset circles.
    inset = unit * 0.08
    for r in range(n):
        for c in range(n):
            if not matrix[r][c] or _in_finder(r, c, n):
                continue
            if logo_box and logo_box[0] <= r < logo_box[1] and logo_box[0] <= c < logo_box[1]:
                continue
            x = (c + _BORDER) * unit
            y = (r + _BORDER) * unit
            draw.ellipse([x + inset, y + inset, x + unit - inset, y + unit - inset], fill=fg_rgba)

    # Finder eyes.
    for (r, c) in ((0, 0), (0, n - 7), (n - 7, 0)):
        _draw_finder(draw, (c + _BORDER) * unit, (r + _BORDER) * unit, unit, eye_rgba, bg)

    if logo_path and logo_box:
        try:
            tile = (logo_box[1] - logo_box[0]) * unit
            tx = ty = (logo_box[0] + _BORDER) * unit
            tile_bg = (255, 255, 255, 255) if theme == "print" else (17, 26, 44, 255)
            if theme == "light":
                tile_bg = (255, 255, 255, 255)
            draw.rounded_rectangle([tx, ty, tx + tile - 1, ty + tile - 1], radius=int(unit * 1.6), fill=tile_bg)

            logo = Image.open(logo_path).convert("RGBA")
            pad = int(unit * 0.8)
            logo.thumbnail((tile - 2 * pad, tile - 2 * pad), Image.LANCZOS)
            mask = Image.new("L", logo.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, logo.width - 1, logo.height - 1], radius=int(unit), fill=255)
            alpha = Image.composite(logo.getchannel("A"), mask, mask)
            logo.putalpha(alpha)
            img.alpha_composite(logo, (tx + (tile - logo.width) // 2, ty + (tile - logo.height) // 2))
        except Exception:
            pass  # missing/corrupt logo -> plain QR rather than an error

    img = img.resize((size, size), Image.LANCZOS)
    if theme == "print":
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer
