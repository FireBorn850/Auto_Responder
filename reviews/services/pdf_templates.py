import io
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .qr_generator import generate_qr_with_logo


def _qr_reader(target_url, logo_path=None, size=500):
    buffer = generate_qr_with_logo(target_url, logo_path=logo_path, size=size)
    return ImageReader(buffer)


def generate_table_tent_pdf(qr, target_url, logo_path=None):
    """
    A5-sized tent card, printed once and folded in half — QR + business
    name on the front-facing half, a short prompt on the back half.
    Includes a 3mm bleed margin and a dashed fold line.
    """
    buffer = io.BytesIO()
    width, height = 148 * mm, 210 * mm  # A5 portrait, folds to A6
    c = canvas.Canvas(buffer, pagesize=(width, height))

    fold_y = height / 2

    # --- Top half (upside down, so it reads correctly once folded) ---
    c.saveState()
    c.translate(width, fold_y)
    c.rotate(180)
    qr_img = _qr_reader(target_url, logo_path=logo_path, size=500)
    qr_size = 70 * mm
    c.drawImage(qr_img, (width - qr_size) / 2, 25 * mm, qr_size, qr_size, mask='auto')
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, 98 * mm, "Scan to leave a review")
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, 15 * mm, qr.title)
    c.restoreState()

    # --- Fold line ---
    c.setDash(3, 3)
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.line(5 * mm, fold_y, width - 5 * mm, fold_y)
    c.setDash()

    # --- Bottom half (reads right-side up) ---
    qr_img2 = _qr_reader(target_url, logo_path=logo_path, size=500)
    c.drawImage(qr_img2, (width - qr_size) / 2, fold_y + 25 * mm, qr_size, qr_size, mask='auto')
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, fold_y + 98 * mm, "Scan to leave a review")
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, fold_y + 15 * mm, qr.title)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def generate_sticker_sheet_pdf(qr, target_url, logo_path=None):
    """
    Letter-sized sheet of small circular-cut-ready square stickers,
    6 per sheet (2 columns x 3 rows), each with a cut guide border.
    """
    buffer = io.BytesIO()
    width, height = letter
    c = canvas.Canvas(buffer, pagesize=letter)

    sticker_size = 70 * mm
    margin_x = (width - (2 * sticker_size)) / 3
    margin_y = 20 * mm
    gap_y = 10 * mm

    qr_img = _qr_reader(target_url, logo_path=logo_path, size=400)

    for row in range(3):
        for col in range(2):
            x = margin_x + col * (sticker_size + margin_x)
            y = height - margin_y - (row + 1) * sticker_size - row * gap_y

            # Cut guide
            c.setDash(2, 2)
            c.setStrokeColorRGB(0.75, 0.75, 0.75)
            c.rect(x, y, sticker_size, sticker_size)
            c.setDash()

            pad = 6 * mm
            c.drawImage(qr_img, x + pad, y + pad + 6 * mm, sticker_size - pad * 2, sticker_size - pad * 2 - 6 * mm, mask='auto')
            c.setFont("Helvetica-Bold", 8)
            c.drawCentredString(x + sticker_size / 2, y + pad, "Scan to review us")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def generate_door_sign_pdf(qr, target_url, logo_path=None):
    """
    Single large A4 sign — one big centered QR code, meant for a door,
    window, or counter stand.
    """
    buffer = io.BytesIO()
    width, height = 210 * mm, 297 * mm  # A4 portrait
    c = canvas.Canvas(buffer, pagesize=(width, height))

    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2, height - 50 * mm, "We'd love your feedback!")

    qr_img = _qr_reader(target_url, logo_path=logo_path, size=800)
    qr_size = 120 * mm
    c.drawImage(qr_img, (width - qr_size) / 2, (height - qr_size) / 2, qr_size, qr_size, mask='auto')

    c.setFont("Helvetica", 16)
    c.drawCentredString(width / 2, (height - qr_size) / 2 - 25 * mm, "Scan the code above to leave a review")
    c.setFont("Helvetica", 11)
    c.drawCentredString(width / 2, 20 * mm, qr.title)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer