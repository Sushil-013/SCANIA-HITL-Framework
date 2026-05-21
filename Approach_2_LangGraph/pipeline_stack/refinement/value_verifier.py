"""
Pipeline stage 05:
value_verifier.py - verifies extracted dimension values against drawing OCR
and separately checks whether the current bbox is on the matched text.
"""

from copy import deepcopy

try:
    from .bbox_refiner import bbox_iou, rect_from_bbox, run_craft_text_localizer, similarity_score
except ImportError:
    from bbox_refiner import bbox_iou, rect_from_bbox, run_craft_text_localizer, similarity_score


def _bbox_contains(outer_bbox, inner_bbox):
    outer = rect_from_bbox(outer_bbox)
    inner = rect_from_bbox(inner_bbox)
    if not outer or not inner:
        return False
    ox1, oy1, ox2, oy2 = outer
    ix1, iy1, ix2, iy2 = inner
    return ox1 <= ix1 and oy1 <= iy1 and ox2 >= ix2 and oy2 >= iy2


def _rect_center(rect):
    rect = rect_from_bbox(rect)
    if not rect:
        return None
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def _rect_center_in_bbox(rect, bbox, padding=80):
    rect = rect_from_bbox(rect)
    bbox = rect_from_bbox(bbox)
    if not rect or not bbox:
        return False
    cx, cy = _rect_center(rect)
    return (
        bbox[0] - padding <= cx <= bbox[2] + padding
        and bbox[1] - padding <= cy <= bbox[3] + padding
    )


def _minimum_similarity_threshold(text):
    compact = "".join(ch for ch in str(text or "") if ch.isalnum())
    length = len(compact)
    if length <= 2:
        return 1.0
    if length <= 4:
        return 0.92
    if length <= 6:
        return 0.82
    return 0.68


def _build_text_matches(target_text, entries):
    threshold = _minimum_similarity_threshold(target_text)
    matches = []
    for entry in entries:
        score = similarity_score(target_text, entry.get("normalized_text") or entry.get("text"))
        if score < threshold:
            continue
        matches.append(
            {
                "text": entry.get("text", ""),
                "rect": rect_from_bbox(entry.get("rect")),
                "score": float(score),
                "source_ids": list(entry.get("source_ids", [])),
            }
        )
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches, threshold


def _select_matches_for_view(matches, view_bbox):
    if not view_bbox:
        return matches, "drawing"
    view_matches = [match for match in matches if _rect_center_in_bbox(match.get("rect"), view_bbox)]
    if view_matches:
        return view_matches, "view"
    return matches, "drawing"


def _choose_location_match(matches, current_bbox):
    current_rect = rect_from_bbox(current_bbox)
    if not current_rect:
        return None, False, 0.0

    best_overlap = None
    best_overlap_score = 0.0
    for match in matches:
        match_rect = rect_from_bbox(match.get("rect"))
        if not match_rect:
            continue
        overlap = bbox_iou(current_rect, match_rect)
        contains = _bbox_contains(current_rect, match_rect) or _bbox_contains(match_rect, current_rect)
        if contains:
            overlap = max(overlap, 1.0)
        if overlap > best_overlap_score:
            best_overlap = match
            best_overlap_score = overlap

    return best_overlap, best_overlap_score >= 0.35, best_overlap_score


def _iter_dimensions(result):
    for view in result.get("views", []):
        yield {
            "view_id": view.get("view_id", "VIEW"),
            "view_type": view.get("view_type", "unknown"),
            "view_bbox": view.get("view_bbox"),
            "dimensions": view.get("dimensions", []),
            "is_unassigned": False,
        }

    unassigned = result.get("unassigned", {})
    yield {
        "view_id": "unassigned",
        "view_type": "unassigned",
        "view_bbox": None,
        "dimensions": unassigned.get("dimensions", []),
        "is_unassigned": True,
    }


def verify_dimension_values(
    result,
    image_path,
    pdf_page=1,
    pdf_dpi=450,
    glm_model_name="zai-org/GLM-OCR",
    max_new_tokens=512,
    use_cuda=True,
    preprocess=True,
):
    verified_result = deepcopy(result)
    metadata = {
        "status": "verified",
        "engine": "craft_glmocr_text_match",
        "total_dimensions": 0,
        "verified_value_count": 0,
        "verified_location_count": 0,
        "bbox_mismatch_count": 0,
        "not_found_in_drawing_count": 0,
        "no_text_count": 0,
        "needs_manual_review": False,
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
        metadata["status"] = "unverified"
        metadata["reason"] = str(exc)
        metadata["needs_manual_review"] = True
        verified_result["value_verification"] = metadata
        return verified_result, metadata

    merged_entries = localization.get("merged_entries", [])

    for group in _iter_dimensions(verified_result):
        view_bbox = group.get("view_bbox")
        for dimension in group.get("dimensions", []):
            raw_text = dimension.get("raw_text", "")
            metadata["total_dimensions"] += 1

            if not str(raw_text or "").strip():
                verification = {
                    "value_verified": False,
                    "location_verified": False,
                    "overall_status": "no_text",
                    "match_scope": "none",
                    "matched_text": None,
                    "matched_bbox": None,
                    "match_score": 0.0,
                    "location_iou": 0.0,
                }
                dimension["value_verification"] = verification
                metadata["no_text_count"] += 1
                continue

            matches, threshold = _build_text_matches(raw_text, merged_entries)
            scoped_matches, match_scope = _select_matches_for_view(matches, view_bbox)

            best_match = scoped_matches[0] if scoped_matches else None
            location_match, location_verified, location_iou = _choose_location_match(scoped_matches, dimension.get("bbox"))
            selected_match = location_match or best_match
            value_verified = bool(scoped_matches)

            if value_verified and location_verified:
                overall_status = "verified"
            elif value_verified:
                overall_status = "value_correct_bbox_wrong"
            else:
                overall_status = "not_found_in_drawing"

            verification = {
                "value_verified": value_verified,
                "location_verified": location_verified,
                "overall_status": overall_status,
                "match_scope": match_scope if value_verified else "none",
                "matched_text": selected_match.get("text") if selected_match else None,
                "matched_bbox": rect_from_bbox(selected_match.get("rect")) if selected_match else None,
                "match_score": round(float(selected_match.get("score", 0.0)), 4) if selected_match else 0.0,
                "location_iou": round(float(location_iou), 4),
                "match_threshold": threshold,
            }
            dimension["value_verification"] = verification

            if value_verified:
                metadata["verified_value_count"] += 1
            else:
                metadata["not_found_in_drawing_count"] += 1

            if location_verified:
                metadata["verified_location_count"] += 1
            elif value_verified:
                metadata["bbox_mismatch_count"] += 1

    if metadata["bbox_mismatch_count"] > 0 or metadata["not_found_in_drawing_count"] > 0 or metadata["no_text_count"] > 0:
        metadata["status"] = "partial"
        metadata["needs_manual_review"] = True

    verified_result["value_verification"] = metadata
    return verified_result, metadata
