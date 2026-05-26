import argparse
import json
import sqlite3
from pathlib import Path

try:
    from .user_parameter_agent import (
        collect_active_text,
        collect_parameter_type,
        collect_parameter_value_text,
        collect_relation_text,
        connect_catia,
        get_child_products,
        get_document_metadata,
        get_document_product,
        iter_com_collection,
        iter_all_parameters,
        normalize_identity_key,
        parameter_leaf_name,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_int,
        safe_text,
        utc_now_iso,
    )
except ImportError:
    from openai_pipeline.pipeline_stack.catia_agents.user_parameter_agent import (
        collect_active_text,
        collect_parameter_type,
        collect_parameter_value_text,
        collect_relation_text,
        connect_catia,
        get_child_products,
        get_document_metadata,
        get_document_product,
        iter_com_collection,
        iter_all_parameters,
        normalize_identity_key,
        parameter_leaf_name,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_int,
        safe_text,
        utc_now_iso,
    )

try:
    from ..helpers.sqlite_importer import connect_db, create_tables, migrate_tables
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.sqlite_importer import connect_db, create_tables, migrate_tables


def normalize_selector(text):
    value = safe_text(text)
    if not value:
        return None
    return value.replace("/", "\\").strip().lower()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Extract published CATIA parameters from a selected part inside the active "
            "CATProduct/CATPart and store them in the drawing pipeline SQLite database."
        )
    )
    parser.add_argument(
        "--db",
        required=True,
        type=str,
        help="Path to the drawing pipeline SQLite database.",
    )
    parser.add_argument(
        "--part_number",
        required=True,
        type=str,
        help="Part number to match inside the open CATIA product structure.",
    )
    parser.add_argument(
        "--document_id",
        default=None,
        type=str,
        help="Optional drawing document_id used as the foreign-key owner in SQLite.",
    )
    parser.add_argument(
        "--root_document",
        default=None,
        type=str,
        help="Optional CATIA CATProduct/CATPart path. Defaults to CATIA ActiveDocument.",
    )
    parser.add_argument(
        "--summary_json",
        default=None,
        type=str,
        help="Optional JSON summary output path.",
    )
    return parser.parse_args()


def load_drawing_identity_from_sqlite(db_path, document_id=None):
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.exists():
        raise RuntimeError("SQLite database not found: {}".format(resolved_db_path))

    conn = sqlite3.connect(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT
                document_id,
                part_number
            FROM documents
        """
        params = []
        if document_id:
            sql += " WHERE document_id = ?"
            params.append(document_id)
        sql += " ORDER BY document_id LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise RuntimeError("No matching drawing document row was found in {}".format(resolved_db_path))
        return {
            "document_id": row["document_id"],
            "part_number": row["part_number"],
        }
    finally:
        conn.close()


def walk_product_instances(product, tree_path, depth=0):
    yield {
        "product": product,
        "tree_path": tree_path,
        "depth": depth,
        "instance_name": safe_text(safe_com_get(product, "Name")),
        "part_number": safe_text(safe_com_get(product, "PartNumber")),
    }

    for index, child in enumerate(get_child_products(product), start=1):
        child_name = safe_text(safe_com_get(child, "Name")) or "child_{}".format(index)
        child_tree_path = "{} > {}".format(tree_path, child_name)
        yield from walk_product_instances(child, child_tree_path, depth + 1)


def matches_part_number(candidate, target_part_number):
    normalized_target = normalize_identity_key(target_part_number)
    if not normalized_target:
        return False

    for value in (candidate.get("part_number"), candidate.get("instance_name")):
        if normalize_identity_key(value) == normalized_target:
            return True
    return False


def resolve_target_matches(root_product, target_part_number):
    root_name = safe_text(safe_com_get(root_product, "Name")) or safe_text(safe_com_get(root_product, "PartNumber")) or "ROOT_PRODUCT"
    matches = []
    for candidate in walk_product_instances(root_product, root_name, depth=0):
        children = get_child_products(candidate["product"])
        if children:
            continue
        if matches_part_number(candidate, target_part_number):
            matches.append(candidate)
    return matches


def get_publication_valuation(publication):
    valuation = safe_com_get(publication, "Valuation")
    if callable(valuation):
        try:
            valuation = valuation()
        except Exception:
            valuation = None
    if valuation is None:
        valuation = safe_com_call(publication, "Valuation")
    return valuation


def build_parameter_snapshot_map(product):
    snapshots = {}
    for parameter in iter_all_parameters(product) or []:
        parameter_path = safe_text(safe_com_get(parameter, "Name"))
        parameter_name = (
            safe_text(safe_com_get(parameter, "RenamedName"))
            or parameter_leaf_name(parameter_path)
            or parameter_path
        )
        snapshot = {
            "parameter_path": parameter_path,
            "parameter_name": parameter_name,
            "value_text": collect_parameter_value_text(parameter),
            "formula_text": collect_relation_text(parameter),
            "active_text": collect_active_text(parameter),
            "user_access_mode": safe_int(safe_com_get(parameter, "UserAccessMode")),
            "parameter_type": collect_parameter_type(parameter),
        }
        for candidate in (
            parameter_path,
            parameter_name,
            parameter_leaf_name(parameter_path),
            parameter_leaf_name(parameter_name),
        ):
            normalized = normalize_selector(candidate)
            if normalized and normalized not in snapshots:
                snapshots[normalized] = snapshot
    return snapshots


def resolve_parameter_snapshot(parameter_snapshots, publication_name, parameter_path, parameter_name):
    for candidate in (
        parameter_path,
        parameter_name,
        parameter_leaf_name(parameter_path),
        parameter_leaf_name(parameter_name),
        publication_name,
    ):
        normalized = normalize_selector(candidate)
        if normalized and normalized in parameter_snapshots:
            return parameter_snapshots[normalized]
    return None


def extract_publication_rows_from_product(product):
    publications = safe_com_get(product, "Publications")
    total = safe_int(safe_com_get(publications, "Count"), default=0) if publications is not None else 0
    parameter_snapshots = build_parameter_snapshot_map(product)
    rows = []

    for index, publication in enumerate(iter_com_collection(publications), start=1):
        publication_name = safe_text(safe_com_get(publication, "Name")) or "publication_{}".format(index)
        valuation = get_publication_valuation(publication)
        parameter_path = safe_text(safe_com_get(valuation, "Name")) if valuation is not None else None
        parameter_name = (
            safe_text(safe_com_get(valuation, "RenamedName")) if valuation is not None else None
        ) or parameter_leaf_name(parameter_path) or parameter_path or publication_name
        value_text = collect_parameter_value_text(valuation) if valuation is not None else None
        formula_text = collect_relation_text(valuation) if valuation is not None else None
        active_text = collect_active_text(valuation) if valuation is not None else None
        user_access_mode = safe_int(safe_com_get(valuation, "UserAccessMode")) if valuation is not None else None
        parameter_type = collect_parameter_type(valuation) if valuation is not None else None
        parameter_snapshot = resolve_parameter_snapshot(
            parameter_snapshots,
            publication_name,
            parameter_path,
            parameter_name,
        )

        if parameter_snapshot is not None:
            parameter_path = parameter_path or parameter_snapshot.get("parameter_path")
            parameter_name = parameter_name or parameter_snapshot.get("parameter_name")
            value_text = value_text or parameter_snapshot.get("value_text")
            formula_text = formula_text or parameter_snapshot.get("formula_text")
            active_text = active_text or parameter_snapshot.get("active_text")
            user_access_mode = user_access_mode if user_access_mode is not None else parameter_snapshot.get("user_access_mode")
            parameter_type = parameter_type or parameter_snapshot.get("parameter_type")

        rows.append(
            {
                "publication_name": publication_name,
                "parameter_path": parameter_path,
                "parameter_name": parameter_name,
                "value_text": value_text,
                "formula_text": formula_text,
                "active_text": active_text,
                "user_access_mode": user_access_mode,
                "parameter_type": parameter_type,
            }
        )

    return total, rows


def merge_publication_rows(*row_groups):
    merged = []
    seen = set()
    for rows in row_groups:
        for row in rows or []:
            key = (
                str(row.get("publication_name") or "").strip().lower(),
                str(row.get("parameter_path") or "").strip().lower(),
                str(row.get("parameter_name") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def extract_publication_rows(owner_product, instance_product=None):
    owner_total, owner_rows = extract_publication_rows_from_product(owner_product)
    instance_total = 0
    instance_rows = []
    if instance_product is not None and instance_product is not owner_product:
        instance_total, instance_rows = extract_publication_rows_from_product(instance_product)
    return owner_total + instance_total, merge_publication_rows(owner_rows, instance_rows)


def save_published_parameters_to_sqlite(
    db_path,
    document_id,
    drawing_part_number,
    root_metadata,
    target_part_number,
    target_instance_name,
    target_tree_path,
    rows,
):
    resolved_db_path = Path(db_path).resolve()
    conn = connect_db(str(resolved_db_path))
    try:
        create_tables(conn)
        migrate_tables(conn)
        conn.execute(
            """
            DELETE FROM catia_published_parameters
            WHERE document_id = ? AND catia_part_number = ?
            """,
            (document_id, target_part_number),
        )
        extracted_at = utc_now_iso()
        for row in rows:
            conn.execute(
                """
                INSERT INTO catia_published_parameters (
                    document_id,
                    drawing_part_number,
                    catia_part_number,
                    catia_instance_name,
                    catia_tree_path,
                    publication_name,
                    parameter_path,
                    parameter_name,
                    value_text,
                    formula_text,
                    active_text,
                    user_access_mode,
                    parameter_type,
                    source_document_name,
                    source_document_path,
                    source_document_type,
                    extracted_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    drawing_part_number,
                    target_part_number,
                    target_instance_name,
                    target_tree_path,
                    row.get("publication_name"),
                    row.get("parameter_path"),
                    row.get("parameter_name"),
                    row.get("value_text"),
                    row.get("formula_text"),
                    row.get("active_text"),
                    row.get("user_access_mode"),
                    row.get("parameter_type"),
                    root_metadata.get("document_name"),
                    root_metadata.get("document_path"),
                    root_metadata.get("document_type"),
                    extracted_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return resolved_db_path


def run_layer2_parameter_extraction_stage(
    db_path,
    target_part_number,
    drawing_document_id=None,
    drawing_part_number=None,
    root_document_path=None,
    summary_json=None,
    progress_callback=None,
):
    resolved_target_part_number = safe_text(target_part_number)
    if not resolved_target_part_number:
        raise RuntimeError("A CATIA target part number is required.")

    drawing_identity = load_drawing_identity_from_sqlite(db_path, document_id=drawing_document_id)
    resolved_document_id = safe_text(drawing_document_id) or drawing_identity.get("document_id")
    resolved_drawing_part_number = safe_text(drawing_part_number) or drawing_identity.get("part_number") or resolved_document_id

    catia = connect_catia()
    root_document, opened_here = resolve_document(catia, root_document_path)
    try:
        if progress_callback:
            progress_callback(
                "Layer 2 Stage 01/03 - CATIA Published Parameter Extraction: locating {} in CATIA, opening the target part context, and reading published parameters".format(
                    resolved_target_part_number
                )
            )
        root_metadata = get_document_metadata(root_document)
        root_product = get_document_product(root_document)
        matches = resolve_target_matches(root_product, resolved_target_part_number)
        if not matches:
            raise RuntimeError(
                "Could not find a CATIA part inside the active product with part number '{}'.".format(
                    resolved_target_part_number
                )
            )

        target = matches[0]
        owner_product = safe_com_get(target["product"], "ReferenceProduct") or target["product"]
        publication_count, rows = extract_publication_rows(owner_product, instance_product=target["product"])
        if progress_callback:
            progress_callback(
                "Layer 2 Stage 02/03 - CATIA Published Parameter Output Writer: saving {} published parameter(s) and CATIA metadata to SQLite{}".format(
                    len(rows),
                    " and JSON summary" if summary_json else "",
                )
            )
        resolved_db_path = save_published_parameters_to_sqlite(
            db_path=db_path,
            document_id=resolved_document_id,
            drawing_part_number=resolved_drawing_part_number,
            root_metadata=root_metadata,
            target_part_number=safe_text(target.get("part_number")) or resolved_target_part_number,
            target_instance_name=target.get("instance_name"),
            target_tree_path=target.get("tree_path"),
            rows=rows,
        )

        summary = {
            "status": "completed",
            "db_path": str(resolved_db_path),
            "table_name": "catia_published_parameters",
            "drawing_document_id": resolved_document_id,
            "drawing_part_number": resolved_drawing_part_number,
            "target_part_number": safe_text(target.get("part_number")) or resolved_target_part_number,
            "target_instance_name": target.get("instance_name"),
            "target_tree_path": target.get("tree_path"),
            "matched_instance_count": len(matches),
            "matched_tree_paths": [match.get("tree_path") for match in matches],
            "publication_count": publication_count,
            "published_parameter_count": len(rows),
            "source_document": root_metadata,
            "parameters": rows,
        }
        if summary_json:
            summary_path = Path(summary_json).resolve()
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            summary["summary_json"] = str(summary_path)
        return summary
    finally:
        if opened_here and root_document is not None:
            safe_com_call(root_document, "Close")


# Backward-compatible alias for existing integrations.
extract_published_parameters_to_sqlite = run_layer2_parameter_extraction_stage


def main():
    args = parse_args()
    summary = run_layer2_parameter_extraction_stage(
        db_path=args.db,
        target_part_number=args.part_number,
        drawing_document_id=args.document_id,
        drawing_part_number=args.part_number,
        root_document_path=args.root_document,
        summary_json=args.summary_json,
    )
    print(
        "CATIA published-parameter agent completed. Stored {} published parameter(s) for part {} in {}.".format(
            summary.get("published_parameter_count", 0),
            summary.get("target_part_number"),
            summary.get("db_path"),
        )
    )


if __name__ == "__main__":
    main()
