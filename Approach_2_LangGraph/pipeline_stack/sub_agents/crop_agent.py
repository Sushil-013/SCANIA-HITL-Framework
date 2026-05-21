from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from ..helpers.drawing_preprocessor import (
        enhance_crop_image,
        enhance_dimension_view_image,
        image_to_data_url,
        load_input_drawing,
    )
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.drawing_preprocessor import (
        enhance_crop_image,
        enhance_dimension_view_image,
        image_to_data_url,
        load_input_drawing,
    )


def normalize_crop_preprocessing_mode(mode):
    if mode is True:
        return "dynamic"
    if mode in (False, None):
        return "skip"
    normalized = str(mode).strip().lower()
    if normalized in ("", "false", "none", "off", "0", "skip"):
        return "skip"
    if normalized in ("true", "1", "dynamic"):
        return "dynamic"
    if normalized in ("red", "red_boxes", "red-boxes", "boxes"):
        return "red_boxes"
    return "skip"


def find_foreground_bbox(image, margin=24):
    grayscale = ImageOps.grayscale(image)
    width, height = grayscale.size
    pixels = grayscale.load()
    xs = []
    ys = []
    step = max(1, min(width, height) // 900)
    for y in range(0, height, step):
        for x in range(0, width, step):
            if pixels[x, y] < 245:
                xs.append(x)
                ys.append(y)
    if not xs or not ys:
        return [0, 0, width, height]
    return [
        max(0, min(xs) - margin),
        max(0, min(ys) - margin),
        min(width, max(xs) + margin),
        min(height, max(ys) + margin),
    ]


def clamp_crop_box(box, width, height):
    x1, y1, x2, y2 = [int(round(value)) for value in box]
    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(x1 + 1, min(width, x2))
    y2 = max(y1 + 1, min(height, y2))
    return [x1, y1, x2, y2]


def expand_crop_box(box, width, height, margin_x, margin_y):
    x1, y1, x2, y2 = box
    return clamp_crop_box([x1 - margin_x, y1 - margin_y, x2 + margin_x, y2 + margin_y], width, height)


def crop_box_area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def crop_box_iou(a, b):
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    intersection = crop_box_area([x1, y1, x2, y2])
    union = crop_box_area(a) + crop_box_area(b) - intersection
    return intersection / float(union) if union > 0 else 0.0


def scale_crop_box(box, source_size, target_size):
    source_width, source_height = source_size
    target_width, target_height = target_size
    if source_width <= 0 or source_height <= 0:
        return [0, 0, target_width, target_height]
    scale_x = float(target_width) / float(source_width)
    scale_y = float(target_height) / float(source_height)
    return clamp_crop_box(
        [
            box[0] * scale_x,
            box[1] * scale_y,
            box[2] * scale_x,
            box[3] * scale_y,
        ],
        target_width,
        target_height,
    )


def mask_red_annotations(image):
    try:
        import numpy as np
    except ImportError:
        return image.copy()

    rgb = np.array(image.convert("RGB"))
    red = rgb[:, :, 0].astype("int16")
    green = rgb[:, :, 1].astype("int16")
    blue = rgb[:, :, 2].astype("int16")
    red_mask = (
        (red >= 150)
        & (green <= 170)
        & (blue <= 170)
        & ((red - green) >= 35)
        & ((red - blue) >= 35)
    )
    if not red_mask.any():
        return image.copy()
    cleaned = rgb.copy()
    cleaned[red_mask] = [255, 255, 255]
    return Image.fromarray(cleaned, mode="RGB")


def merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.08):
    merged = []
    for box in boxes:
        current = box
        changed = True
        while changed:
            changed = False
            kept = []
            for existing in merged:
                close_x = current[0] <= existing[2] and existing[0] <= current[2]
                close_y = current[1] <= existing[3] and existing[1] <= current[3]
                if crop_box_iou(current, existing) >= iou_threshold or (close_x and close_y):
                    current = clamp_crop_box(
                        [
                            min(current[0], existing[0]),
                            min(current[1], existing[1]),
                            max(current[2], existing[2]),
                            max(current[3], existing[3]),
                        ],
                        width,
                        height,
                    )
                    changed = True
                else:
                    kept.append(existing)
            merged = kept
        merged.append(current)
    return merged


def detect_red_markup_boxes(image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    rgb = np.array(image.convert("RGB"))
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)

    lower_red_1 = np.array([0, 70, 70], dtype=np.uint8)
    upper_red_1 = np.array([12, 255, 255], dtype=np.uint8)
    lower_red_2 = np.array([168, 70, 70], dtype=np.uint8)
    upper_red_2 = np.array([180, 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower_red_1, upper_red_1) | cv2.inRange(hsv, lower_red_2, upper_red_2)

    kernel_size = max(3, int(round(min(width, height) / 450.0)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    cleaned = cv2.dilate(cleaned, kernel, iterations=1)

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    image_area = float(width * height)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        bbox_area = float(w * h)
        if bbox_area < image_area * 0.0035:
            continue
        if bbox_area > image_area * 0.85:
            continue
        if w < width * 0.08 or h < height * 0.06:
            continue
        contour_area = max(1.0, float(cv2.contourArea(contour)))
        fill_ratio = contour_area / max(1.0, bbox_area)
        if fill_ratio > 0.30:
            continue
        boxes.append(expand_crop_box([x, y, x + w, y + h], width, height, max(6, width // 300), max(6, height // 300)))

    return merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.18)


def build_red_box_crop_regions(source_image, target_size=None):
    source_width, source_height = source_image.size
    target_size = target_size or source_image.size
    boxes = detect_red_markup_boxes(source_image)
    if len(boxes) < 3:
        return []

    drawing_box = max(boxes, key=crop_box_area)
    remaining = [box for box in boxes if box != drawing_box]

    left_candidates = [
        box for box in remaining
        if ((box[0] + box[2]) / 2.0) < (source_width / 2.0) and ((box[1] + box[3]) / 2.0) > (source_height * 0.45)
    ]
    right_candidates = [
        box for box in remaining
        if ((box[0] + box[2]) / 2.0) >= (source_width / 2.0) and ((box[1] + box[3]) / 2.0) > (source_height * 0.45)
    ]

    notes_box = max(left_candidates, key=crop_box_area) if left_candidates else None
    bom_box = max(right_candidates, key=crop_box_area) if right_candidates else None

    if notes_box is None or bom_box is None:
        lower_boxes = sorted(
            remaining,
            key=lambda box: (((box[1] + box[3]) / 2.0), crop_box_area(box)),
            reverse=True,
        )[:2]
        if len(lower_boxes) < 2:
            return []
        lower_boxes = sorted(lower_boxes, key=lambda box: ((box[0] + box[2]) / 2.0))
        notes_box, bom_box = lower_boxes[0], lower_boxes[1]

    scaled_drawing_box = scale_crop_box(drawing_box, source_image.size, target_size)
    scaled_notes_box = scale_crop_box(notes_box, source_image.size, target_size)
    scaled_bom_box = scale_crop_box(bom_box, source_image.size, target_size)
    target_width, target_height = target_size

    return [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet for global context and coordinate consistency.",
            "box": [0, 0, target_width, target_height],
            "enhance": False,
            "mask_red": True,
        },
        {
            "name": "drawing_views_dimensions",
            "description": "User-marked drawing region with main views, dimensions, GD&T callouts, and detail views.",
            "box": scaled_drawing_box,
            "enhance": True,
            "enhance_mode": "dimension_view",
            "mask_red": True,
        },
        {
            "name": "notes_text_zone",
            "description": "User-marked notes/specifications region.",
            "box": scaled_notes_box,
            "enhance": True,
            "enhance_mode": "text_block",
            "mask_red": True,
        },
        {
            "name": "bom_title_block_zone",
            "description": "User-marked title block and BOM region.",
            "box": scaled_bom_box,
            "enhance": True,
            "enhance_mode": "text_block",
            "mask_red": True,
        },
    ]


def detect_dense_layout_boxes(image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    grayscale = np.array(ImageOps.grayscale(image))
    height, width = grayscale.shape[:2]
    foreground = cv2.threshold(grayscale, 245, 255, cv2.THRESH_BINARY_INV)[1]
    border_x = max(8, int(width * 0.025))
    border_y = max(8, int(height * 0.025))
    foreground[:border_y, :] = 0
    foreground[-border_y:, :] = 0
    foreground[:, :border_x] = 0
    foreground[:, -border_x:] = 0

    boxes = []
    kernels = [
        (max(24, width // 120), max(8, height // 260), 3),
        (max(40, width // 75), max(12, height // 180), 3),
        (max(70, width // 45), max(18, height // 130), 2),
    ]
    for kernel_width, kernel_height, iterations in kernels:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, kernel_height))
        grouped = cv2.dilate(foreground, kernel, iterations=iterations)
        contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            if area < (width * height) * 0.0005:
                continue
            if w < width * 0.035 or h < height * 0.012:
                continue
            if w > width * 0.985 and h > height * 0.92:
                continue
            near_side_border = x < width * 0.04 or (x + w) > width * 0.96
            near_bottom_border = (y + h) > height * 0.96
            if near_side_border and w < width * 0.10:
                continue
            if near_bottom_border and h < height * 0.08 and w < width * 0.18:
                continue
            boxes.append(expand_crop_box([x, y, x + w, y + h], width, height, width // 85, height // 85))

    return merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.18)


def describe_crop_position(box, width, height):
    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    horizontal = "left" if center_x < width / 3.0 else "right" if center_x > width * 2.0 / 3.0 else "center"
    vertical = "top" if center_y < height / 3.0 else "bottom" if center_y > height * 2.0 / 3.0 else "middle"
    return "{}_{}".format(vertical, horizontal)


def build_dynamic_crop_regions(image):
    width, height = image.size
    content_x1, content_y1, content_x2, content_y2 = find_foreground_bbox(image)
    dense_boxes = detect_dense_layout_boxes(image)
    bottom_boxes = [
        box for box in dense_boxes
        if (
            box[1] >= int(height * 0.42)
            or ((box[1] + box[3]) / 2.0) >= height * 0.62
            or box[3] >= int(height * 0.78)
        )
        and crop_box_area(box) <= width * height * 0.35
    ]
    bottom_boxes = sorted(
        bottom_boxes,
        key=lambda box: ((box[1] + box[3]) / 2.0, crop_box_area(box)),
        reverse=True,
    )

    selected_admin_boxes = []
    for box in bottom_boxes:
        if all(crop_box_iou(box, kept) < 0.25 for kept in selected_admin_boxes):
            selected_admin_boxes.append(box)
        if len(selected_admin_boxes) >= 4:
            break
    selected_area = sum(crop_box_area(box) for box in selected_admin_boxes)
    if selected_area < width * height * 0.025:
        selected_admin_boxes = []

    admin_top = min([box[1] for box in selected_admin_boxes], default=int(height * 0.72))
    drawing_box = clamp_crop_box(
        [
            max(0, content_x1 - int(width * 0.025)),
            max(0, content_y1 - int(height * 0.025)),
            min(width, content_x2 + int(width * 0.025)),
            min(height, max(int(height * 0.55), admin_top + int(height * 0.04))),
        ],
        width,
        height,
    )

    regions = [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet for global context and coordinate consistency.",
            "box": [0, 0, width, height],
            "enhance": False,
        },
        {
            "name": "drawing_views_dimensions",
            "description": "Main drawing views, dimensions, GD&T callouts, and detail views. Region is content-aware.",
            "box": drawing_box,
            "enhance": True,
            "enhance_mode": "dimension_view",
        },
    ]
    for index, box in enumerate(selected_admin_boxes, start=1):
        position = describe_crop_position(box, width, height)
        regions.append(
            {
                "name": "candidate_text_table_{}_{}".format(index, position),
                "description": "Detected text/table candidate block. Classify from content: may be notes, title block, BOM, revision fields, material, or default tolerances.",
                "box": box,
                "enhance": True,
                "enhance_mode": "text_block",
            }
        )
    if not selected_admin_boxes:
        regions.extend(
            [
                {
                    "name": "bottom_left_text_zone",
                    "description": "Fallback lower-left/bottom text zone for notes, specifications, and default tolerances.",
                    "box": clamp_crop_box([0, int(height * 0.58), int(width * 0.46), height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
                {
                    "name": "bottom_center_text_zone",
                    "description": "Fallback bottom-center text/table zone for general notes, BOM continuations, and drawing references.",
                    "box": clamp_crop_box([int(width * 0.27), int(height * 0.58), int(width * 0.73), height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
                {
                    "name": "bottom_right_text_zone",
                    "description": "Fallback lower-right/bottom text zone for title block, BOM, revision/status fields, and material.",
                    "box": clamp_crop_box([int(width * 0.54), int(height * 0.58), width, height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
            ]
        )
    return regions


def resolve_crop_regions(preprocessed_image, crop_mode, source_image=None):
    normalized_mode = normalize_crop_preprocessing_mode(crop_mode)
    if normalized_mode == "red_boxes" and source_image is not None:
        red_box_regions = build_red_box_crop_regions(source_image, target_size=preprocessed_image.size)
        if red_box_regions:
            return red_box_regions
    if normalized_mode in ("dynamic", "red_boxes"):
        return build_dynamic_crop_regions(preprocessed_image)
    width, height = preprocessed_image.size
    return [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet.",
            "box": [0, 0, width, height],
            "enhance": False,
            "mask_red": False,
        }
    ]


def prepare_region_crop(crop, region):
    working = mask_red_annotations(crop) if region.get("mask_red") else crop.copy()
    if not region.get("enhance"):
        return working

    if region.get("enhance_mode") == "dimension_view":
        prepared = enhance_dimension_view_image(working)
    else:
        prepared = enhance_crop_image(working)
    working.close()
    return prepared


def build_input_image_parts(image_path, pdf_page=1, pdf_dpi=450, preprocess=True, crop_preprocess=False):
    crop_mode = normalize_crop_preprocessing_mode(crop_preprocess)
    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=preprocess)
    source_image = None
    try:
        if crop_mode == "skip":
            return [{"name": "full_sheet", "description": "Complete drawing sheet.", "image_url": image_to_data_url(image)}]

        if crop_mode == "red_boxes":
            source_image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=False)

        parts = []
        for region in resolve_crop_regions(image, crop_mode, source_image=source_image):
            crop = image.crop(region["box"])
            prepared = None
            try:
                prepared = prepare_region_crop(crop, region)
                parts.append(
                    {
                        "name": region["name"],
                        "description": region["description"],
                        "bbox": region["box"],
                        "image_url": image_to_data_url(prepared),
                    }
                )
            finally:
                crop.close()
                if prepared is not None:
                    prepared.close()
        return parts
    finally:
        if source_image is not None:
            source_image.close()
        image.close()


def get_annotation_font(font_size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        return ImageFont.load_default()


def make_annotation_label_ascii(label):
    replacements = {
        "\u2300": "DIA",
        "\u00d8": "DIA",
        "\u00b1": "+/-",
        "\u00b0": "deg",
        "\u2264": "<=",
        "\u2265": ">=",
        "\u00b5": "u",
    }
    safe_label = str(label or "")
    for symbol, replacement in replacements.items():
        safe_label = safe_label.replace(symbol, replacement)
    return safe_label.encode("latin-1", errors="replace").decode("latin-1")


def save_crop_preprocessing_outputs(image_path, output_dir, output_stem, pdf_page=1, pdf_dpi=450, sheet_info=None, crop_mode="dynamic"):
    crop_dir = Path(output_dir) / "{}_crop_preprocessing".format(output_stem)
    crop_dir.mkdir(parents=True, exist_ok=True)

    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=True)
    source_image = None
    saved = []
    try:
        normalized_mode = normalize_crop_preprocessing_mode(crop_mode)
        if normalized_mode == "red_boxes":
            source_image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=False)
        regions = resolve_crop_regions(image, normalized_mode, source_image=source_image)

        full_path = crop_dir / "{}_preprocessed_full_sheet.png".format(output_stem)
        image.save(full_path)
        saved.append(
            {
                "name": "preprocessed_full_sheet",
                "description": "Full preprocessed drawing sheet.",
                "bbox": [0, 0, image.size[0], image.size[1]],
                "path": str(full_path.resolve()),
            }
        )

        overview = image.convert("RGBA")
        draw = ImageDraw.Draw(overview)
        font = get_annotation_font(max(18, int(round(max(image.size) / 120.0))))
        colors = {
            "drawing_views_dimensions": (34, 197, 94, 255),
            "bottom_left_text_zone": (0, 102, 255, 255),
            "bottom_center_text_zone": (168, 85, 247, 255),
            "bottom_right_text_zone": (220, 38, 38, 255),
            "notes_text_zone": (0, 102, 255, 255),
            "bom_title_block_zone": (220, 38, 38, 255),
            "full_sheet": (120, 120, 120, 255),
        }

        for region in regions:
            crop = image.crop(region["box"])
            prepared = None
            try:
                prepared = prepare_region_crop(crop, region)
                crop_path = crop_dir / "{}_{}.png".format(output_stem, region["name"])
                prepared.save(crop_path)
                saved.append(
                    {
                        "name": region["name"],
                        "description": region["description"],
                        "bbox": region["box"],
                        "enhance_mode": region.get("enhance_mode", "none"),
                        "path": str(crop_path.resolve()),
                    }
                )
            finally:
                crop.close()
                if prepared is not None:
                    prepared.close()

            if region["name"] != "full_sheet":
                x1, y1, x2, y2 = region["box"]
                outline = colors.get(
                    region["name"],
                    (0, 102, 255, 255) if region["name"].startswith("candidate_text_table") else (234, 88, 12, 255),
                )
                line_width = max(3, int(round(max(image.size) / 800.0)))
                draw.rectangle([x1, y1, x2, y2], outline=outline, width=line_width)
                label = make_annotation_label_ascii(region["name"])
                label_bbox = draw.textbbox((0, 0), label, font=font)
                label_width = label_bbox[2] - label_bbox[0]
                label_height = label_bbox[3] - label_bbox[1]
                pad = max(6, line_width * 2)
                label_y = max(0, y1 - label_height - (2 * pad))
                draw.rectangle([x1, label_y, x1 + label_width + (2 * pad), label_y + label_height + (2 * pad)], fill=outline)
                draw.text((x1 + pad, label_y + pad), label, fill=(255, 255, 255, 255), font=font)

        overview_path = crop_dir / "{}_crop_overview.png".format(output_stem)
        overview.convert("RGB").save(overview_path)
        saved.append(
            {
                "name": "crop_overview",
                "description": "Full preprocessed sheet with crop boxes overlaid.",
                "bbox": [0, 0, image.size[0], image.size[1]],
                "path": str(overview_path.resolve()),
            }
        )
        return {"status": "saved", "folder": str(crop_dir.resolve()), "sheet_info": sheet_info or {}, "outputs": saved}
    finally:
        if source_image is not None:
            source_image.close()
        image.close()
