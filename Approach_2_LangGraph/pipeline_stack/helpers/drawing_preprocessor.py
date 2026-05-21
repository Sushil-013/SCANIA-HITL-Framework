import base64
import io
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def preprocess_drawing_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=1)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.25)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(1.35)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=1.2, percent=165, threshold=2))

    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1600:
        scale = min(2.0, 1600.0 / float(shortest_side))
        resized = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
        return resized.convert("RGB")
    return grayscale.convert("RGB")


def enhance_crop_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=0)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.55)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(1.75)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=1.0, percent=190, threshold=1))
    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1300:
        scale = min(3.0, 1300.0 / float(max(1, shortest_side)))
        grayscale = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
    return grayscale.convert("RGB")


def enhance_dimension_view_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=0)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.85)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(2.1)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=0.8, percent=230, threshold=1))
    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1800:
        scale = min(3.2, 1800.0 / float(max(1, shortest_side)))
        grayscale = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
    return grayscale.convert("RGB")


def load_input_drawing(image_path, pdf_page=1, pdf_dpi=450, preprocess=True):
    path = Path(image_path)
    if path.suffix.lower() != ".pdf":
        image = Image.open(path).convert("RGB")
        return preprocess_drawing_image(image) if preprocess else image

    if pdf_page < 1:
        raise ValueError("pdf_page must be 1 or greater.")
    if pdf_dpi < 72:
        raise ValueError("pdf_dpi must be at least 72.")

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError(
            "PDF input requires PyMuPDF. Install it with `pip install PyMuPDF` or update the environment from requirements.txt."
        ) from exc

    with fitz.open(str(path)) as document:
        if document.page_count < 1:
            raise RuntimeError("PDF input does not contain any pages: {}".format(path))
        page_index = pdf_page - 1
        if page_index >= document.page_count:
            raise RuntimeError(
                "Requested PDF page {} but {} only has {} page(s).".format(pdf_page, path.name, document.page_count)
            )
        page = document.load_page(page_index)
        scale = float(pdf_dpi) / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return preprocess_drawing_image(image) if preprocess else image


def detect_pdf_sheet_info(image_path, pdf_page=1):
    path = Path(image_path)
    if path.suffix.lower() != ".pdf":
        return {"source_type": "image", "sheet_size": "unknown", "orientation": "unknown"}

    try:
        import fitz
    except ImportError:
        return {"source_type": "pdf", "sheet_size": "unknown", "orientation": "unknown"}

    iso_sizes = {
        "A0": (841.0, 1189.0),
        "A1": (594.0, 841.0),
        "A2": (420.0, 594.0),
        "A3": (297.0, 420.0),
        "A4": (210.0, 297.0),
    }
    with fitz.open(str(path)) as document:
        page_index = max(0, min(pdf_page - 1, document.page_count - 1))
        rect = document.load_page(page_index).rect
        width_mm = float(rect.width) * 25.4 / 72.0
        height_mm = float(rect.height) * 25.4 / 72.0

    orientation = "landscape" if width_mm >= height_mm else "portrait"
    short_side = min(width_mm, height_mm)
    long_side = max(width_mm, height_mm)
    best_name = "unknown"
    best_error = None
    for name, (iso_short, iso_long) in iso_sizes.items():
        error = abs(short_side - iso_short) + abs(long_side - iso_long)
        if best_error is None or error < best_error:
            best_name = name
            best_error = error
    if best_error is not None and best_error > 35.0:
        best_name = "custom"

    return {
        "source_type": "pdf",
        "sheet_size": best_name,
        "orientation": orientation,
        "width_mm": round(width_mm, 2),
        "height_mm": round(height_mm, 2),
        "pdf_page": pdf_page,
    }


def image_to_data_url(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return "data:image/png;base64,{}".format(encoded)


def build_input_image_data_url(image_path, pdf_page=1, pdf_dpi=450, preprocess=True):
    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=preprocess)
    try:
        return image_to_data_url(image)
    finally:
        image.close()
