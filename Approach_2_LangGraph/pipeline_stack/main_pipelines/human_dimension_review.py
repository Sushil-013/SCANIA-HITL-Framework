import os
import re
import sqlite3
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

try:
    from ..helpers.sqlite_importer import create_tables, migrate_tables, has_tolerance_data
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.sqlite_importer import create_tables, migrate_tables, has_tolerance_data

try:
    from ..helpers.tk_ui_sizing import (
        STANDARD_REVIEW_PREVIEW_HEIGHT,
        STANDARD_REVIEW_PREVIEW_WIDTH,
        apply_standard_window,
    )
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.tk_ui_sizing import (
        STANDARD_REVIEW_PREVIEW_HEIGHT,
        STANDARD_REVIEW_PREVIEW_WIDTH,
        apply_standard_window,
    )


REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_PENDING = "pending"
REVIEW_RERUN_REQUESTED = "rerun_requested"
MANUAL_DIMENSION_STATUS = "manual_added"


def _default_post_review_actions():
    return {
        "run_layer2_extract": True,
        "run_layer2_link": True,
        "run_layer3": True,
    }


def _safe_console_text(text):
    resolved = str(text or "")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        resolved.encode(encoding)
        return resolved
    except Exception:
        return resolved.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _rect_from_bbox(bbox):
    if not bbox:
        return None
    if len(bbox) == 4:
        return [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
    xs = bbox[0::2]
    ys = bbox[1::2]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _build_dimension_records(result):
    records = []
    dimension_counter = 1

    for view in result.get("views", []):
        for dimension in view.get("dimensions", []):
            records.append(
                {
                    "dimension_id": "d{}".format(dimension_counter),
                    "view_id": view.get("view_id"),
                    "view_type": view.get("view_type"),
                    "is_unassigned": False,
                    "dimension": deepcopy(dimension),
                }
            )
            dimension_counter += 1

    for dimension in result.get("unassigned", {}).get("dimensions", []):
        records.append(
            {
                "dimension_id": "d{}".format(dimension_counter),
                "view_id": None,
                "view_type": "unassigned",
                "is_unassigned": True,
                "dimension": deepcopy(dimension),
            }
        )
        dimension_counter += 1

    return records


def _requires_manual_review(record):
    dimension = record["dimension"]
    verification = dimension.get("value_verification") or {}
    if not dimension.get("annotation_ready"):
        return True
    if verification.get("overall_status") not in (None, "", "verified"):
        return True
    return False


def _format_tolerance_text(dimension):
    nominal = dimension.get("nominal_value")
    plus_value = dimension.get("tolerance_plus")
    minus_value = dimension.get("tolerance_minus")
    if nominal is None:
        return "Nominal: missing"
    if plus_value is None and minus_value is None:
        return "Nominal: {}".format(nominal)
    return "Nominal: {} | +{} / {}".format(nominal, plus_value, minus_value)


def _parse_optional_float(text):
    value = str(text or "").strip().replace(",", ".")
    if not value:
        return None
    return float(value)


def _infer_dimension_type(raw_text):
    text = str(raw_text or "").strip().lower()
    if "r" in text and "sr" not in text and ("radius" in text or text.startswith("r") or " r" in text):
        return "radius"
    if "°" in text or "deg" in text:
        return "angle"
    if "ø" in text or "⌀" in text or "diam" in text or text.startswith("phi"):
        return "diameter"
    return "linear"


def _infer_tolerance_fields(nominal_value, tolerance_plus, tolerance_minus):
    if tolerance_plus is None and tolerance_minus is None:
        return "none", "none", "none"
    if tolerance_plus is not None and tolerance_minus is not None:
        if abs(tolerance_plus) == abs(tolerance_minus):
            return "manual", "plus_minus", "symmetric"
        return "manual", "plus_minus", "bilateral"
    if tolerance_plus is not None:
        return "manual", "unilateral", "plus_only"
    return "manual", "unilateral", "minus_only"


def _build_manual_raw_text(nominal_value, tolerance_plus, tolerance_minus):
    if nominal_value is None:
        return ""
    nominal_text = "{:g}".format(float(nominal_value))
    if tolerance_plus is None and tolerance_minus is None:
        return nominal_text
    if tolerance_plus is not None and tolerance_minus is not None and abs(tolerance_plus) == abs(tolerance_minus):
        return "{} +/-{:g}".format(nominal_text, abs(float(tolerance_plus)))
    plus_text = "" if tolerance_plus is None else "+{:g}".format(float(tolerance_plus))
    minus_text = "" if tolerance_minus is None else "{:g}".format(float(tolerance_minus))
    combined = " ".join(piece for piece in (nominal_text, plus_text, minus_text) if piece)
    return combined.strip()


def _load_document_identity(db_path, document_id):
    conn = sqlite3.connect(str(Path(db_path).resolve()))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT document_id, part_number, document_number
            FROM documents
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Document {} was not found in the review database.".format(document_id))
        return dict(row)
    finally:
        conn.close()


def _suggest_next_dimension_id(db_path, document_id):
    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        rows = conn.execute(
            """
            SELECT dimension_id FROM dimensions WHERE document_id = ?
            UNION
            SELECT dimension_id FROM manual_dimensions WHERE document_id = ?
            """,
            (document_id, document_id),
        ).fetchall()
    finally:
        conn.close()

    max_number = 0
    for (dimension_id,) in rows:
        match = re.fullmatch(r"[dD](\d+)", str(dimension_id or "").strip())
        if match:
            max_number = max(max_number, int(match.group(1)))
    return "D{}".format(max_number + 1 if max_number >= 0 else 1)


def _dimension_id_exists_case_insensitive(conn, document_id, dimension_id):
    row = conn.execute(
        """
        SELECT 1
        FROM dimensions
        WHERE document_id = ? AND LOWER(dimension_id) = LOWER(?)
        UNION
        SELECT 1
        FROM manual_dimensions
        WHERE document_id = ? AND LOWER(dimension_id) = LOWER(?)
        LIMIT 1
        """,
        (document_id, dimension_id, document_id, dimension_id),
    ).fetchone()
    return row is not None


def _build_manual_record(row):
    dimension = {
        "raw_text": row["raw_text"],
        "dimension_type": row["dimension_type"],
        "nominal_value": row["nominal_value"],
        "tolerance_class": row["tolerance_class"],
        "tolerance_format": row["tolerance_format"],
        "tolerance_pattern": row["tolerance_pattern"],
        "tolerance_plus": row["tolerance_plus"],
        "tolerance_minus": row["tolerance_minus"],
        "status": row["status"],
        "annotation_ready": False,
        "bbox": None,
        "value_verification": {"overall_status": "manual_entry"},
    }
    return {
        "dimension_id": row["dimension_id"],
        "view_id": row["view_id"],
        "view_type": row["view_id"] or "manual",
        "is_unassigned": bool(row["is_unassigned"]),
        "dimension": dimension,
        "requires_manual_review": False,
        "human_review_status": row["human_review_status"] or REVIEW_APPROVED,
        "is_manual_addition": True,
    }


def _load_manual_dimension_records(db_path, document_id):
    conn = sqlite3.connect(str(Path(db_path).resolve()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                document_id,
                part_number,
                view_id,
                dimension_id,
                raw_text,
                dimension_type,
                nominal_value,
                tolerance_class,
                tolerance_format,
                tolerance_pattern,
                tolerance_plus,
                tolerance_minus,
                status,
                is_unassigned,
                human_review_status
            FROM manual_dimensions
            WHERE document_id = ?
            ORDER BY
                CASE
                    WHEN LOWER(dimension_id) GLOB 'd[0-9]*' THEN CAST(SUBSTR(LOWER(dimension_id), 2) AS INTEGER)
                    ELSE 999999
                END,
                dimension_id
            """,
            (document_id,),
        ).fetchall()
        return [_build_manual_record(row) for row in rows]
    finally:
        conn.close()


def save_manual_dimension(
    db_path,
    document_id,
    dimension_id=None,
    raw_text=None,
    nominal_value=None,
    tolerance_plus=None,
    tolerance_minus=None,
    tolerance_symmetric=None,
    view_id=None,
    dimension_type=None,
    notes=None,
):
    document_identity = _load_document_identity(db_path, document_id)
    resolved_dimension_id = str(dimension_id or "").strip() or _suggest_next_dimension_id(db_path, document_id)
    resolved_view_id = str(view_id or "").strip() or None

    resolved_nominal_value = _parse_optional_float(nominal_value)
    resolved_tolerance_plus = _parse_optional_float(tolerance_plus)
    resolved_tolerance_minus = _parse_optional_float(tolerance_minus)
    resolved_symmetric = _parse_optional_float(tolerance_symmetric)
    if resolved_symmetric is not None:
        if resolved_tolerance_plus is None:
            resolved_tolerance_plus = abs(resolved_symmetric)
        if resolved_tolerance_minus is None:
            resolved_tolerance_minus = -abs(resolved_symmetric)

    resolved_raw_text = str(raw_text or "").strip()
    if not resolved_raw_text:
        resolved_raw_text = _build_manual_raw_text(
            resolved_nominal_value,
            resolved_tolerance_plus,
            resolved_tolerance_minus,
        )
    if not resolved_raw_text:
        raise RuntimeError("Manual dimension requires raw text or a nominal value.")

    resolved_dimension_type = str(dimension_type or "").strip().lower() or _infer_dimension_type(resolved_raw_text)
    tolerance_class, tolerance_format, tolerance_pattern = _infer_tolerance_fields(
        resolved_nominal_value,
        resolved_tolerance_plus,
        resolved_tolerance_minus,
    )
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_notes = str(notes or "Manually added during human review.").strip()

    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        create_tables(conn)
        migrate_tables(conn)
        if _dimension_id_exists_case_insensitive(conn, document_id, resolved_dimension_id):
            raise RuntimeError(
                "Dimension id {} already exists for document {}.".format(resolved_dimension_id, document_id)
            )

        manual_row = (
            document_id,
            document_identity["part_number"],
            resolved_view_id,
            resolved_dimension_id,
            resolved_raw_text,
            resolved_dimension_type,
            resolved_nominal_value,
            tolerance_class,
            tolerance_format,
            tolerance_pattern,
            resolved_tolerance_plus,
            resolved_tolerance_minus,
            MANUAL_DIMENSION_STATUS,
            0 if resolved_view_id else 1,
            REVIEW_APPROVED,
            reviewed_at,
            review_notes,
            reviewed_at,
        )

        conn.execute(
            """
            INSERT INTO manual_dimensions (
                document_id, part_number, view_id, dimension_id, raw_text, dimension_type,
                nominal_value, tolerance_class, tolerance_format, tolerance_pattern,
                tolerance_plus, tolerance_minus, status, is_unassigned,
                human_review_status, human_reviewed_at, human_review_notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            manual_row,
        )
        conn.execute(
            """
            INSERT INTO dimensions (
                document_id, part_number, view_id, dimension_id, raw_text, dimension_type,
                nominal_value, tolerance_class, tolerance_format, tolerance_pattern,
                tolerance_plus, tolerance_minus, status, is_unassigned,
                human_review_status, human_reviewed_at, human_review_notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            manual_row[:-1],
        )

        manual_dimension = {
            "raw_text": resolved_raw_text,
            "nominal_value": resolved_nominal_value,
            "tolerance_class": tolerance_class,
            "tolerance_format": tolerance_format,
            "tolerance_pattern": tolerance_pattern,
            "tolerance_plus": resolved_tolerance_plus,
            "tolerance_minus": resolved_tolerance_minus,
            "status": MANUAL_DIMENSION_STATUS,
        }
        if has_tolerance_data(manual_dimension):
            conn.execute(
                """
                INSERT INTO tolerances (
                    document_id, part_number, view_id, dimension_id, raw_text, nominal_value,
                    tolerance_class, tolerance_format, tolerance_pattern,
                    tolerance_plus, tolerance_minus, status, is_unassigned
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    document_identity["part_number"],
                    resolved_view_id,
                    resolved_dimension_id,
                    resolved_raw_text,
                    resolved_nominal_value,
                    tolerance_class,
                    tolerance_format,
                    tolerance_pattern,
                    resolved_tolerance_plus,
                    resolved_tolerance_minus,
                    MANUAL_DIMENSION_STATUS,
                    0 if resolved_view_id else 1,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "dimension_id": resolved_dimension_id,
        "view_id": resolved_view_id,
        "view_type": resolved_view_id or "manual",
        "is_unassigned": resolved_view_id is None,
        "dimension": {
            "raw_text": resolved_raw_text,
            "dimension_type": resolved_dimension_type,
            "nominal_value": resolved_nominal_value,
            "tolerance_class": tolerance_class,
            "tolerance_format": tolerance_format,
            "tolerance_pattern": tolerance_pattern,
            "tolerance_plus": resolved_tolerance_plus,
            "tolerance_minus": resolved_tolerance_minus,
            "status": MANUAL_DIMENSION_STATUS,
            "annotation_ready": False,
            "bbox": None,
            "value_verification": {"overall_status": "manual_entry"},
        },
        "requires_manual_review": False,
        "human_review_status": REVIEW_APPROVED,
        "is_manual_addition": True,
    }


def update_dimension_record(
    db_path,
    document_id,
    dimension_id,
    raw_text=None,
    nominal_value=None,
    tolerance_plus=None,
    tolerance_minus=None,
    tolerance_symmetric=None,
    view_id=None,
    dimension_type=None,
    notes=None,
):
    document_identity = _load_document_identity(db_path, document_id)
    resolved_dimension_id = str(dimension_id or "").strip()
    if not resolved_dimension_id:
        raise RuntimeError("A dimension id is required for update.")

    resolved_view_id = str(view_id or "").strip() or None
    resolved_nominal_value = _parse_optional_float(nominal_value)
    resolved_tolerance_plus = _parse_optional_float(tolerance_plus)
    resolved_tolerance_minus = _parse_optional_float(tolerance_minus)
    resolved_symmetric = _parse_optional_float(tolerance_symmetric)
    if resolved_symmetric is not None:
        if resolved_tolerance_plus is None:
            resolved_tolerance_plus = abs(resolved_symmetric)
        if resolved_tolerance_minus is None:
            resolved_tolerance_minus = -abs(resolved_symmetric)

    resolved_raw_text = str(raw_text or "").strip()
    if not resolved_raw_text:
        resolved_raw_text = _build_manual_raw_text(
            resolved_nominal_value,
            resolved_tolerance_plus,
            resolved_tolerance_minus,
        )
    if not resolved_raw_text:
        raise RuntimeError("Dimension update requires raw text or a nominal value.")

    resolved_dimension_type = str(dimension_type or "").strip().lower() or _infer_dimension_type(resolved_raw_text)
    tolerance_class, tolerance_format, tolerance_pattern = _infer_tolerance_fields(
        resolved_nominal_value,
        resolved_tolerance_plus,
        resolved_tolerance_minus,
    )
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_notes = str(notes or "Edited during human review.").strip()

    conn = sqlite3.connect(str(Path(db_path).resolve()))
    conn.row_factory = sqlite3.Row
    try:
        create_tables(conn)
        migrate_tables(conn)
        existing_row = conn.execute(
            """
            SELECT
                d.status,
                d.human_review_status,
                EXISTS(
                    SELECT 1
                    FROM manual_dimensions AS m
                    WHERE m.document_id = d.document_id AND m.dimension_id = d.dimension_id
                ) AS is_manual_addition
            FROM dimensions AS d
            WHERE d.document_id = ? AND d.dimension_id = ?
            """,
            (document_id, resolved_dimension_id),
        ).fetchone()
        if existing_row is None:
            raise RuntimeError(
                "Dimension {} was not found for document {}.".format(resolved_dimension_id, document_id)
            )

        resolved_status = existing_row["status"] or MANUAL_DIMENSION_STATUS
        resolved_review_status = existing_row["human_review_status"] or REVIEW_PENDING
        is_manual_addition = bool(existing_row["is_manual_addition"])
        is_unassigned = 0 if resolved_view_id else 1

        conn.execute(
            """
            UPDATE dimensions
            SET
                part_number = ?,
                view_id = ?,
                raw_text = ?,
                dimension_type = ?,
                nominal_value = ?,
                tolerance_class = ?,
                tolerance_format = ?,
                tolerance_pattern = ?,
                tolerance_plus = ?,
                tolerance_minus = ?,
                status = ?,
                is_unassigned = ?,
                human_review_status = ?,
                human_reviewed_at = ?,
                human_review_notes = ?
            WHERE document_id = ? AND dimension_id = ?
            """,
            (
                document_identity["part_number"],
                resolved_view_id,
                resolved_raw_text,
                resolved_dimension_type,
                resolved_nominal_value,
                tolerance_class,
                tolerance_format,
                tolerance_pattern,
                resolved_tolerance_plus,
                resolved_tolerance_minus,
                resolved_status,
                is_unassigned,
                resolved_review_status,
                reviewed_at,
                review_notes,
                document_id,
                resolved_dimension_id,
            ),
        )

        if is_manual_addition:
            conn.execute(
                """
                UPDATE manual_dimensions
                SET
                    part_number = ?,
                    view_id = ?,
                    raw_text = ?,
                    dimension_type = ?,
                    nominal_value = ?,
                    tolerance_class = ?,
                    tolerance_format = ?,
                    tolerance_pattern = ?,
                    tolerance_plus = ?,
                    tolerance_minus = ?,
                    status = ?,
                    is_unassigned = ?,
                    human_review_status = ?,
                    human_reviewed_at = ?,
                    human_review_notes = ?
                WHERE document_id = ? AND dimension_id = ?
                """,
                (
                    document_identity["part_number"],
                    resolved_view_id,
                    resolved_raw_text,
                    resolved_dimension_type,
                    resolved_nominal_value,
                    tolerance_class,
                    tolerance_format,
                    tolerance_pattern,
                    resolved_tolerance_plus,
                    resolved_tolerance_minus,
                    resolved_status,
                    is_unassigned,
                    resolved_review_status,
                    reviewed_at,
                    review_notes,
                    document_id,
                    resolved_dimension_id,
                ),
            )

        conn.execute(
            "DELETE FROM tolerances WHERE document_id = ? AND dimension_id = ?",
            (document_id, resolved_dimension_id),
        )
        updated_dimension = {
            "raw_text": resolved_raw_text,
            "nominal_value": resolved_nominal_value,
            "tolerance_class": tolerance_class,
            "tolerance_format": tolerance_format,
            "tolerance_pattern": tolerance_pattern,
            "tolerance_plus": resolved_tolerance_plus,
            "tolerance_minus": resolved_tolerance_minus,
            "status": resolved_status,
        }
        if has_tolerance_data(updated_dimension):
            conn.execute(
                """
                INSERT INTO tolerances (
                    document_id, part_number, view_id, dimension_id, raw_text, nominal_value,
                    tolerance_class, tolerance_format, tolerance_pattern,
                    tolerance_plus, tolerance_minus, status, is_unassigned
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    document_identity["part_number"],
                    resolved_view_id,
                    resolved_dimension_id,
                    resolved_raw_text,
                    resolved_nominal_value,
                    tolerance_class,
                    tolerance_format,
                    tolerance_pattern,
                    resolved_tolerance_plus,
                    resolved_tolerance_minus,
                    resolved_status,
                    is_unassigned,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "dimension_id": resolved_dimension_id,
        "view_id": resolved_view_id,
        "view_type": resolved_view_id or ("manual" if is_manual_addition else "unassigned"),
        "is_unassigned": resolved_view_id is None,
        "dimension": {
            "raw_text": resolved_raw_text,
            "dimension_type": resolved_dimension_type,
            "nominal_value": resolved_nominal_value,
            "tolerance_class": tolerance_class,
            "tolerance_format": tolerance_format,
            "tolerance_pattern": tolerance_pattern,
            "tolerance_plus": resolved_tolerance_plus,
            "tolerance_minus": resolved_tolerance_minus,
            "status": resolved_status,
        },
        "human_review_status": resolved_review_status,
        "is_manual_addition": is_manual_addition,
    }


def delete_dimension_record(db_path, document_id, dimension_id):
    resolved_dimension_id = str(dimension_id or "").strip()
    if not resolved_dimension_id:
        raise RuntimeError("A dimension id is required for delete.")

    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        create_tables(conn)
        migrate_tables(conn)
        exists_row = conn.execute(
            "SELECT 1 FROM dimensions WHERE document_id = ? AND dimension_id = ?",
            (document_id, resolved_dimension_id),
        ).fetchone()
        if exists_row is None:
            raise RuntimeError(
                "Dimension {} was not found for document {}.".format(resolved_dimension_id, document_id)
            )
        conn.execute("DELETE FROM catia_dimension_links WHERE document_id = ? AND dimension_id = ?", (document_id, resolved_dimension_id))
        conn.execute("DELETE FROM tolerances WHERE document_id = ? AND dimension_id = ?", (document_id, resolved_dimension_id))
        conn.execute("DELETE FROM manual_dimensions WHERE document_id = ? AND dimension_id = ?", (document_id, resolved_dimension_id))
        conn.execute("DELETE FROM dimensions WHERE document_id = ? AND dimension_id = ?", (document_id, resolved_dimension_id))
        conn.commit()
    finally:
        conn.close()


def _load_existing_review_statuses(db_path, document_id):
    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        rows = conn.execute(
            """
            SELECT dimension_id, human_review_status
            FROM dimensions
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        conn.close()


def save_dimension_review_decision(db_path, document_id, dimension_id, status, notes=None):
    if status not in (REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_PENDING):
        raise RuntimeError("Unsupported human review status: {}".format(status))

    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        conn.execute(
            """
            UPDATE dimensions
            SET
                human_review_status = ?,
                human_reviewed_at = ?,
                human_review_notes = ?
            WHERE document_id = ? AND dimension_id = ?
            """,
            (
                status,
                datetime.now(timezone.utc).isoformat(),
                notes,
                document_id,
                dimension_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def save_dimension_review_decisions_bulk(db_path, document_id, dimension_ids, status, notes=None):
    if status not in (REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_PENDING):
        raise RuntimeError("Unsupported human review status: {}".format(status))
    if not dimension_ids:
        return

    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """
            UPDATE dimensions
            SET
                human_review_status = ?,
                human_reviewed_at = ?,
                human_review_notes = ?
            WHERE document_id = ? AND dimension_id = ?
            """,
            [(status, reviewed_at, notes, document_id, dimension_id) for dimension_id in dimension_ids],
        )
        conn.commit()
    finally:
        conn.close()


def summarize_dimension_reviews(db_path, document_id):
    conn = sqlite3.connect(str(Path(db_path).resolve()))
    try:
        rows = conn.execute(
            """
            SELECT human_review_status, COUNT(*)
            FROM dimensions
            WHERE document_id = ?
            GROUP BY human_review_status
            """,
            (document_id,),
        ).fetchall()
        counts = {REVIEW_APPROVED: 0, REVIEW_REJECTED: 0, REVIEW_PENDING: 0}
        for status, count in rows:
            counts[status or REVIEW_PENDING] = int(count)
        counts["total"] = counts[REVIEW_APPROVED] + counts[REVIEW_REJECTED] + counts[REVIEW_PENDING]
        return counts
    finally:
        conn.close()


def _summarize_candidate_statuses(db_path, document_id, dimension_ids):
    status_map = _load_existing_review_statuses(db_path, document_id)
    counts = {REVIEW_APPROVED: 0, REVIEW_REJECTED: 0, REVIEW_PENDING: 0}
    for dimension_id in dimension_ids:
        status = status_map.get(dimension_id, REVIEW_PENDING)
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(dimension_ids)
    return counts


def _build_review_candidates(result, db_path, document_id, review_scope):
    records = _build_dimension_records(result)
    statuses = _load_existing_review_statuses(db_path, document_id)
    candidates = []
    seen_dimension_ids = set()
    for record in records:
        record["requires_manual_review"] = _requires_manual_review(record)
        record["human_review_status"] = statuses.get(record["dimension_id"], REVIEW_PENDING)
        record["is_manual_addition"] = False
        if review_scope == "flagged" and not record["requires_manual_review"]:
            continue
        candidates.append(record)
        seen_dimension_ids.add(str(record["dimension_id"]).lower())

    for record in _load_manual_dimension_records(db_path, document_id):
        dimension_key = str(record["dimension_id"]).lower()
        if dimension_key in seen_dimension_ids:
            continue
        candidates.append(record)
        seen_dimension_ids.add(dimension_key)
    return candidates


def _resolve_display_bbox(record):
    dimension = record.get("dimension") or {}
    verification = dimension.get("value_verification") or {}

    matched_bbox = _rect_from_bbox(verification.get("matched_bbox"))
    if matched_bbox and verification.get("value_verified"):
        return matched_bbox

    return _rect_from_bbox(dimension.get("bbox"))


def _coverage_outline(record):
    if record.get("human_review_status") == REVIEW_REJECTED:
        return (220, 38, 38), (220, 38, 38, 44)
    if record.get("requires_manual_review"):
        return (245, 158, 11), (245, 158, 11, 40)
    return (34, 197, 94), (34, 197, 94, 40)


def build_coverage_review_image(prepared_image_path, candidates, output_path):
    image = Image.open(prepared_image_path).convert("RGBA")
    try:
        overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        for record in candidates:
            rect = _resolve_display_bbox(record)
            if not rect:
                continue
            outline, fill = _coverage_outline(record)
            draw.rectangle(rect, outline=outline, fill=fill, width=3)
            label = str(record.get("dimension_id") or "").upper()
            text_bbox = draw.textbbox((0, 0), label)
            text_width = text_bbox[2] - text_bbox[0]
            text_height = text_bbox[3] - text_bbox[1]
            pad_x = 6
            pad_y = 3
            label_x1 = rect[0]
            label_y1 = max(0, rect[1] - text_height - (2 * pad_y) - 4)
            label_x2 = min(image.size[0] - 1, label_x1 + text_width + (2 * pad_x))
            label_y2 = min(image.size[1] - 1, label_y1 + text_height + (2 * pad_y))
            draw.rectangle([label_x1, label_y1, label_x2, label_y2], fill=outline)
            draw.text((label_x1 + pad_x, label_y1 + pad_y), label, fill=(255, 255, 255, 255))

        combined = Image.alpha_composite(image, overlay)
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        combined.save(output_path)
        return output_path
    finally:
        image.close()


def _coverage_summary_text(candidates, expected_dimension_count=None):
    extracted_count = len(candidates)
    lines = ["Extracted dimensions: {}".format(extracted_count)]
    if expected_dimension_count is not None:
        missing_estimate = max(0, int(expected_dimension_count) - extracted_count)
        lines.append("Expected dimensions: {}".format(int(expected_dimension_count)))
        lines.append("Possible missing dimensions: {}".format(missing_estimate))
    flagged_count = sum(1 for record in candidates if record.get("requires_manual_review"))
    manual_count = sum(1 for record in candidates if record.get("is_manual_addition"))
    lines.append("Flagged dimensions: {}".format(flagged_count))
    lines.append("Manual additions: {}".format(manual_count))
    lines.append("Coverage question: do the numbered boxes cover all real dimensions on the drawing?")
    return "\n".join(lines)


def _dimension_list_lines(candidates):
    lines = []
    for record in candidates:
        dimension = record.get("dimension") or {}
        verification = dimension.get("value_verification") or {}
        nominal = dimension.get("nominal_value")
        lines.append(
            "{id} | {text} | nominal={nominal} | status={status}".format(
                id=record.get("dimension_id") or "?",
                text=(dimension.get("raw_text") or "<empty>").replace("\n", " ").strip(),
                nominal="missing" if nominal is None else nominal,
                status=verification.get("overall_status", "unknown"),
            )
        )
    return lines


def _emit_dimension_log(candidates, progress_callback=None):
    lines = _dimension_list_lines(candidates)
    if progress_callback:
        progress_callback("Human review extracted dimensions:")
        for line in lines:
            progress_callback("  {}".format(line))
    else:
        print("")
        print("Extracted dimensions:")
        for line in lines:
            print("  {}".format(line))
    return lines


def _rect_center(rect):
    if not rect:
        return None
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def _scale_rect(rect, scale_x, scale_y):
    if not rect:
        return None
    return [
        int(round(rect[0] * scale_x)),
        int(round(rect[1] * scale_y)),
        int(round(rect[2] * scale_x)),
        int(round(rect[3] * scale_y)),
    ]


def _prompt_manual_dimension_terminal(db_path, document_id):
    print("")
    print("Add manual dimension")
    suggested_id = _suggest_next_dimension_id(db_path, document_id)
    dimension_id = input("Dimension id [default {}]: ".format(suggested_id)).strip() or suggested_id
    raw_text = input("Raw text [optional if nominal value is provided]: ").strip()
    nominal_value = input("Nominal value: ").strip()
    tolerance_symmetric = input("Tolerance +/- [optional]: ").strip()
    tolerance_plus = input("Tolerance + [optional]: ").strip()
    tolerance_minus = input("Tolerance - [optional]: ").strip()
    view_id = input("View id [optional]: ").strip()
    return save_manual_dimension(
        db_path=db_path,
        document_id=document_id,
        dimension_id=dimension_id,
        raw_text=raw_text,
        nominal_value=nominal_value,
        tolerance_symmetric=tolerance_symmetric,
        tolerance_plus=tolerance_plus,
        tolerance_minus=tolerance_minus,
        view_id=view_id,
    )


def _confirm_coverage_terminal(review_image_path, candidates, expected_dimension_count=None, db_path=None, document_id=None):
    print("")
    print(_safe_console_text("Review image: {}".format(review_image_path)))
    print(_safe_console_text(_coverage_summary_text(candidates, expected_dimension_count=expected_dimension_count)))
    print("")
    print("Extracted dimensions:")
    for line in _dimension_list_lines(candidates):
        print(_safe_console_text("  {}".format(line)))
    print("Human review terminal fallback: auto-approving extracted dimensions and continuing to Layer 2.")
    result = {"coverage_ok": True}
    result.update(_default_post_review_actions())
    return result


def _confirm_coverage_tk(
    review_image_path,
    candidates,
    expected_dimension_count=None,
    coverage_image_path=None,
    db_path=None,
    document_id=None,
):
    import tkinter as tk
    from tkinter import messagebox, ttk

    from PIL import ImageTk

    result = {"coverage_ok": None}
    result.update(_default_post_review_actions())
    root = tk.Tk()
    root.title("Coverage Review")
    apply_standard_window(root)
    container = ttk.Frame(root, padding=12)
    container.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    container.columnconfigure(0, weight=3)
    container.columnconfigure(1, weight=2)
    container.rowconfigure(0, weight=1)

    full_image = Image.open(review_image_path).convert("RGB")
    try:
        preview = full_image.copy()
        max_preview_width = STANDARD_REVIEW_PREVIEW_WIDTH
        max_preview_height = STANDARD_REVIEW_PREVIEW_HEIGHT
        preview.thumbnail((max_preview_width, max_preview_height))
        photo = ImageTk.PhotoImage(preview)
        preview_width, preview_height = preview.size
        full_width, full_height = full_image.size
    finally:
        full_image.close()

    scale_x = preview_width / float(max(1, full_width))
    scale_y = preview_height / float(max(1, full_height))

    preview_rects = [_scale_rect(_resolve_display_bbox(record), scale_x, scale_y) for record in candidates]

    image_frame = ttk.Frame(container)
    image_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    image_frame.columnconfigure(0, weight=1)
    image_frame.rowconfigure(0, weight=1)
    image_canvas = tk.Canvas(
        image_frame,
        width=preview_width,
        height=preview_height,
        highlightthickness=1,
        highlightbackground="#666666",
        background="#1f1f1f",
    )
    image_canvas.grid(row=0, column=0, sticky="nsew")
    image_canvas.create_image(0, 0, anchor="nw", image=photo, tags=("sheet_image",))
    image_canvas.image = photo

    summary_frame = ttk.Frame(container)
    summary_frame.grid(row=0, column=1, sticky="nsew")
    summary_frame.columnconfigure(0, weight=1)
    summary_frame.rowconfigure(4, weight=1)
    summary_var = tk.StringVar(value=_coverage_summary_text(candidates, expected_dimension_count=expected_dimension_count))
    ttk.Label(summary_frame, text="Coverage Review", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
    ttk.Label(
        summary_frame,
        textvariable=summary_var,
        justify="left",
        wraplength=440,
    ).grid(row=1, column=0, sticky="w", pady=(10, 12))
    ttk.Button(
        summary_frame,
        text="Open Review Image",
        command=lambda: _open_file_in_default_viewer(review_image_path),
    ).grid(row=2, column=0, sticky="w", pady=(0, 16))
    if coverage_image_path and str(Path(coverage_image_path).resolve()) != str(Path(review_image_path).resolve()):
        ttk.Button(
            summary_frame,
            text="Open Coverage Image",
            command=lambda: _open_file_in_default_viewer(coverage_image_path),
        ).grid(row=2, column=0, sticky="e", pady=(0, 16))

    ttk.Label(summary_frame, text="Extracted Dimensions", font=("Segoe UI", 10, "bold")).grid(
        row=3, column=0, sticky="w", pady=(0, 6)
    )
    list_frame = ttk.Frame(summary_frame)
    list_frame.grid(row=4, column=0, sticky="nsew")
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(0, weight=1)
    listbox = tk.Listbox(list_frame, height=20, width=64, font=("Segoe UI", 11))
    listbox.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    listbox.configure(yscrollcommand=scrollbar.set)

    selection_var = tk.StringVar(
        value="Click a dimension in the list to point to it on the annotated drawing."
    )
    ttk.Label(
        summary_frame,
        textvariable=selection_var,
        justify="left",
        wraplength=440,
    ).grid(row=5, column=0, sticky="w", pady=(10, 12))

    def update_canvas_pointer(index):
        image_canvas.delete("selection_overlay")
        if index is None or index < 0 or index >= len(candidates):
            selection_var.set("Click a dimension in the list to point to it on the annotated drawing.")
            return

        rect = preview_rects[index]
        record = candidates[index]
        dimension = record.get("dimension") or {}
        if not rect:
            selection_var.set(
                "{} selected, but no bbox is available for this dimension.".format(record.get("dimension_id") or "?")
            )
            return

        x1, y1, x2, y2 = rect
        center = _rect_center(rect)
        center_x = int(round(center[0]))
        center_y = int(round(center[1]))

        image_canvas.create_rectangle(
            x1,
            y1,
            x2,
            y2,
            outline="#eb1e13",
            width=4,
            tags=("selection_overlay",),
        )
        image_canvas.create_line(
            max(8, x1 - 70),
            max(8, y1 - 55),
            center_x,
            center_y,
            fill="#ece915",
            width=3,
            arrow="last",
            arrowshape=(12, 14, 5),
            tags=("selection_overlay",),
        )
        image_canvas.create_oval(
            center_x - 6,
            center_y - 6,
            center_x + 6,
            center_y + 6,
            outline="#fc1b0f",
            width=3,
            tags=("selection_overlay",),
        )
        image_canvas.create_text(
            max(10, x1 - 6),
            max(10, y1 - 62),
            anchor="sw",
            text=str(record.get("dimension_id") or "").upper(),
            fill="#f02319",
            font=("Segoe UI", 10, "bold"),
            tags=("selection_overlay",),
        )
        selection_var.set(
            "{} -> {} | nominal={} | verification={}".format(
                record.get("dimension_id") or "?",
                (dimension.get("raw_text") or "<empty>").replace("\n", " ").strip(),
                "missing" if dimension.get("nominal_value") is None else dimension.get("nominal_value"),
                (dimension.get("value_verification") or {}).get("overall_status", "unknown"),
            )
        )

    def refresh_dimension_list():
        summary_var.set(_coverage_summary_text(candidates, expected_dimension_count=expected_dimension_count))
        listbox.delete(0, "end")
        for line in _dimension_list_lines(candidates):
            listbox.insert("end", line)

    def get_selected_index():
        selection = listbox.curselection()
        if not selection:
            return None
        selected_index = int(selection[0])
        if selected_index < 0 or selected_index >= len(candidates):
            return None
        return selected_index

    def select_index(index):
        if index < 0 or index >= len(candidates):
            return
        listbox.selection_clear(0, "end")
        listbox.selection_set(index)
        listbox.activate(index)
        listbox.see(index)
        update_canvas_pointer(index)

    def on_listbox_select(_event=None):
        selection = listbox.curselection()
        if not selection:
            update_canvas_pointer(None)
            return
        update_canvas_pointer(int(selection[0]))

    def on_canvas_click(event):
        if not preview_rects:
            return
        best_index = None
        best_score = None
        for index, rect in enumerate(preview_rects):
            if not rect:
                continue
            x1, y1, x2, y2 = rect
            center = _rect_center(rect)
            center_x = center[0]
            center_y = center[1]
            inside_bonus = 0 if (x1 <= event.x <= x2 and y1 <= event.y <= y2) else 1000000
            dx = center_x - event.x
            dy = center_y - event.y
            score = inside_bonus + (dx * dx) + (dy * dy)
            if best_score is None or score < best_score:
                best_score = score
                best_index = index
        if best_index is not None:
            select_index(best_index)

    listbox.bind("<<ListboxSelect>>", on_listbox_select)
    image_canvas.bind("<Button-1>", on_canvas_click)
    listbox.focus_set()
    refresh_dimension_list()
    if candidates:
        select_index(0)

    def open_manual_dimension_dialog():
        dialog = tk.Toplevel(root)
        dialog.title("Add Manual Dimension")
        dialog.transient(root)
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)

        suggested_id = _suggest_next_dimension_id(db_path, document_id)
        field_defaults = {
            "dimension_id": suggested_id,
            "raw_text": "",
            "nominal_value": "",
            "tolerance_symmetric": "",
            "tolerance_plus": "",
            "tolerance_minus": "",
            "view_id": "",
        }
        variables = {name: tk.StringVar(value=value) for name, value in field_defaults.items()}

        fields = [
            ("Dimension ID", "dimension_id"),
            ("Raw text", "raw_text"),
            ("Nominal value", "nominal_value"),
            ("Tolerance +/-", "tolerance_symmetric"),
            ("Tolerance +", "tolerance_plus"),
            ("Tolerance -", "tolerance_minus"),
            ("View ID", "view_id"),
        ]

        for row_index, (label, key) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row_index, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(dialog, textvariable=variables[key], width=32).grid(
                row=row_index,
                column=1,
                sticky="ew",
                padx=(0, 10),
                pady=6,
            )

        ttk.Label(
            dialog,
            text="Leave Raw text blank if you want it built from the nominal value and tolerance.",
            justify="left",
            wraplength=360,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))

        def submit():
            try:
                record = save_manual_dimension(
                    db_path=db_path,
                    document_id=document_id,
                    dimension_id=variables["dimension_id"].get(),
                    raw_text=variables["raw_text"].get(),
                    nominal_value=variables["nominal_value"].get(),
                    tolerance_symmetric=variables["tolerance_symmetric"].get(),
                    tolerance_plus=variables["tolerance_plus"].get(),
                    tolerance_minus=variables["tolerance_minus"].get(),
                    view_id=variables["view_id"].get(),
                )
            except Exception as exc:
                messagebox.showerror("Could not Add Dimension", str(exc), parent=dialog)
                return

            candidates.append(record)
            preview_rects.append(None)
            refresh_dimension_list()
            select_index(len(candidates) - 1)
            dialog.destroy()

        button_frame = ttk.Frame(dialog)
        button_frame.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(button_frame, text="Cancel", command=dialog.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_frame, text="Add Dimension", command=submit).grid(row=0, column=1)

        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.wait_window()

    def open_edit_dimension_dialog():
        selected_index = get_selected_index()
        if selected_index is None:
            messagebox.showinfo("Select Dimension", "Select a dimension to modify.")
            return

        record = candidates[selected_index]
        dimension = record.get("dimension") or {}
        dialog = tk.Toplevel(root)
        dialog.title("Modify Dimension")
        dialog.transient(root)
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)

        symmetric_default = ""
        plus_value = dimension.get("tolerance_plus")
        minus_value = dimension.get("tolerance_minus")
        if plus_value is not None and minus_value is not None and abs(float(plus_value)) == abs(float(minus_value)):
            symmetric_default = "{:g}".format(abs(float(plus_value)))

        field_defaults = {
            "dimension_id": record.get("dimension_id") or "",
            "raw_text": dimension.get("raw_text") or "",
            "nominal_value": "" if dimension.get("nominal_value") is None else "{:g}".format(float(dimension.get("nominal_value"))),
            "tolerance_symmetric": symmetric_default,
            "tolerance_plus": "" if plus_value is None else "{:g}".format(float(plus_value)),
            "tolerance_minus": "" if minus_value is None else "{:g}".format(float(minus_value)),
            "view_id": record.get("view_id") or "",
        }
        variables = {name: tk.StringVar(value=value) for name, value in field_defaults.items()}

        fields = [
            ("Dimension ID", "dimension_id"),
            ("Raw text", "raw_text"),
            ("Nominal value", "nominal_value"),
            ("Tolerance +/-", "tolerance_symmetric"),
            ("Tolerance +", "tolerance_plus"),
            ("Tolerance -", "tolerance_minus"),
            ("View ID", "view_id"),
        ]

        for row_index, (label, key) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row_index, column=0, sticky="w", padx=10, pady=6)
            entry_state = "readonly" if key == "dimension_id" else "normal"
            ttk.Entry(dialog, textvariable=variables[key], width=32, state=entry_state).grid(
                row=row_index,
                column=1,
                sticky="ew",
                padx=(0, 10),
                pady=6,
            )

        ttk.Label(
            dialog,
            text="Dimension ID stays fixed. Leave Raw text blank if you want it rebuilt from nominal and tolerance.",
            justify="left",
            wraplength=380,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))

        def submit():
            try:
                updated_record = update_dimension_record(
                    db_path=db_path,
                    document_id=document_id,
                    dimension_id=record.get("dimension_id"),
                    raw_text=variables["raw_text"].get(),
                    nominal_value=variables["nominal_value"].get(),
                    tolerance_symmetric=variables["tolerance_symmetric"].get(),
                    tolerance_plus=variables["tolerance_plus"].get(),
                    tolerance_minus=variables["tolerance_minus"].get(),
                    view_id=variables["view_id"].get(),
                    dimension_type=dimension.get("dimension_type"),
                )
            except Exception as exc:
                messagebox.showerror("Could not Modify Dimension", str(exc), parent=dialog)
                return

            preserved_dimension = dict(record.get("dimension") or {})
            preserved_dimension.update(updated_record.get("dimension") or {})
            updated_record["dimension"] = preserved_dimension
            updated_record["requires_manual_review"] = record.get("requires_manual_review", False)
            updated_record["human_review_status"] = record.get("human_review_status", REVIEW_PENDING)
            updated_record["is_manual_addition"] = record.get("is_manual_addition", False)
            candidates[selected_index] = updated_record
            refresh_dimension_list()
            select_index(selected_index)
            dialog.destroy()

        button_frame = ttk.Frame(dialog)
        button_frame.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(button_frame, text="Cancel", command=dialog.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_frame, text="Save Changes", command=submit).grid(row=0, column=1)

        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.wait_window()

    def delete_selected_dimension():
        selected_index = get_selected_index()
        if selected_index is None:
            messagebox.showinfo("Select Dimension", "Select a dimension to delete.")
            return

        record = candidates[selected_index]
        dimension_label = "{} -> {}".format(
            record.get("dimension_id") or "?",
            ((record.get("dimension") or {}).get("raw_text") or "<empty>").replace("\n", " ").strip(),
        )
        if not messagebox.askyesno(
            "Delete Dimension",
            "Delete this dimension from the review and SQLite?\n\n{}".format(dimension_label),
            parent=root,
        ):
            return

        try:
            delete_dimension_record(db_path, document_id, record.get("dimension_id"))
        except Exception as exc:
            messagebox.showerror("Could not Delete Dimension", str(exc), parent=root)
            return

        del candidates[selected_index]
        del preview_rects[selected_index]
        refresh_dimension_list()
        if candidates:
            select_index(min(selected_index, len(candidates) - 1))
        else:
            update_canvas_pointer(None)

    def accept():
        result["coverage_ok"] = True
        result["run_layer2_extract"] = bool(run_layer2_extract_var.get())
        result["run_layer2_link"] = bool(run_layer2_link_var.get())
        result["run_layer3"] = bool(run_layer3_var.get())
        root.destroy()

    def rerun():
        result["coverage_ok"] = False
        root.destroy()

    post_run_frame = ttk.LabelFrame(summary_frame, text="Continue Pipeline After Review", padding=10)
    post_run_frame.grid(row=6, column=0, sticky="ew", pady=(2, 12))
    run_layer2_extract_var = tk.BooleanVar(value=result["run_layer2_extract"])
    run_layer2_link_var = tk.BooleanVar(value=result["run_layer2_link"])
    run_layer3_var = tk.BooleanVar(value=result["run_layer3"])
    ttk.Checkbutton(
        post_run_frame,
        text="Run Layer 2 published-parameter extraction",
        variable=run_layer2_extract_var,
    ).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(
        post_run_frame,
        text="Open Layer 2 dimension link UI",
        variable=run_layer2_link_var,
    ).grid(row=1, column=0, sticky="w", pady=(4, 0))
    ttk.Checkbutton(
        post_run_frame,
        text="Run Layer 3 DMU",
        variable=run_layer3_var,
    ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _sync_post_review_dependencies():
        if not run_layer2_extract_var.get():
            run_layer2_link_var.set(False)

    run_layer2_extract_var.trace_add("write", lambda *_args: _sync_post_review_dependencies())
    _sync_post_review_dependencies()

    button_frame = ttk.Frame(summary_frame)
    button_frame.grid(row=7, column=0, sticky="w")
    ttk.Button(button_frame, text="All Dimensions are Approved", command=accept).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(button_frame, text="Add Dimension", command=open_manual_dimension_dialog).grid(
        row=0, column=1, padx=(0, 8)
    )
    ttk.Button(button_frame, text="Modify Dimension", command=open_edit_dimension_dialog).grid(
        row=0, column=2, padx=(0, 8)
    )
    ttk.Button(button_frame, text="Delete Dimension", command=delete_selected_dimension).grid(
        row=0, column=3, padx=(0, 8)
    )
    ttk.Button(button_frame, text="Rerun 2D Extraction", command=rerun).grid(row=0, column=4)

    root.protocol("WM_DELETE_WINDOW", rerun)
    root.mainloop()
    return result


def _build_crop_image(prepared_image_path, bbox, padding=120):
    image = Image.open(prepared_image_path).convert("RGB")
    rect = _rect_from_bbox(bbox)
    if not rect:
        image.thumbnail((900, 700))
        return image

    width, height = image.size
    left = max(0, rect[0] - padding)
    top = max(0, rect[1] - padding)
    right = min(width, rect[2] + padding)
    bottom = min(height, rect[3] + padding)
    crop = image.crop((left, top, right, bottom))

    draw = ImageDraw.Draw(crop)
    draw.rectangle(
        (
            rect[0] - left,
            rect[1] - top,
            rect[2] - left,
            rect[3] - top,
        ),
        outline=(220, 40, 40),
        width=4,
    )
    crop.thumbnail((900, 700))
    return crop


def _open_file_in_default_viewer(path):
    if not path:
        return
    resolved = str(Path(path).resolve())
    if hasattr(os, "startfile"):
        os.startfile(resolved)


def _run_terminal_review(candidates, db_path, document_id, progress_callback=None):
    for index, record in enumerate(candidates, start=1):
        if record.get("human_review_status") != REVIEW_PENDING:
            continue
        if progress_callback:
            progress_callback(
                "Human review {}/{} - {}".format(index, len(candidates), record["dimension_id"])
            )

        dimension = record["dimension"]
        verification = dimension.get("value_verification") or {}
        print("")
        print("[{}/{}] {}".format(index, len(candidates), record["dimension_id"]))
        print("View: {} ({})".format(record.get("view_id") or "unassigned", record.get("view_type") or "unknown"))
        print("Raw text: {}".format(dimension.get("raw_text") or "<empty>"))
        print(_format_tolerance_text(dimension))
        print("Value verification: {}".format(verification.get("overall_status", "unknown")))
        print("BBox ready: {}".format("yes" if dimension.get("annotation_ready") else "no"))

        while True:
            choice = input("Approve this dimension for CATIA sync? [y=yes / n=no]: ").strip().lower()
            if choice in ("y", "yes"):
                save_dimension_review_decision(db_path, document_id, record["dimension_id"], REVIEW_APPROVED)
                record["human_review_status"] = REVIEW_APPROVED
                break
            if choice in ("n", "no"):
                save_dimension_review_decision(db_path, document_id, record["dimension_id"], REVIEW_REJECTED)
                record["human_review_status"] = REVIEW_REJECTED
                break
            print("Please enter y/yes or n/no.")


def _run_tk_review(candidates, db_path, document_id, prepared_image_path, coverage_image_path=None, annotated_path=None, progress_callback=None):
    import tkinter as tk
    from tkinter import messagebox, ttk

    from PIL import ImageTk

    class ReviewApp:
        def __init__(self, root):
            self.root = root
            self.root.title("Dimension Review")
            apply_standard_window(self.root)
            self.index = self._find_first_pending_index()
            self.photo = None

            self.progress_var = tk.StringVar()
            self.title_var = tk.StringVar()
            self.status_var = tk.StringVar()
            self.detail_var = tk.StringVar()

            container = ttk.Frame(root, padding=12)
            container.grid(row=0, column=0, sticky="nsew")
            root.columnconfigure(0, weight=1)
            root.rowconfigure(0, weight=1)
            container.columnconfigure(0, weight=3)
            container.columnconfigure(1, weight=2)
            container.rowconfigure(1, weight=1)

            ttk.Label(container, textvariable=self.progress_var, font=("Segoe UI", 11, "bold")).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
            )

            self.image_label = ttk.Label(container)
            self.image_label.grid(row=1, column=0, sticky="nsew", padx=(0, 12))

            detail_frame = ttk.Frame(container)
            detail_frame.grid(row=1, column=1, sticky="nsew")
            detail_frame.columnconfigure(0, weight=1)

            ttk.Label(detail_frame, textvariable=self.title_var, font=("Segoe UI", 12, "bold"), wraplength=420).grid(
                row=0, column=0, sticky="w", pady=(0, 8)
            )
            ttk.Label(detail_frame, textvariable=self.status_var, justify="left", wraplength=420).grid(
                row=1, column=0, sticky="w", pady=(0, 8)
            )
            ttk.Label(detail_frame, textvariable=self.detail_var, justify="left", wraplength=420).grid(
                row=2, column=0, sticky="w"
            )

            button_frame = ttk.Frame(detail_frame)
            button_frame.grid(row=3, column=0, sticky="w", pady=(18, 0))

            ttk.Button(button_frame, text="Back", command=self.go_back).grid(row=0, column=0, padx=(0, 8))
            ttk.Button(button_frame, text="Reject", command=lambda: self.apply(REVIEW_REJECTED)).grid(
                row=0, column=1, padx=(0, 8)
            )
            ttk.Button(button_frame, text="Approve", command=lambda: self.apply(REVIEW_APPROVED)).grid(
                row=0, column=2, padx=(0, 8)
            )

            open_frame = ttk.Frame(detail_frame)
            open_frame.grid(row=4, column=0, sticky="w", pady=(12, 0))
            ttk.Button(
                open_frame,
                text="Open Prepared Image",
                command=lambda: _open_file_in_default_viewer(prepared_image_path),
            ).grid(row=0, column=0, padx=(0, 8))
            if coverage_image_path:
                ttk.Button(
                    open_frame,
                    text="Open Coverage Image",
                    command=lambda: _open_file_in_default_viewer(coverage_image_path),
                ).grid(row=0, column=1, padx=(0, 8))
            if annotated_path:
                ttk.Button(
                    open_frame,
                    text="Open Annotated Image",
                    command=lambda: _open_file_in_default_viewer(annotated_path),
                ).grid(row=0, column=2)

            self.root.bind("<Left>", lambda _event: self.go_back())
            self.root.bind("<Right>", lambda _event: self.apply(REVIEW_APPROVED))
            self.root.bind("y", lambda _event: self.apply(REVIEW_APPROVED))
            self.root.bind("n", lambda _event: self.apply(REVIEW_REJECTED))
            self.root.protocol("WM_DELETE_WINDOW", self.handle_close)
            self.render_current()

        def _find_first_pending_index(self):
            for index, record in enumerate(candidates):
                if record.get("human_review_status") == REVIEW_PENDING:
                    return index
            return 0

        def current_record(self):
            return candidates[self.index]

        def render_current(self):
            record = self.current_record()
            dimension = record["dimension"]
            verification = dimension.get("value_verification") or {}
            total = len(candidates)
            self.progress_var.set("Review {}/{} | {}".format(self.index + 1, total, record["dimension_id"]))
            self.title_var.set(dimension.get("raw_text") or "<empty dimension text>")

            status_lines = [
                "View: {} ({})".format(record.get("view_id") or "unassigned", record.get("view_type") or "unknown"),
                "Current decision: {}".format(record.get("human_review_status", REVIEW_PENDING)),
                "BBox ready: {}".format("yes" if dimension.get("annotation_ready") else "no"),
                "Value verification: {}".format(verification.get("overall_status", "unknown")),
            ]
            self.status_var.set("\n".join(status_lines))

            detail_lines = [
                _format_tolerance_text(dimension),
                "Matched text: {}".format(verification.get("matched_text") or "none"),
                "Location verified: {}".format("yes" if verification.get("location_verified") else "no"),
                "Needs manual review: {}".format("yes" if record.get("requires_manual_review") else "no"),
            ]
            self.detail_var.set("\n".join(detail_lines))

            crop = _build_crop_image(prepared_image_path, dimension.get("bbox"))
            self.photo = ImageTk.PhotoImage(crop)
            self.image_label.configure(image=self.photo)

            if progress_callback:
                progress_callback(
                    "Human review {}/{} - {}".format(self.index + 1, total, record["dimension_id"])
                )

        def apply(self, decision):
            record = self.current_record()
            save_dimension_review_decision(db_path, document_id, record["dimension_id"], decision)
            record["human_review_status"] = decision
            if self.index >= len(candidates) - 1:
                self.root.destroy()
                return
            self.index += 1
            self.render_current()

        def go_back(self):
            if self.index <= 0:
                return
            self.index -= 1
            self.render_current()

        def handle_close(self):
            pending = [record for record in candidates if record.get("human_review_status") == REVIEW_PENDING]
            if pending:
                messagebox.showwarning(
                    "Review Incomplete",
                    "Some dimensions are still pending. Finish the review or rerun it later before CATIA sync.",
                )
                return
            self.root.destroy()

    root = tk.Tk()
    ReviewApp(root)
    root.mainloop()


def run_human_dimension_review(
    result,
    db_path,
    prepared_image_path,
    document_id=None,
    review_scope="all",
    expected_dimension_count=None,
    annotated_path=None,
    progress_callback=None,
):
    resolved_document_id = document_id or result.get("document_id")
    effective_review_scope = "all"
    if not resolved_document_id:
        raise RuntimeError("Human dimension review requires a document_id.")

    if review_scope not in ("all", "flagged"):
        raise RuntimeError("Unsupported human review scope: {}".format(review_scope))

    all_records = _build_review_candidates(result, db_path, resolved_document_id, "all")
    if not all_records:
        return {
            "status": "skipped",
            "review_scope": effective_review_scope,
            "document_id": resolved_document_id,
            "candidate_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "pending_count": 0,
        }

    coverage_image_path = build_coverage_review_image(
        prepared_image_path,
        all_records,
        Path(annotated_path).with_name("{}_coverage_review.png".format(Path(annotated_path).stem.replace("_annotated", "")))
        if annotated_path
        else Path(prepared_image_path).with_name("{}_coverage_review.png".format(Path(prepared_image_path).stem)),
    )
    review_image_path = coverage_image_path
    if annotated_path:
        annotated_candidate = Path(annotated_path).resolve()
        if annotated_candidate.exists():
            review_image_path = annotated_candidate
    extracted_count = len(all_records)
    missing_dimension_estimate = None if expected_dimension_count is None else max(0, int(expected_dimension_count) - extracted_count)
    manual_addition_count = sum(1 for record in all_records if record.get("is_manual_addition"))

    try:
        coverage_decision = _confirm_coverage_tk(
            review_image_path,
            all_records,
            expected_dimension_count=expected_dimension_count,
            coverage_image_path=coverage_image_path,
            db_path=db_path,
            document_id=resolved_document_id,
        )
    except Exception:
        coverage_decision = _confirm_coverage_terminal(
            review_image_path,
            all_records,
            expected_dimension_count=expected_dimension_count,
            db_path=db_path,
            document_id=resolved_document_id,
        )

    if isinstance(coverage_decision, dict):
        coverage_ok = bool(coverage_decision.get("coverage_ok"))
        post_review_actions = {
            "run_layer2_extract": bool(coverage_decision.get("run_layer2_extract", True)),
            "run_layer2_link": bool(coverage_decision.get("run_layer2_link", True)),
            "run_layer3": bool(coverage_decision.get("run_layer3", True)),
        }
    else:
        coverage_ok = bool(coverage_decision)
        post_review_actions = _default_post_review_actions()

    candidates = list(all_records)

    if not coverage_ok:
        return {
            "status": REVIEW_RERUN_REQUESTED,
            "review_scope": effective_review_scope,
            "document_id": resolved_document_id,
            "candidate_count": len(candidates),
            "approved_count": 0,
            "rejected_count": 0,
            "pending_count": _summarize_candidate_statuses(
                db_path,
                resolved_document_id,
                [record["dimension_id"] for record in candidates],
            ).get(REVIEW_PENDING, 0),
            "coverage_image_path": str(coverage_image_path),
            "expected_dimension_count": expected_dimension_count,
            "extracted_dimension_count": extracted_count,
            "missing_dimension_estimate": missing_dimension_estimate,
            "manual_dimensions_added": manual_addition_count,
            "post_review_actions": post_review_actions,
        }

    if progress_callback:
        progress_callback("Human review accepted coverage - approving extracted dimensions for CATIA sync")

    save_dimension_review_decisions_bulk(
        db_path,
        resolved_document_id,
        [record["dimension_id"] for record in candidates],
        REVIEW_APPROVED,
        notes="Approved from coverage review.",
    )
    dimension_log_lines = _emit_dimension_log(candidates, progress_callback=progress_callback)

    candidate_counts = _summarize_candidate_statuses(
        db_path,
        resolved_document_id,
        [record["dimension_id"] for record in candidates],
    )
    overall_counts = summarize_dimension_reviews(db_path, resolved_document_id)
    status = "completed" if candidate_counts.get(REVIEW_PENDING, 0) == 0 else "incomplete"

    return {
        "status": status,
        "review_scope": effective_review_scope,
        "document_id": resolved_document_id,
        "candidate_count": len(candidates),
        "approved_count": candidate_counts.get(REVIEW_APPROVED, 0),
        "rejected_count": candidate_counts.get(REVIEW_REJECTED, 0),
        "pending_count": candidate_counts.get(REVIEW_PENDING, 0),
        "coverage_image_path": str(coverage_image_path),
        "expected_dimension_count": expected_dimension_count,
        "extracted_dimension_count": extracted_count,
        "missing_dimension_estimate": missing_dimension_estimate,
        "manual_dimensions_added": sum(1 for record in candidates if record.get("is_manual_addition")),
        "overall_approved_count": overall_counts.get(REVIEW_APPROVED, 0),
        "overall_rejected_count": overall_counts.get(REVIEW_REJECTED, 0),
        "overall_pending_count": overall_counts.get(REVIEW_PENDING, 0),
        "dimension_log_lines": dimension_log_lines,
        "post_review_actions": post_review_actions,
    }
