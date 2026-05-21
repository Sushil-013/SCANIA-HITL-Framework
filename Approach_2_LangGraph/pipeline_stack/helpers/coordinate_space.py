from pathlib import Path

from PIL import Image


def build_render_metadata(source_image_path, prepared_image_path, pdf_page=1, pdf_dpi=450):
    source_path = Path(source_image_path).resolve()
    prepared_path = Path(prepared_image_path).resolve()
    with Image.open(prepared_path) as image:
        page_width_px, page_height_px = image.size

    metadata = {
        "source_type": "pdf" if source_path.suffix.lower() == ".pdf" else "image",
        "source_image_path": str(source_path),
        "prepared_image_path": str(prepared_path),
        "pdf_page": int(pdf_page) if source_path.suffix.lower() == ".pdf" else None,
        "render_dpi": int(pdf_dpi) if source_path.suffix.lower() == ".pdf" else None,
        "page_image_width_px": int(page_width_px),
        "page_image_height_px": int(page_height_px),
        "pdf_page_width_pt": None,
        "pdf_page_height_pt": None,
        "bbox_coordinate_space": "rendered_page_image_pixels",
        "bbox_origin": "top_left",
        "bbox_format": "[x1, y1, x2, y2]",
        "coordinate_system_description": "All bbox values use the rendered page image pixel grid with a top-left origin.",
    }

    if source_path.suffix.lower() == ".pdf":
        try:
            import fitz
        except ImportError:
            return metadata

        with fitz.open(str(source_path)) as document:
            page_index = max(0, min(int(pdf_page) - 1, document.page_count - 1))
            page = document.load_page(page_index)
            metadata["pdf_page_width_pt"] = round(float(page.rect.width), 2)
            metadata["pdf_page_height_pt"] = round(float(page.rect.height), 2)

    return metadata


def build_coordinate_system_instruction(render_metadata):
    return (
        "Return bbox coordinates only in the pixel coordinate system of the provided rendered image. "
        f"Image width is {int(render_metadata['page_image_width_px'])}px and height is {int(render_metadata['page_image_height_px'])}px. "
        "Origin is top-left. bbox format is [x1, y1, x2, y2]. "
        "Do not use PDF points, millimeters, normalized coordinates, or approximate page units."
    )


def clamp_bbox_to_page_bounds(bbox, page_width, page_height):
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None, False
    try:
        x1, y1, x2, y2 = [int(round(float(value))) for value in bbox[:4]]
    except (TypeError, ValueError):
        return None, False

    original = [x1, y1, x2, y2]
    x1 = max(0, min(page_width, x1))
    x2 = max(0, min(page_width, x2))
    y1 = max(0, min(page_height, y1))
    y2 = max(0, min(page_height, y2))

    if page_width > 0 and x2 <= x1:
        if x1 >= page_width:
            x1 = max(0, page_width - 1)
            x2 = page_width
        else:
            x2 = min(page_width, x1 + 1)

    if page_height > 0 and y2 <= y1:
        if y1 >= page_height:
            y1 = max(0, page_height - 1)
            y2 = page_height
        else:
            y2 = min(page_height, y1 + 1)

    clamped = [x1, y1, x2, y2]
    return clamped, clamped != original


def _apply_bbox_to_item(item, bbox_key, warning_key, page_width, page_height):
    if not isinstance(item, dict):
        return False
    bbox = item.get(bbox_key)
    clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(bbox, page_width, page_height)
    if clamped_bbox is None:
        return False
    item[bbox_key] = clamped_bbox
    if was_clamped:
        item[warning_key] = "clamped_to_page_bounds"
    else:
        item.pop(warning_key, None)
    return True


def apply_result_coordinate_space(result, render_metadata):
    result.update(render_metadata)
    page_width = int(render_metadata["page_image_width_px"])
    page_height = int(render_metadata["page_image_height_px"])

    title_block = result.get("title_block") or {}
    _apply_bbox_to_item(title_block, "bbox", "bbox_warning", page_width, page_height)
    for field in title_block.get("fields", []):
        _apply_bbox_to_item(field, "bbox", "bbox_warning", page_width, page_height)

    bom = result.get("bill_of_materials") or {}
    _apply_bbox_to_item(bom, "bbox", "bbox_warning", page_width, page_height)
    for column in bom.get("columns", []):
        _apply_bbox_to_item(column, "bbox", "bbox_warning", page_width, page_height)
    for row in bom.get("rows", []):
        _apply_bbox_to_item(row, "bbox", "bbox_warning", page_width, page_height)
        for cell in row.get("cells", []):
            _apply_bbox_to_item(cell, "bbox", "bbox_warning", page_width, page_height)

    for note in result.get("notes", []):
        _apply_bbox_to_item(note, "bbox", "bbox_warning", page_width, page_height)

    for annotation in result.get("other_annotations", []):
        _apply_bbox_to_item(annotation, "bbox", "bbox_warning", page_width, page_height)

    for view in result.get("views", []):
        _apply_bbox_to_item(view, "view_bbox", "view_bbox_warning", page_width, page_height)
        for dimension in view.get("dimensions", []):
            _apply_bbox_to_item(dimension, "bbox", "bbox_warning", page_width, page_height)
        for datum in view.get("datums", []):
            _apply_bbox_to_item(datum, "bbox", "bbox_warning", page_width, page_height)
        for fcf in view.get("feature_control_frames", []):
            _apply_bbox_to_item(fcf, "bbox", "bbox_warning", page_width, page_height)

    unassigned = result.get("unassigned") or {}
    for dimension in unassigned.get("dimensions", []):
        _apply_bbox_to_item(dimension, "bbox", "bbox_warning", page_width, page_height)
    for datum in unassigned.get("datums", []):
        _apply_bbox_to_item(datum, "bbox", "bbox_warning", page_width, page_height)
    for fcf in unassigned.get("feature_control_frames", []):
        _apply_bbox_to_item(fcf, "bbox", "bbox_warning", page_width, page_height)

    return result
