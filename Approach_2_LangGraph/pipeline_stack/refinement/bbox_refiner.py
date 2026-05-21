"""
Pipeline stage 03:
bbox_refiner.py - local CRAFT/GLM-OCR bbox refinement.
"""

import copy
import importlib
import math
import re
import sys
from argparse import Namespace
from difflib import SequenceMatcher
from pathlib import Path


_CRAFT_RUNTIME_CACHE = {}


def preprocess_drawing_image(image):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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


def load_input_drawing(image_path, pdf_page=1, pdf_dpi=450, preprocess=True):
    from PIL import Image

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
        raise RuntimeError("PDF input requires PyMuPDF for bbox refinement.") from exc

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


def normalize_match_text(text):
    normalized = str(text or "")
    replacements = {
        "\u2300": "\u00d8",
        "\u03a6": "\u00d8",
        "\u00f8": "\u00d8",
        "\u00d8": "\u00d8",
        "\u00b1": "+/-",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = normalized.lower().replace(",", ".")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"[^a-z0-9+\-./()]+", "", normalized)
    return normalized


def rect_from_bbox(bbox):
    if not bbox:
        return None
    if len(bbox) == 4:
        return [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
    xs = bbox[0::2]
    ys = bbox[1::2]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def bbox_iou(bbox_a, bbox_b):
    rect_a = rect_from_bbox(bbox_a)
    rect_b = rect_from_bbox(bbox_b)
    if not rect_a or not rect_b:
        return 0.0
    ax1, ay1, ax2, ay2 = rect_a
    bx1, by1, bx2, by2 = rect_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter_area / float(area_a + area_b - inter_area)


def union_rect(rects):
    rects = [rect for rect in rects if rect]
    if not rects:
        return None
    return [
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    ]


def expand_rect(rect, pad_x, pad_y):
    if not rect:
        return None
    return [
        int(round(rect[0] - pad_x)),
        int(round(rect[1] - pad_y)),
        int(round(rect[2] + pad_x)),
        int(round(rect[3] + pad_y)),
    ]


def rect_center(rect):
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def center_distance(rect_a, rect_b):
    if not rect_a or not rect_b:
        return float("inf")
    ax, ay = rect_center(rect_a)
    bx, by = rect_center(rect_b)
    return math.hypot(ax - bx, ay - by)


def similarity_score(left, right):
    left_norm = normalize_match_text(left)
    right_norm = normalize_match_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(a=left_norm, b=right_norm).ratio()


def _load_craft_module():
    package_root = Path(__file__).resolve().parents[2]
    craft_dir = package_root / "craft_runtime"
    if not craft_dir.exists():
        raise RuntimeError("CRAFT runtime folder not found inside openai_pipeline: {}".format(craft_dir))

    cache_key = str(craft_dir.resolve())
    module = _CRAFT_RUNTIME_CACHE.get(("module", cache_key))
    if module is not None:
        return module, craft_dir

    try:
        module = importlib.import_module("openai_pipeline.craft_runtime.craft_glmocr_pipeline")
    except ImportError:
        if str(package_root.parent) not in sys.path:
            sys.path.insert(0, str(package_root.parent))
        module = importlib.import_module("openai_pipeline.craft_runtime.craft_glmocr_pipeline")
    _CRAFT_RUNTIME_CACHE[("module", cache_key)] = module
    return module, craft_dir


def _load_craft_runtime(glm_model_name, max_new_tokens, use_cuda):
    module, craft_dir = _load_craft_module()
    cache_key = ("runtime", str(craft_dir.resolve()), glm_model_name, int(max_new_tokens), bool(use_cuda))
    runtime = _CRAFT_RUNTIME_CACHE.get(cache_key)
    if runtime is not None:
        return runtime

    args = Namespace(
        image="",
        document_id=None,
        trained_model=str((craft_dir / "weights" / "craft_mlt_25k.pth").resolve()),
        text_threshold=0.7,
        low_text=0.4,
        link_threshold=0.4,
        cuda=use_cuda,
        canvas_size=1280,
        mag_ratio=1.5,
        poly=False,
        show_time=False,
        refine=False,
        refiner_model=str((craft_dir / "weights" / "craft_refiner_CTW1500.pth").resolve()),
        glm_model=glm_model_name,
        max_new_tokens=max_new_tokens,
        result_folder="",
        schema_version="1.0",
        enable_view_assignment=False,
        openai_model="gpt-5.4-mini",
        openai_api_key=None,
    )
    net, refine_net, use_cuda_runtime = module.load_craft_model(args)
    processor, glm_model = module.load_glmocr_model(glm_model_name)
    runtime = {
        "module": module,
        "args": args,
        "net": net,
        "refine_net": refine_net,
        "use_cuda": use_cuda_runtime,
        "processor": processor,
        "glm_model": glm_model,
    }
    _CRAFT_RUNTIME_CACHE[cache_key] = runtime
    return runtime


def run_craft_text_localizer(image_path, pdf_page=1, pdf_dpi=450, glm_model_name="zai-org/GLM-OCR", max_new_tokens=512, use_cuda=True, preprocess=True):
    import numpy as np

    runtime = _load_craft_runtime(glm_model_name=glm_model_name, max_new_tokens=max_new_tokens, use_cuda=use_cuda)
    module = runtime["module"]

    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=preprocess)
    try:
        cv_image = np.array(image)[:, :, ::-1].copy()
        boxes, crop_shapes, _ = module.detect_text_with_craft(
            cv_image,
            runtime["net"],
            runtime["args"],
            refine_net=runtime["refine_net"],
            use_cuda=runtime["use_cuda"],
        )
        crops = module.crop_regions(image, crop_shapes)
        texts = [
            module.recognize_with_glmocr(crop, runtime["processor"], runtime["glm_model"], runtime["args"].max_new_tokens)
            for crop in crops
        ]
        ocr_results = module.assemble_json(boxes, texts)
        raw_items = module.make_raw_items(ocr_results)
        merged_candidates = module.merge_raw_items(raw_items)
    finally:
        image.close()

    raw_entries = [
        {
            "text": item.get("text", ""),
            "normalized_text": item.get("normalized_text", ""),
            "rect": item.get("rect"),
            "source_ids": [item.get("ocr_id")],
            "kind": "raw",
        }
        for item in raw_items
    ]
    merged_entries = [
        {
            "text": item.get("raw_text", ""),
            "normalized_text": item.get("normalized_text", ""),
            "rect": rect_from_bbox(item.get("bbox")),
            "source_ids": item.get("source_ocr_ids", []),
            "kind": "merged",
        }
        for item in merged_candidates
    ]
    return {
        "raw_entries": raw_entries,
        "merged_entries": merged_entries,
    }


def choose_best_text_match(target_text, target_bbox, entries, min_text_score=0.55, max_distance_multiplier=6.0):
    target_rect = rect_from_bbox(target_bbox)
    target_text_norm = normalize_match_text(target_text)
    if not target_text_norm:
        return None

    target_height = max(1, (target_rect[3] - target_rect[1])) if target_rect else 24
    max_distance = target_height * max_distance_multiplier
    best = None
    best_score = -1.0

    for entry in entries:
        entry_rect = entry.get("rect")
        text_score = similarity_score(target_text_norm, entry.get("normalized_text") or entry.get("text"))
        if text_score < min_text_score:
            continue
        distance = center_distance(target_rect, entry_rect) if target_rect else 0.0
        if target_rect and distance > max_distance and bbox_iou(target_rect, entry_rect) < 0.05:
            continue
        distance_score = 1.0 if not target_rect else max(0.0, 1.0 - (distance / max(1.0, max_distance)))
        overlap_bonus = bbox_iou(target_rect, entry_rect) if target_rect else 0.0
        score = (0.72 * text_score) + (0.18 * distance_score) + (0.10 * overlap_bonus)
        if score > best_score:
            best = entry
            best_score = score

    return best


def choose_nearest_label_match(label, target_bbox, entries, exact_only=False, max_distance_multiplier=10.0):
    target_rect = rect_from_bbox(target_bbox)
    target_height = max(1, (target_rect[3] - target_rect[1])) if target_rect else 20
    max_distance = target_height * max_distance_multiplier
    label_norm = normalize_match_text(label)
    best = None
    best_distance = float("inf")

    for entry in entries:
        entry_text_norm = normalize_match_text(entry.get("text") or entry.get("normalized_text"))
        if not entry_text_norm:
            continue
        if exact_only and entry_text_norm != label_norm:
            continue
        if not exact_only and similarity_score(label_norm, entry_text_norm) < 0.6:
            continue
        distance = center_distance(target_rect, entry.get("rect")) if target_rect else 0.0
        if target_rect and distance > max_distance and bbox_iou(target_rect, entry.get("rect")) < 0.05:
            continue
        if distance < best_distance:
            best = entry
            best_distance = distance

    return best


def find_entries_inside_bbox(target_bbox, entries, padding=16):
    target_rect = rect_from_bbox(target_bbox)
    if not target_rect:
        return []
    padded = expand_rect(target_rect, padding, padding)
    matches = []
    for entry in entries:
        rect = entry.get("rect")
        if not rect:
            continue
        cx, cy = rect_center(rect)
        if padded[0] <= cx <= padded[2] and padded[1] <= cy <= padded[3]:
            matches.append(entry)
    return matches


def _replace_bbox(item, new_bbox):
    old_bbox = rect_from_bbox(item.get("bbox"))
    if not new_bbox:
        return False
    new_bbox = rect_from_bbox(new_bbox)
    if old_bbox == new_bbox:
        return False
    item["bbox"] = new_bbox
    return True


def _mark_annotation_state(items, ready=False, source="vlm"):
    for item in items:
        item["annotation_ready"] = bool(ready)
        item["annotation_source"] = source


def _iter_target_annotation_groups(result):
    for view in result.get("views", []):
        yield "dimensions", view.get("dimensions", [])
        yield "feature_control_frames", view.get("feature_control_frames", [])
        yield "datums", view.get("datums", [])
    unassigned = result.get("unassigned", {})
    yield "dimensions", unassigned.get("dimensions", [])
    yield "feature_control_frames", unassigned.get("feature_control_frames", [])
    yield "datums", unassigned.get("datums", [])


def initialize_hybrid_annotation_state(result):
    for _, items in _iter_target_annotation_groups(result):
        _mark_annotation_state(items, ready=False, source="vlm_only")


def enable_vlm_annotation_fallback(result, preserve_existing=False):
    counts = {
        "dimensions": 0,
        "feature_control_frames": 0,
        "datums": 0,
    }
    for group_name, items in _iter_target_annotation_groups(result):
        for item in items:
            if preserve_existing and item.get("annotation_ready"):
                continue
            bbox = rect_from_bbox(item.get("bbox"))
            if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                item["annotation_ready"] = False
                item["annotation_source"] = "missing_bbox"
                continue
            item["bbox"] = bbox
            item["annotation_ready"] = True
            item["annotation_source"] = "vlm_fallback"
            counts[group_name] += 1
    return counts


def count_ready_target_annotations(result):
    total = 0
    for _, items in _iter_target_annotation_groups(result):
        total += sum(1 for item in items if item.get("annotation_ready"))
    return total


def _refine_dimensions(dimensions, merged_entries):
    refined = 0
    for item in dimensions:
        match = choose_best_text_match(item.get("raw_text"), item.get("bbox"), merged_entries, min_text_score=0.45, max_distance_multiplier=8.0)
        if not match:
            item["annotation_ready"] = False
            continue
        _replace_bbox(item, match.get("rect"))
        item["annotation_ready"] = True
        item["annotation_source"] = "ocr_text_match"
        refined += 1
    return refined


def _refine_other_annotations(other_annotations, raw_entries):
    refined = 0
    for item in other_annotations:
        match = choose_nearest_label_match(item.get("label"), item.get("bbox"), raw_entries, exact_only=False, max_distance_multiplier=14.0)
        if match and _replace_bbox(item, match.get("rect")):
            refined += 1
    return refined


def _refine_datums(datums, raw_entries):
    refined = 0
    for item in datums:
        match = choose_nearest_label_match(item.get("label"), item.get("bbox"), raw_entries, exact_only=True, max_distance_multiplier=10.0)
        if not match:
            item["annotation_ready"] = False
            continue
        rect = match.get("rect")
        width = max(1, rect[2] - rect[0])
        height = max(1, rect[3] - rect[1])
        expanded = expand_rect(rect, max(6, width * 0.9), max(6, height * 0.8))
        _replace_bbox(item, expanded)
        item["annotation_ready"] = True
        item["annotation_source"] = "ocr_label_match"
        refined += 1
    return refined


def _refine_fcfs(fcfs, raw_entries):
    refined = 0
    for item in fcfs:
        nearby = find_entries_inside_bbox(item.get("bbox"), raw_entries, padding=20)
        if not nearby:
            item["annotation_ready"] = False
            continue
        rect = union_rect([entry.get("rect") for entry in nearby])
        if not rect:
            item["annotation_ready"] = False
            continue
        height = max(1, rect[3] - rect[1])
        expanded = expand_rect(rect, max(8, height * 0.5), max(6, height * 0.4))
        _replace_bbox(item, expanded)
        item["annotation_ready"] = True
        item["annotation_source"] = "ocr_region_group"
        refined += 1
    return refined


def refine_result_bboxes(
    result,
    image_path,
    pdf_page=1,
    pdf_dpi=450,
    glm_model_name="zai-org/GLM-OCR",
    max_new_tokens=512,
    use_cuda=True,
    preprocess=True,
):
    refined_result = copy.deepcopy(result)
    initialize_hybrid_annotation_state(refined_result)
    metadata = {
        "status": "skipped",
        "engine": "craft_glmocr",
        "hybrid_mode": True,
        "refined_dimensions": 0,
        "refined_feature_control_frames": 0,
        "refined_datums": 0,
        "refined_other_annotations": 0,
    }

    try:
        localization = run_craft_text_localizer(
            image_path=image_path,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
            glm_model_name=glm_model_name,
            max_new_tokens=max_new_tokens,
            use_cuda=use_cuda,
            preprocess=preprocess,
        )
    except Exception as exc:
        fallback_counts = enable_vlm_annotation_fallback(refined_result)
        metadata["status"] = "fallback_vlm"
        metadata["reason"] = str(exc)
        metadata["fallback_dimensions"] = fallback_counts["dimensions"]
        metadata["fallback_feature_control_frames"] = fallback_counts["feature_control_frames"]
        metadata["fallback_datums"] = fallback_counts["datums"]
        metadata["ready_annotation_count"] = count_ready_target_annotations(refined_result)
        refined_result["bbox_refinement"] = metadata
        return refined_result, metadata

    raw_entries = localization["raw_entries"]
    merged_entries = localization["merged_entries"]

    for view in refined_result.get("views", []):
        metadata["refined_dimensions"] += _refine_dimensions(view.get("dimensions", []), merged_entries)
        metadata["refined_feature_control_frames"] += _refine_fcfs(view.get("feature_control_frames", []), raw_entries)
        metadata["refined_datums"] += _refine_datums(view.get("datums", []), raw_entries)

    unassigned = refined_result.get("unassigned", {})
    metadata["refined_dimensions"] += _refine_dimensions(unassigned.get("dimensions", []), merged_entries)
    metadata["refined_feature_control_frames"] += _refine_fcfs(unassigned.get("feature_control_frames", []), raw_entries)
    metadata["refined_datums"] += _refine_datums(unassigned.get("datums", []), raw_entries)
    metadata["refined_other_annotations"] += _refine_other_annotations(refined_result.get("other_annotations", []), raw_entries)

    fallback_counts = enable_vlm_annotation_fallback(refined_result, preserve_existing=True)
    metadata["fallback_dimensions"] = fallback_counts["dimensions"]
    metadata["fallback_feature_control_frames"] = fallback_counts["feature_control_frames"]
    metadata["fallback_datums"] = fallback_counts["datums"]
    metadata["ready_annotation_count"] = count_ready_target_annotations(refined_result)
    metadata["ocr_entry_count"] = len(raw_entries)
    metadata["merged_entry_count"] = len(merged_entries)
    refined_target_total = (
        metadata["refined_dimensions"]
        + metadata["refined_feature_control_frames"]
        + metadata["refined_datums"]
    )
    fallback_target_total = (
        metadata["fallback_dimensions"]
        + metadata["fallback_feature_control_frames"]
        + metadata["fallback_datums"]
    )
    if refined_target_total and fallback_target_total:
        metadata["status"] = "refined_with_fallback"
    elif refined_target_total:
        metadata["status"] = "refined"
    elif fallback_target_total:
        metadata["status"] = "fallback_vlm_no_matches"
    else:
        metadata["status"] = "no_matches"
    refined_result["bbox_refinement"] = metadata
    return refined_result, metadata
