import io
from qrcode import QRCode, constants
from qrcode.image.pil import PilImage
from PIL import Image


def generate_qr_with_logo(data: str, logo_path: str = None, fill_color: str = "#000000", size: int = 500) -> io.BytesIO:
    """
    Generates a QR code PNG as an in-memory buffer. If logo_path is given,
    pastes that image in the center — uses high error correction so the
    code stays scannable even with part of it covered.
    """
    qr = QRCode(
        error_correction=constants.ERROR_CORRECT_H if logo_path else constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(image_factory=PilImage, fill_color=fill_color, back_color="white").convert('RGB')
    img = img.resize((size, size), Image.LANCZOS)

    if logo_path:
        try:
            logo = Image.open(logo_path).convert('RGBA')
            # Logo should cover roughly 22% of the QR width — big enough to
            # be visible, small enough that error correction can compensate.
            logo_size = int(size * 0.22)
            logo.thumbnail((logo_size, logo_size), Image.LANCZOS)

            # White backing square so the logo stays legible on any QR color
            pad = 10
            backing = Image.new('RGBA', (logo.width + pad * 2, logo.height + pad * 2), (255, 255, 255, 255))
            backing.paste(logo, (pad, pad), logo if logo.mode == 'RGBA' else None)

            pos = ((size - backing.width) // 2, (size - backing.height) // 2)
            img = img.convert('RGBA')
            img.paste(backing, pos, backing)
            img = img.convert('RGB')
        except Exception:
            pass  # if the logo file is missing/corrupt, fall back to a plain QR rather than failing

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer