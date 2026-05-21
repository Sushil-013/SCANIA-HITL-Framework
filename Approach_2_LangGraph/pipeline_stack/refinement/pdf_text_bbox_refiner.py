import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


def fix_mojibake(text):
    value = str(text or "")
    replacements = {
        "Ã‚Â±": "Â±",
        "ÃƒËœ": "Ã˜",
        "Ãƒâ€”": "Ã—",
        "Ã¢Å’â‚¬": "âŒ€",
        "Ã¢Ë†â€¦": "âŒ€",
        "âˆ’": "-",
        "â€“": "-",
        "â€”": "-",
        "Âµ": "Î¼",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def normalize_visible_text(text):
    value = fix_mojibake(text)
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\u0000-\u001F\u007F]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_match_text(text):
    value = normalize_visible_text(text).upper()
    replacements = {
        "⌀": "DIA",
        "Ø": "DIA",
        "Φ": "DIA",
        "±": "PM",
        "+/-": "PM",
        ",": ".",
        "×": "X",
        "°": "DEG",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = re.sub(r"[^A-Z0-9.+\-/() ]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def extract_numbers(text):
    values = []
    for token in re.findall(r"\d+(?:[.,]\d+)?", normalize_visible_text(text)):
        values.append(token.replace(",", "."))
    return values


def union_bbox(boxes):
    usable = [box for box in boxes if box and len(box) == 4]
    if not usable:
        return None
    return [
        min(box[0] for box in usable),
        min(box[1] for box in usable),
        max(box[2] for box in usable),
        max(box[3] for box in usable),
    ]


def bbox_iou(box_a, box_b):
    if not box_a or not box_b:
        return 0.0
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def center_distance(box_a, box_b):
    if not box_a or not box_b:
        return 0.0
    acx = (box_a[0] + box_a[2]) / 2.0
    acy = (box_a[1] + box_a[3]) / 2.0
    bcx = (box_b[0] + box_b[2]) / 2.0
    bcy = (box_b[1] + box_b[3]) / 2.0
    return ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5


def bbox_center(bbox):
    if not bbox or len(bbox) != 4:
        return None
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_size(bbox):
    if not bbox or len(bbox) != 4:
        return (0, 0)
    return (max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1]))


def bbox_diagonal(bbox):
    width, height = bbox_size(bbox)
    return (width ** 2 + height ** 2) ** 0.5


def expand_bbox(bbox, x_padding, y_padding):
    if not bbox or len(bbox) != 4:
        return None
    return [
        int(round(bbox[0] - x_padding)),
        int(round(bbox[1] - y_padding)),
        int(round(bbox[2] + x_padding)),
        int(round(bbox[3] + y_padding)),
    ]


def bbox_intersects(box_a, box_b):
    if not box_a or not box_b:
        return False
    return not (
        box_a[2] <= box_b[0]
        or box_a[0] >= box_b[2]
        or box_a[3] <= box_b[1]
        or box_a[1] >= box_b[3]
    )


def center_within_view(bbox, view_bbox, padding=220):
    if not bbox or not view_bbox:
        return True
    center = bbox_center(bbox)
    if center is None:
        return False
    cx, cy = center
    return (
        view_bbox[0] - padding <= cx <= view_bbox[2] + padding
        and view_bbox[1] - padding <= cy <= view_bbox[3] + padding
    )


def clamp_bbox_to_page_bounds(bbox, page_width, page_height):
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None, False
    try:
        x1, y1, x2, y2 = [int(round(float(value))) for value in bbox]
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


def scale_bbox_to_pixels(bbox_points, render_dpi):
    scale = float(render_dpi) / 72.0
    return [
        int(round(float(bbox_points[0]) * scale)),
        int(round(float(bbox_points[1]) * scale)),
        int(round(float(bbox_points[2]) * scale)),
        int(round(float(bbox_points[3]) * scale)),
    ]


def extract_native_pdf_page(source_pdf_path, pdf_page):
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF text bbox refinement requires PyMuPDF.") from exc

    pdf_path = Path(source_pdf_path).resolve()
    if pdf_path.suffix.lower() != ".pdf":
        return None

    with fitz.open(str(pdf_path)) as document:
        page_index = max(0, min(int(pdf_page) - 1, document.page_count - 1))
        page = document.load_page(page_index)
        words = []
        for item in page.get_text("words"):
            words.append(
                {
                    "bbox": [round(item[0], 2), round(item[1], 2), round(item[2], 2), round(item[3], 2)],
                    "text": normalize_visible_text(item[4]),
                    "block_no": item[5],
                    "line_no": item[6],
                    "word_no": item[7],
                }
            )
        blocks = []
        for item in page.get_text("blocks"):
            block_text = normalize_visible_text(item[4] or "")
            if not block_text:
                continue
            blocks.append(
                {
                    "bbox": [round(item[0], 2), round(item[1], 2), round(item[2], 2), round(item[3], 2)],
                    "text": block_text,
                    "block_no": item[5],
                    "block_type": item[6],
                }
            )
        return {
            "words": words,
            "blocks": blocks,
            "page_rect_points": [0, 0, round(page.rect.width, 2), round(page.rect.height, 2)],
        }


def build_word_entries(native_page, render_dpi):
    entries = []
    for item in native_page.get("words", []):
        entries.append(
            {
                "text": normalize_visible_text(item.get("text", "")),
                "norm": normalize_match_text(item.get("text", "")),
                "bbox": scale_bbox_to_pixels(item["bbox"], render_dpi),
                "block_no": item.get("block_no"),
                "line_no": item.get("line_no"),
                "word_no": item.get("word_no"),
                "word_key": (
                    item.get("block_no"),
                    item.get("line_no"),
                    item.get("word_no"),
                    normalize_visible_text(item.get("text", "")),
                ),
            }
        )
    return entries


def build_dimension_candidates(word_entries, max_window=6):
    grouped = defaultdict(list)
    for entry in word_entries:
        grouped[(entry["block_no"], entry["line_no"])].append(entry)

    by_block = defaultdict(list)
    for (block_no, line_no), items in grouped.items():
        items = sorted(items, key=lambda item: item["word_no"])
        line_text = " ".join(item["text"] for item in items).strip()
        line_bbox = union_bbox([item["bbox"] for item in items])
        by_block[block_no].append(
            {
                "line_no": line_no,
                "items": items,
                "text": line_text,
                "norm": normalize_match_text(line_text),
                "bbox": line_bbox,
            }
        )

    candidates = []
    seen = set()
    for _, lines in by_block.items():
        lines = sorted(lines, key=lambda item: item["line_no"])
        for line in lines:
            items = line["items"]
            for start in range(len(items)):
                for end in range(start + 1, min(len(items), start + max_window) + 1):
                    chunk = items[start:end]
                    text = " ".join(item["text"] for item in chunk).strip()
                    bbox = union_bbox([item["bbox"] for item in chunk])
                    norm = normalize_match_text(text)
                    key = (norm, tuple(bbox))
                    if norm and key not in seen:
                        seen.add(key)
                        candidates.append(
                            {
                                "text": text,
                                "norm": norm,
                                "bbox": bbox,
                                "source": "line_window",
                                "word_keys": [item["word_key"] for item in chunk],
                            }
                        )

        for index in range(len(lines) - 1):
            combined_lines = [lines[index], lines[index + 1]]
            text = " ".join(line["text"] for line in combined_lines if line["text"]).strip()
            bbox = union_bbox([line["bbox"] for line in combined_lines])
            norm = normalize_match_text(text)
            key = (norm, tuple(bbox))
            if norm and key not in seen:
                seen.add(key)
                candidates.append(
                    {
                        "text": text,
                        "norm": norm,
                        "bbox": bbox,
                        "source": "two_lines",
                        "word_keys": [
                            word["word_key"]
                            for line in combined_lines
                            for word in line["items"]
                        ],
                    }
                )
    return candidates


def target_tokens(text):
    return [token for token in normalize_match_text(text).split() if token]


def word_numbers(entry):
    return set(extract_numbers(entry.get("text", "")))


def select_local_word_cluster_candidate(target_text, seed_bbox, word_entries, used_word_keys=None):
    if not seed_bbox:
        return None

    used_word_keys = used_word_keys or set()
    target_norm = normalize_match_text(target_text)
    tokens = set(target_tokens(target_text))
    numbers = set(extract_numbers(target_text))
    if not target_norm or not numbers:
        return None

    nominal_number = next(iter(extract_numbers(target_text)), None)
    width, height = bbox_size(seed_bbox)
    diagonal = bbox_diagonal(seed_bbox)
    x_padding = max(260, int(width * 2.2), int(diagonal * 1.25))
    y_padding = max(180, int(height * 2.0), 260)
    search_bbox = expand_bbox(seed_bbox, x_padding, y_padding)
    if not search_bbox:
        return None

    nearby_entries = []
    for entry in word_entries:
        if not bbox_intersects(entry["bbox"], search_bbox):
            continue
        entry_norm = entry["norm"]
        entry_numbers = word_numbers(entry)
        token_match = entry_norm in tokens
        number_match = bool(entry_numbers & numbers)
        if token_match or number_match:
            nearby_entries.append(entry)

    if not nearby_entries:
        return None

    anchors = []
    for entry in nearby_entries:
        entry_numbers = word_numbers(entry)
        if nominal_number and nominal_number in entry_numbers:
            anchors.append(entry)
    if not anchors:
        anchors = nearby_entries

    best = None
    for anchor in anchors:
        anchor_box = anchor["bbox"]
        cluster_box = expand_bbox(
            anchor_box,
            max(180, int(width * 1.1), 260),
            max(160, int(height * 1.3), 240),
        )
        cluster_words = []
        covered_tokens = set()
        covered_numbers = set()
        already_used = 0
        for entry in nearby_entries:
            if not bbox_intersects(entry["bbox"], cluster_box):
                continue
            entry_norm = entry["norm"]
            entry_numbers = word_numbers(entry)
            if entry_norm in tokens or (entry_numbers & numbers):
                cluster_words.append(entry)
                if entry["word_key"] in used_word_keys:
                    already_used += 1
                if entry_norm in tokens:
                    covered_tokens.add(entry_norm)
                covered_numbers.update(entry_numbers & numbers)

        if not cluster_words:
            continue

        if nominal_number and nominal_number not in covered_numbers:
            continue

        if len(cluster_words) > 0 and already_used / float(len(cluster_words)) > 0.45:
            continue

        cluster_bbox = union_bbox([entry["bbox"] for entry in cluster_words])
        if not cluster_bbox:
            continue

        cluster_width, cluster_height = bbox_size(cluster_bbox)
        if width and cluster_width > max(720, int(width * 2.2)):
            continue
        if height and cluster_height > max(320, int(height * 2.4)):
            continue

        score = 0.0
        score += len(covered_numbers) * 28.0
        score += len(covered_tokens) * 16.0
        score += bbox_iou(seed_bbox, cluster_bbox) * 35.0
        score -= min(22.0, center_distance(seed_bbox, cluster_bbox) / 90.0)
        score -= already_used * 18.0

        candidate = {
            "text": " ".join(entry["text"] for entry in cluster_words),
            "norm": target_norm,
            "bbox": cluster_bbox,
            "source": "local_word_cluster",
            "score": score,
            "word_keys": [entry["word_key"] for entry in cluster_words],
            "covered_numbers": covered_numbers,
            "covered_tokens": covered_tokens,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    return best


def is_suspicious_seed_bbox(seed_bbox, page_width, page_height):
    if not seed_bbox or len(seed_bbox) != 4 or page_width <= 0 or page_height <= 0:
        return False
    width, height = bbox_size(seed_bbox)
    if width <= 0 or height <= 0:
        return True
    return (
        seed_bbox[2] <= int(page_width * 0.25)
        and seed_bbox[3] <= int(page_height * 0.25)
        and width <= int(page_width * 0.12)
        and height <= int(page_height * 0.08)
    )


def collect_view_word_cluster_candidates(target_text, view_bbox, word_entries, used_word_keys=None):
    if not view_bbox:
        return []

    used_word_keys = used_word_keys or set()
    target_norm = normalize_match_text(target_text)
    tokens = set(target_tokens(target_text))
    numbers = set(extract_numbers(target_text))
    nominal_number = next(iter(extract_numbers(target_text)), None)
    if not target_norm or not numbers:
        return []

    search_bbox = expand_bbox(view_bbox, 220, 220)
    if not search_bbox:
        return []

    nearby_entries = []
    for entry in word_entries:
        if not bbox_intersects(entry["bbox"], search_bbox):
            continue
        entry_norm = entry["norm"]
        entry_numbers = word_numbers(entry)
        if entry_norm in tokens or (entry_numbers & numbers):
            nearby_entries.append(entry)

    if not nearby_entries:
        return []

    anchors = []
    for entry in nearby_entries:
        entry_numbers = word_numbers(entry)
        if nominal_number and nominal_number in entry_numbers:
            anchors.append(entry)
    if not anchors:
        anchors = nearby_entries

    candidates = []
    seen = set()
    for anchor in anchors:
        anchor_box = anchor["bbox"]
        cluster_box = expand_bbox(anchor_box, 320, 250)
        if not cluster_box:
            continue
        cluster_words = []
        covered_tokens = set()
        covered_numbers = set()
        already_used = 0
        for entry in nearby_entries:
            if not bbox_intersects(entry["bbox"], cluster_box):
                continue
            entry_norm = entry["norm"]
            entry_numbers = word_numbers(entry)
            if entry_norm in tokens or (entry_numbers & numbers):
                cluster_words.append(entry)
                if entry["word_key"] in used_word_keys:
                    already_used += 1
                if entry_norm in tokens:
                    covered_tokens.add(entry_norm)
                covered_numbers.update(entry_numbers & numbers)

        if nominal_number and nominal_number not in covered_numbers:
            continue
        if len(cluster_words) > 0 and already_used / float(len(cluster_words)) > 0.55:
            continue

        cluster_bbox = union_bbox([entry["bbox"] for entry in cluster_words])
        if not cluster_bbox:
            continue
        cluster_width, cluster_height = bbox_size(cluster_bbox)
        if cluster_width > 820 or cluster_height > 340:
            continue

        center = bbox_center(cluster_bbox)
        if center is None:
            continue
        score = 0.0
        score += len(covered_numbers) * 30.0
        score += len(covered_tokens) * 16.0
        if target_norm == normalize_match_text(" ".join(entry["text"] for entry in cluster_words)):
            score += 24.0
        if "DIA" in tokens and "DIA" in covered_tokens:
            score += 14.0
        if "PM" in tokens and "PM" in covered_tokens:
            score += 10.0
        score -= already_used * 12.0
        score -= max(0.0, (center[0] - view_bbox[2]) / 120.0)
        score -= max(0.0, (view_bbox[0] - center[0]) / 120.0)
        score -= max(0.0, (center[1] - view_bbox[3]) / 120.0)
        score -= max(0.0, (view_bbox[1] - center[1]) / 120.0)

        candidate = {
            "text": " ".join(entry["text"] for entry in cluster_words),
            "norm": target_norm,
            "bbox": cluster_bbox,
            "source": "view_word_cluster",
            "score": score,
            "word_keys": [entry["word_key"] for entry in cluster_words],
            "covered_numbers": covered_numbers,
            "covered_tokens": covered_tokens,
        }
        key = tuple(candidate["bbox"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    candidates.sort(key=lambda item: (-item["score"], item["bbox"][0], item["bbox"][1]))
    return candidates


def score_text_candidate(target_text, target_bbox, candidate):
    target_norm = normalize_match_text(target_text)
    candidate_norm = candidate["norm"]
    if not target_norm or not candidate_norm:
        return -1.0

    ratio = SequenceMatcher(None, target_norm, candidate_norm).ratio()
    target_numbers = set(extract_numbers(target_text))
    candidate_numbers = set(extract_numbers(candidate["text"]))
    common_numbers = len(target_numbers & candidate_numbers)

    score = ratio * 100.0
    if target_norm == candidate_norm:
        score += 60.0
    elif target_norm in candidate_norm or candidate_norm in target_norm:
        score += 25.0

    if target_numbers:
        if common_numbers == 0:
            score -= 40.0
        else:
            score += 18.0 * common_numbers

    if "DIA" in target_norm and "DIA" in candidate_norm:
        score += 10.0
    if "PM" in target_norm and "PM" in candidate_norm:
        score += 10.0
    if "DEG" in target_norm and "DEG" in candidate_norm:
        score += 8.0

    length_gap = abs(len(candidate_norm) - len(target_norm))
    score -= min(20.0, length_gap * 0.8)

    if target_bbox:
        score += bbox_iou(target_bbox, candidate["bbox"]) * 25.0
        distance = center_distance(target_bbox, candidate["bbox"])
        score -= min(25.0, distance / 120.0)

    return score


def iter_dimension_items(result):
    for view in result.get("views", []):
        for dimension in view.get("dimensions", []):
            yield {
                "dimension": dimension,
                "view_id": view.get("view_id"),
                "view_bbox": view.get("view_bbox"),
            }
    for dimension in result.get("unassigned", {}).get("dimensions", []):
        yield {
            "dimension": dimension,
            "view_id": "unassigned",
            "view_bbox": None,
        }


def _bboxes_match(box_a, box_b, max_center_distance=170.0):
    if not box_a or not box_b:
        return False
    return bbox_iou(box_a, box_b) >= 0.2 or center_distance(box_a, box_b) <= max_center_distance


def recover_missing_repeated_dimensions(result, word_entries, page_width, page_height):
    recovered_count = 0
    used_word_keys = set()

    for view in result.get("views", []):
        view_bbox = view.get("view_bbox")
        dimensions = view.get("dimensions", [])
        prototypes_by_text = defaultdict(list)
        for dimension in dimensions:
            text = normalize_match_text(dimension.get("raw_text", ""))
            if text:
                prototypes_by_text[text].append(dimension)

        for normalized_text, prototypes in prototypes_by_text.items():
            prototype = prototypes[0]
            raw_text = prototype.get("raw_text", "")
            candidates = collect_view_word_cluster_candidates(
                raw_text,
                view_bbox,
                word_entries,
                used_word_keys=used_word_keys,
            )
            if len(candidates) <= len(prototypes):
                continue

            existing_boxes = [dimension.get("bbox") for dimension in prototypes if dimension.get("bbox")]
            next_index = len(dimensions) + 1
            for candidate in candidates:
                if candidate.get("score", 0.0) < 86.0:
                    continue
                candidate_bbox = candidate.get("bbox")
                if not candidate_bbox:
                    continue
                if any(_bboxes_match(existing_bbox, candidate_bbox) for existing_bbox in existing_boxes):
                    continue
                clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(candidate_bbox, page_width, page_height)
                if clamped_bbox is None:
                    continue
                recovered = dict(prototype)
                recovered["bbox"] = clamped_bbox
                recovered["bbox_source"] = "pdf_text_layer_recovery"
                recovered["bbox_refined"] = True
                recovered["annotation_source"] = "pdf_text_layer"
                recovered["annotation_ready"] = True
                recovered["dimension_id"] = "{}_r{}".format(view.get("view_id", "view"), next_index)
                if was_clamped:
                    recovered["bbox_warning"] = "clamped_to_page_bounds"
                else:
                    recovered.pop("bbox_warning", None)
                dimensions.append(recovered)
                existing_boxes.append(clamped_bbox)
                used_word_keys.update(candidate.get("word_keys", []))
                next_index += 1
                recovered_count += 1
                if len(existing_boxes) >= len(candidates):
                    break

    return recovered_count


def refine_result_bboxes_from_pdf_text(result, source_pdf_path, pdf_page=1):
    native_page = extract_native_pdf_page(source_pdf_path, pdf_page)
    if not native_page:
        return result, {
            "status": "skipped",
            "reason": "source is not a PDF",
            "dimensions_refined": 0,
        }

    render_dpi = int(result.get("render_dpi") or 450)
    page_width = int(result.get("page_image_width_px") or 0)
    page_height = int(result.get("page_image_height_px") or 0)
    word_entries = build_word_entries(native_page, render_dpi)
    candidates = build_dimension_candidates(word_entries)
    used = set()
    used_word_keys = set()
    refined_count = 0

    dimension_records = list(iter_dimension_items(result))
    text_frequencies = defaultdict(int)
    for record in dimension_records:
        text_frequencies[normalize_match_text(record["dimension"].get("raw_text", ""))] += 1

    dimensions = sorted(
        dimension_records,
        key=lambda item: len(normalize_match_text(item["dimension"].get("raw_text", ""))),
        reverse=True,
    )
    for record in dimensions:
        dimension = record["dimension"]
        view_bbox = record.get("view_bbox")
        raw_text_norm = normalize_match_text(dimension.get("raw_text", ""))
        duplicate_text_count = text_frequencies.get(raw_text_norm, 0)
        current_bbox = dimension.get("bbox")
        seed_bbox = current_bbox[:] if isinstance(current_bbox, list) and len(current_bbox) == 4 else None
        if current_bbox:
            clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(current_bbox, page_width, page_height)
            if clamped_bbox is not None:
                dimension["bbox"] = clamped_bbox
                seed_bbox = clamped_bbox[:]
                if "bbox_before_pdf_text_refinement" not in dimension:
                    dimension["bbox_before_pdf_text_refinement"] = clamped_bbox[:]
                if was_clamped:
                    dimension["bbox_warning"] = "clamped_to_page_bounds"
                else:
                    dimension.pop("bbox_warning", None)
        elif "bbox_before_pdf_text_refinement" in dimension:
            seed_bbox = dimension.get("bbox_before_pdf_text_refinement")

        suspicious_seed = is_suspicious_seed_bbox(seed_bbox, page_width, page_height)

        local_candidate = select_local_word_cluster_candidate(
            dimension.get("raw_text", ""),
            seed_bbox,
            word_entries,
            used_word_keys=used_word_keys,
        )
        local_threshold = 86.0 if duplicate_text_count > 1 else 74.0
        if local_candidate and not suspicious_seed and local_candidate["score"] >= local_threshold:
            clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(local_candidate["bbox"], page_width, page_height)
            if clamped_bbox is not None:
                dimension["bbox"] = clamped_bbox
                dimension["bbox_source"] = "pdf_text_layer"
                dimension["bbox_refined"] = True
                dimension["annotation_source"] = "pdf_text_layer"
                dimension["annotation_ready"] = True
                if was_clamped:
                    dimension["bbox_warning"] = "clamped_to_page_bounds"
                else:
                    dimension.pop("bbox_warning", None)
                refined_count += 1
                used_word_keys.update(local_candidate.get("word_keys", []))
                continue

        view_cluster_matched = False
        if view_bbox:
            view_cluster_candidates = collect_view_word_cluster_candidates(
                dimension.get("raw_text", ""),
                view_bbox,
                word_entries,
                used_word_keys=used_word_keys,
            )
            view_threshold = 84.0 if duplicate_text_count > 1 else 72.0
            for candidate in view_cluster_candidates:
                if candidate["score"] < view_threshold:
                    continue
                clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(candidate["bbox"], page_width, page_height)
                if clamped_bbox is None:
                    continue
                dimension["bbox"] = clamped_bbox
                dimension["bbox_source"] = "pdf_text_layer"
                dimension["bbox_refined"] = True
                dimension["annotation_source"] = "pdf_text_layer"
                dimension["annotation_ready"] = True
                if was_clamped:
                    dimension["bbox_warning"] = "clamped_to_page_bounds"
                else:
                    dimension.pop("bbox_warning", None)
                refined_count += 1
                used_word_keys.update(candidate.get("word_keys", []))
                view_cluster_matched = True
                break
            if view_cluster_matched:
                continue

        global_cluster_candidates = collect_view_word_cluster_candidates(
            dimension.get("raw_text", ""),
            [0, 0, page_width, page_height],
            word_entries,
            used_word_keys=used_word_keys,
        )
        global_threshold = 84.0 if duplicate_text_count > 1 else 72.0
        for candidate in global_cluster_candidates:
            if candidate["score"] < global_threshold:
                continue
            clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(candidate["bbox"], page_width, page_height)
            if clamped_bbox is None:
                continue
            dimension["bbox"] = clamped_bbox
            dimension["bbox_source"] = "pdf_text_layer"
            dimension["bbox_refined"] = True
            dimension["annotation_source"] = "pdf_text_layer"
            dimension["annotation_ready"] = True
            if was_clamped:
                dimension["bbox_warning"] = "clamped_to_page_bounds"
            else:
                dimension.pop("bbox_warning", None)
            refined_count += 1
            used_word_keys.update(candidate.get("word_keys", []))
            view_cluster_matched = True
            break
        if view_cluster_matched:
            continue

        scored = []
        for index, candidate in enumerate(candidates):
            candidate_bbox = candidate["bbox"]
            if view_bbox and not seed_bbox and not center_within_view(candidate_bbox, view_bbox, padding=240):
                continue

            current_width, current_height = bbox_size(seed_bbox or dimension.get("bbox"))
            seed_diagonal = bbox_diagonal(seed_bbox or dimension.get("bbox"))
            candidate_width, candidate_height = bbox_size(candidate_bbox)
            if current_width and candidate_width > max(900, int(current_width * 2.6)):
                continue
            if current_height and candidate_height > max(320, int(current_height * 2.6)):
                continue
            if duplicate_text_count > 1 and candidate_width > 700:
                continue
            if seed_bbox:
                max_distance = max(900.0, seed_diagonal * (2.15 if duplicate_text_count > 1 else 3.0))
                if center_distance(seed_bbox, candidate_bbox) > max_distance:
                    continue

            score = score_text_candidate(dimension.get("raw_text", ""), seed_bbox, candidate)
            threshold = 78.0 if duplicate_text_count > 1 else 70.0
            if score >= threshold:
                scored.append((score, index, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)

        matched = False
        for _, index, candidate in scored:
            if index in used and bbox_iou(dimension.get("bbox"), candidate["bbox"]) < 0.4:
                continue
            clamped_bbox, was_clamped = clamp_bbox_to_page_bounds(candidate["bbox"], page_width, page_height)
            if clamped_bbox is None:
                continue
            dimension["bbox"] = clamped_bbox
            dimension["bbox_source"] = "pdf_text_layer"
            dimension["bbox_refined"] = True
            dimension["annotation_source"] = "pdf_text_layer"
            dimension["annotation_ready"] = True
            if was_clamped:
                dimension["bbox_warning"] = "clamped_to_page_bounds"
            else:
                dimension.pop("bbox_warning", None)
            refined_count += 1
            used.add(index)
            used_word_keys.update(candidate.get("word_keys", []))
            matched = True
            break

        if not matched:
            dimension["bbox_source"] = dimension.get("bbox_source") or "openai_vision"
            dimension["bbox_refined"] = False
            if dimension.get("bbox"):
                dimension["annotation_ready"] = True

    added_count = recover_missing_repeated_dimensions(result, word_entries, page_width, page_height)

    metadata = {
        "status": "refined" if refined_count else "no_matches",
        "engine": "pymupdf_text_layer",
        "dimensions_refined": refined_count,
        "dimensions_added": added_count,
        "candidate_count": len(candidates),
        "pdf_page": int(pdf_page),
        "render_dpi": render_dpi,
    }
    result["pdf_text_bbox_refinement"] = metadata
    return result, metadata
