"""
Pipeline stage 04:
dimension_verifier.py - verifies and counts green-box dimensions.
"""

def build_dimension_verification(result):
    verification = {
        "status": "verified",
        "extracted_dimension_count": 0,
        "green_box_dimension_count": 0,
        "missing_green_box_dimension_count": 0,
        "views": [],
        "unassigned": {
            "extracted_dimension_count": 0,
            "green_box_dimension_count": 0,
            "missing_green_box_dimension_count": 0,
            "dimensions": [],
        },
    }

    dimension_counter = 1

    for view in result.get("views", []):
        view_summary = {
            "view_id": view.get("view_id", "VIEW"),
            "view_type": view.get("view_type", "unknown"),
            "extracted_dimension_count": 0,
            "green_box_dimension_count": 0,
            "missing_green_box_dimension_count": 0,
            "dimensions": [],
        }
        for dimension in view.get("dimensions", []):
            annotation_ready = bool(dimension.get("annotation_ready"))
            annotation_label = "D{}".format(dimension_counter) if annotation_ready else None
            view_summary["dimensions"].append(
                {
                    "dimension_id": dimension.get("dimension_id"),
                    "raw_text": dimension.get("raw_text"),
                    "dimension_type": dimension.get("dimension_type"),
                    "bbox": dimension.get("bbox"),
                    "annotation_ready": annotation_ready,
                    "annotation_source": dimension.get("annotation_source"),
                    "annotation_label": annotation_label,
                }
            )
            view_summary["extracted_dimension_count"] += 1
            verification["extracted_dimension_count"] += 1
            if annotation_ready:
                view_summary["green_box_dimension_count"] += 1
                verification["green_box_dimension_count"] += 1
                dimension_counter += 1
        view_summary["missing_green_box_dimension_count"] = (
            view_summary["extracted_dimension_count"] - view_summary["green_box_dimension_count"]
        )
        verification["views"].append(view_summary)

    for dimension in result.get("unassigned", {}).get("dimensions", []):
        annotation_ready = bool(dimension.get("annotation_ready"))
        annotation_label = "D{}".format(dimension_counter) if annotation_ready else None
        verification["unassigned"]["dimensions"].append(
            {
                "dimension_id": dimension.get("dimension_id"),
                "raw_text": dimension.get("raw_text"),
                "dimension_type": dimension.get("dimension_type"),
                "bbox": dimension.get("bbox"),
                "annotation_ready": annotation_ready,
                "annotation_source": dimension.get("annotation_source"),
                "annotation_label": annotation_label,
            }
        )
        verification["unassigned"]["extracted_dimension_count"] += 1
        verification["extracted_dimension_count"] += 1
        if annotation_ready:
            verification["unassigned"]["green_box_dimension_count"] += 1
            verification["green_box_dimension_count"] += 1
            dimension_counter += 1

    verification["unassigned"]["missing_green_box_dimension_count"] = (
        verification["unassigned"]["extracted_dimension_count"] - verification["unassigned"]["green_box_dimension_count"]
    )
    verification["missing_green_box_dimension_count"] = (
        verification["extracted_dimension_count"] - verification["green_box_dimension_count"]
    )
    if verification["missing_green_box_dimension_count"] > 0:
        verification["status"] = "partial"

    return verification


def count_green_box_dimensions(result):
    return build_dimension_verification(result).get("green_box_dimension_count", 0)
