import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

try:
    from .user_parameter_agent import (
        build_document_file_index,
        connect_catia,
        get_catia_document_kind,
        get_document_metadata,
        iter_com_collection,
        normalize_identity_key,
        resolve_source_file_for_node,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_text,
    )
except ImportError:
    from openai_pipeline.pipeline_stack.catia_agents.user_parameter_agent import (
        build_document_file_index,
        connect_catia,
        get_catia_document_kind,
        get_document_metadata,
        iter_com_collection,
        normalize_identity_key,
        resolve_source_file_for_node,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_text,
    )


@dataclass
class LengthParameterSpec:
    name: str
    value_mm: float
    source_dimension_id: str | None = None
    source_nominal_value: float | None = None
    source_tolerance_plus: float | None = None
    source_tolerance_minus: float | None = None
    source_mode: str = "direct"


def parse_parameter_names(text):
    names = []
    for raw in str(text or "").split(","):
        name = raw.strip()
        if not name:
            continue
        names.append(name)
    if not names:
        raise RuntimeError("Provide at least one parameter name, for example: D1,D2,D3,D4")
    return names


def parse_parameter_specs(parameter_names, value_mm, explicit_values):
    if explicit_values:
        specs = []
        for raw in str(explicit_values).split(","):
            piece = raw.strip()
            if not piece:
                continue
            if "=" not in piece:
                raise RuntimeError("Invalid --values item: {}. Use NAME=VALUE_MM, for example D1=20".format(piece))
            name, value_text = piece.split("=", 1)
            try:
                resolved_value = float(value_text.strip())
            except ValueError as exc:
                raise RuntimeError("Invalid numeric value in --values item: {}".format(piece)) from exc
            specs.append(LengthParameterSpec(name=name.strip(), value_mm=resolved_value))
        if not specs:
            raise RuntimeError("No valid parameter specs were found in --values.")
        return specs

    resolved_value_mm = float(value_mm)
    return [LengthParameterSpec(name=name, value_mm=resolved_value_mm) for name in parse_parameter_names(parameter_names)]


def normalize_dimension_parameter_name(dimension_id):
    text = str(dimension_id or "").strip()
    if not text:
        raise RuntimeError("Encountered an empty dimension_id in the SQLite data.")
    return text.upper()


def normalize_tolerance_mode(mode):
    text = str(mode or "nominal").strip().lower()
    alias_map = {
        "maximum": "max",
        "minimum": "min",
        "maxiumum": "max",
    }
    return alias_map.get(text, text)


def choose_tolerance_mode(mode):
    normalized = normalize_tolerance_mode(mode)
    if normalized in ("nominal", "max", "min"):
        return normalized

    if normalized != "ask":
        raise RuntimeError("Unsupported tolerance mode: {}. Use nominal, max, min, or ask.".format(mode))

    import sys

    if not sys.stdin.isatty():
        return "nominal"

    print("")
    print("CATIA parameter target:")
    print("  1. Nominal (default) - update D1, D2, D3 using nominal_value")
    print("  2. Maximum - update D1, D2, D3 using nominal + upper tolerance")
    print("  3. Minimum - update D1, D2, D3 using nominal + lower tolerance")
    while True:
        try:
            choice = input("Choose parameter target [1/nominal, 2/max, 3/min, default nominal]: ").strip().lower()
        except EOFError:
            return "nominal"
        if choice in ("", "1", "n", "nominal"):
            return "nominal"
        if choice in ("2", "x", "max", "maximum"):
            return "max"
        if choice in ("3", "m", "min", "minimum"):
            return "min"
        print("Please enter 1/nominal, 2/max, or 3/min.")


def build_dimension_parameter_specs(dimension_id, nominal_value, tolerance_plus, tolerance_minus, tolerance_mode):
    base_name = normalize_dimension_parameter_name(dimension_id)
    nominal_mm = float(nominal_value)
    plus_value = None if tolerance_plus is None else float(tolerance_plus)
    minus_value = None if tolerance_minus is None else float(tolerance_minus)
    max_mm = nominal_mm + (plus_value if plus_value is not None else 0.0)
    min_mm = nominal_mm + (minus_value if minus_value is not None else 0.0)

    common_kwargs = {
        "source_dimension_id": str(dimension_id).strip(),
        "source_nominal_value": nominal_mm,
        "source_tolerance_plus": plus_value,
        "source_tolerance_minus": minus_value,
    }

    if tolerance_mode == "nominal":
        return [LengthParameterSpec(name=base_name, value_mm=nominal_mm, source_mode="nominal", **common_kwargs)]
    if tolerance_mode == "max":
        return [LengthParameterSpec(name=base_name, value_mm=max_mm, source_mode="max", **common_kwargs)]
    if tolerance_mode == "min":
        return [LengthParameterSpec(name=base_name, value_mm=min_mm, source_mode="min", **common_kwargs)]
    raise RuntimeError("Unsupported tolerance mode: {}".format(tolerance_mode))


def load_parameter_specs_from_sqlite(db_path, document_id=None, limit=None, tolerance_mode="nominal", approved_only=False):
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.exists():
        raise RuntimeError("SQLite database not found: {}".format(resolved_db_path))

    conn = sqlite3.connect(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT
                dimension_id,
                nominal_value,
                tolerance_plus,
                tolerance_minus,
                raw_text
            FROM dimensions
            WHERE nominal_value IS NOT NULL
        """
        params = []
        if document_id:
            sql += " AND document_id = ?"
            params.append(document_id)
        if approved_only:
            sql += " AND human_review_status = ?"
            params.append("approved")
        sql += """
            ORDER BY
                CASE
                    WHEN dimension_id GLOB 'd[0-9]*' THEN CAST(SUBSTR(dimension_id, 2) AS INTEGER)
                    ELSE 999999
                END,
                dimension_id
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            if approved_only:
                raise RuntimeError(
                    "No approved dimensions with nominal_value were found in {}. Complete human review and approve at least one dimension first.".format(
                        resolved_db_path
                    )
                )
            raise RuntimeError("No dimensions with nominal_value were found in {}".format(resolved_db_path))

        specs = []
        for row in rows:
            specs.extend(
                build_dimension_parameter_specs(
                    row["dimension_id"],
                    row["nominal_value"],
                    row["tolerance_plus"],
                    row["tolerance_minus"],
                    normalize_tolerance_mode(tolerance_mode),
                )
            )
        return specs
    finally:
        conn.close()


def load_source_document_identity_from_sqlite(db_path, document_id=None):
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.exists():
        raise RuntimeError("SQLite database not found: {}".format(resolved_db_path))

    conn = sqlite3.connect(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT
                document_id,
                part_number,
                document_number
            FROM documents
        """
        params = []
        if document_id:
            sql += " WHERE document_id = ?"
            params.append(document_id)
        sql += " ORDER BY document_id LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise RuntimeError("No matching document row was found in {}".format(resolved_db_path))
        return {
            "document_id": row["document_id"],
            "part_number": row["part_number"],
            "document_number": row["document_number"],
        }
    finally:
        conn.close()


def get_part_from_document(document):
    part = safe_com_get(document, "Part")
    if part is None:
        document_type = safe_text(safe_com_get(document, "Type")) or "unknown"
        raise RuntimeError(
            "This agent only works with CATPart documents. Active document type was {}.".format(document_type)
        )
    return part


def get_parameters_collection(part):
    parameters = safe_com_get(part, "Parameters")
    if parameters is None:
        raise RuntimeError("CATIA Part.Parameters is not available on this document.")
    return parameters


def find_parameter_by_name(parameters, parameter_name):
    normalized_name = str(parameter_name).strip().lower()
    for parameter in iter_com_collection(parameters):
        name = safe_text(safe_com_get(parameter, "Name")) or ""
        if name.strip().lower() == normalized_name or name.split("\\")[-1].strip().lower() == normalized_name:
            return parameter
    return None


def create_length_parameter(parameters, parameter_name):
    parameter = safe_com_call(parameters, "CreateDimension", parameter_name, "LENGTH", 0.0)
    if parameter is None:
        raise RuntimeError("CATIA could not create length parameter {}.".format(parameter_name))
    return parameter


def set_length_parameter_value(parameter, value_mm):
    value_text = "{}mm".format(value_mm if int(value_mm) != value_mm else int(value_mm))
    updated = safe_com_call(parameter, "ValuateFromString", value_text)
    if updated is None:
        # Some COM calls return None on success too, so verify by reading back later.
        return value_text
    return value_text


def read_parameter_value_text(parameter):
    for attr in ("ValueAsString", "Value"):
        value = safe_com_call(parameter, attr)
        text = safe_text(value)
        if text:
            return text

        value = safe_com_get(parameter, attr)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        text = safe_text(value)
        if text:
            return text
    return None


def create_or_update_length_parameters(document, specs):
    part = get_part_from_document(document)
    parameters = get_parameters_collection(part)
    changes = []

    for spec in specs:
        existing = find_parameter_by_name(parameters, spec.name)
        created = existing is None
        parameter = existing or create_length_parameter(parameters, spec.name)
        assigned_text = set_length_parameter_value(parameter, spec.value_mm)
        changes.append(
            {
                "name": spec.name,
                "value_mm": spec.value_mm,
                "assigned_text": assigned_text,
                "resolved_value": read_parameter_value_text(parameter),
                "action": "created" if created else "updated",
                "source_dimension_id": spec.source_dimension_id,
                "source_nominal_value": spec.source_nominal_value,
                "source_tolerance_plus": spec.source_tolerance_plus,
                "source_tolerance_minus": spec.source_tolerance_minus,
                "source_mode": spec.source_mode,
            }
        )

    safe_com_call(part, "Update")
    return changes


def save_document(document):
    safe_com_call(document, "Save")


def find_open_part_document_by_identity(catia, identity_value):
    normalized_identity = normalize_identity_key(identity_value)
    if not normalized_identity:
        return None

    documents = safe_com_get(catia, "Documents")
    for document in iter_com_collection(documents):
        if get_catia_document_kind(document) != "part":
            continue
        document_name = safe_text(safe_com_get(document, "Name"))
        full_name = safe_text(safe_com_get(document, "FullName"))
        stem = Path(full_name).stem if full_name else document_name
        if normalize_identity_key(stem) == normalized_identity:
            return document
    return None


def resolve_target_part_document(catia, root_document, target_identity):
    root_type = get_catia_document_kind(root_document)
    root_path = safe_text(safe_com_get(root_document, "FullName"))

    if root_type == "part":
        return root_document, False, root_path

    open_match = find_open_part_document_by_identity(catia, target_identity)
    if open_match is not None:
        return open_match, False, safe_text(safe_com_get(open_match, "FullName"))

    if not root_path:
        raise RuntimeError(
            "The active CATIA product does not have a saved file path, so the agent could not locate the target CATPart in the background."
        )

    file_index = build_document_file_index(root_path)
    resolved_file = resolve_source_file_for_node("part", target_identity, target_identity, file_index)
    if resolved_file is None:
        raise RuntimeError(
            "Could not find a CATPart in the product folder matching part/document number {}.".format(target_identity)
        )

    document, opened_here = resolve_document(catia, str(resolved_file))
    return document, opened_here, str(resolved_file.resolve())


def sync_parameters_from_dimension_db(
    dimension_db,
    root_document_path=None,
    dimension_document_id=None,
    dimension_limit=None,
    target_identity=None,
    summary_json=None,
    tolerance_mode="nominal",
    approved_only=False,
):
    source_identity = load_source_document_identity_from_sqlite(dimension_db, document_id=dimension_document_id)
    resolved_target_identity = (
        safe_text(target_identity)
        or safe_text(source_identity.get("part_number"))
        or safe_text(source_identity.get("document_number"))
        or safe_text(source_identity.get("document_id"))
    )
    if not resolved_target_identity:
        raise RuntimeError("Could not determine the target part identity from the SQLite data.")

    specs = load_parameter_specs_from_sqlite(
        dimension_db,
        document_id=dimension_document_id or source_identity.get("document_id"),
        limit=dimension_limit,
        tolerance_mode=tolerance_mode,
        approved_only=approved_only,
    )

    catia = connect_catia()
    root_document, root_opened_here = resolve_document(catia, root_document_path)
    target_document = None
    target_opened_here = False
    try:
        target_document, target_opened_here, resolved_part_path = resolve_target_part_document(
            catia,
            root_document,
            resolved_target_identity,
        )
        metadata = get_document_metadata(target_document)
        changes = create_or_update_length_parameters(target_document, specs)
        save_document(target_document)
        summary = {
            "document": metadata,
            "target_identity": resolved_target_identity,
            "resolved_part_path": resolved_part_path,
            "parameter_count": len(changes),
            "tolerance_mode": tolerance_mode,
            "approved_only": bool(approved_only),
            "source_dimension_db": str(Path(dimension_db).resolve()),
            "source_dimension_document_id": dimension_document_id or source_identity.get("document_id"),
            "source_document_identity": source_identity,
            "parameters": changes,
        }
        if summary_json:
            summary_path = Path(summary_json).resolve()
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary
    finally:
        if target_opened_here and target_document is not None and target_document is not root_document:
            safe_com_call(target_document, "Close")
        if root_opened_here and root_document is not None:
            safe_com_call(root_document, "Close")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create or update CATIA CATPart length parameters such as D1, D2, D3 with nominal, maximum, minimum, or manual values in mm."
    )
    parser.add_argument(
        "--document",
        default=None,
        type=str,
        help="Optional CATPart path. If omitted, the agent uses CATIA ActiveDocument.",
    )
    parser.add_argument(
        "--dimension_db",
        default=None,
        type=str,
        help="Optional SQLite drawing_data.db path. When used, parameters are created from dimensions.dimension_id and nominal_value.",
    )
    parser.add_argument(
        "--dimension_document_id",
        default=None,
        type=str,
        help="Optional document_id filter when reading dimensions from --dimension_db.",
    )
    parser.add_argument(
        "--dimension_limit",
        default=None,
        type=int,
        help="Optional limit when reading dimensions from --dimension_db.",
    )
    parser.add_argument(
        "--target_identity",
        default=None,
        type=str,
        help="Optional CATIA part identity override. Defaults to part_number/document_id from the dimension DB.",
    )
    parser.add_argument(
        "--tolerance_mode",
        choices=("ask", "nominal", "max", "maximum", "min", "minimum"),
        default="ask",
        type=str,
        help="When reading from --dimension_db, choose whether CATIA gets nominal values, maximum values, or minimum values.",
    )
    parser.add_argument(
        "--approved_only",
        default=False,
        action="store_true",
        help="When reading from --dimension_db, only sync dimensions whose human_review_status is approved.",
    )
    parser.add_argument(
        "--parameter_names",
        default="D1,D2,D3,D4",
        type=str,
        help="Comma-separated parameter names to create with the same value. Example: D1,D2,D3,D4",
    )
    parser.add_argument(
        "--value_mm",
        default=20.0,
        type=float,
        help="Length value in mm assigned to each parameter when --values is not used.",
    )
    parser.add_argument(
        "--values",
        default=None,
        type=str,
        help="Optional explicit per-parameter values. Example: D1=20,D2=35.5,D3=10",
    )
    parser.add_argument(
        "--summary_json",
        default=None,
        type=str,
        help="Optional JSON summary output path.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.dimension_db:
        tolerance_mode = choose_tolerance_mode(args.tolerance_mode)
        summary = sync_parameters_from_dimension_db(
            args.dimension_db,
            root_document_path=args.document,
            dimension_document_id=args.dimension_document_id,
            dimension_limit=args.dimension_limit,
            target_identity=args.target_identity,
            summary_json=args.summary_json,
            tolerance_mode=tolerance_mode,
            approved_only=args.approved_only,
        )
    else:
        specs = parse_parameter_specs(args.parameter_names, args.value_mm, args.values)
        catia = connect_catia()
        document, opened_here = resolve_document(catia, args.document)

        try:
            metadata = get_document_metadata(document)
            changes = create_or_update_length_parameters(document, specs)
            save_document(document)
            summary = {
                "document": metadata,
                "parameter_count": len(changes),
                "tolerance_mode": "direct",
                "source_dimension_db": None,
                "source_dimension_document_id": None,
                "parameters": changes,
            }

            if args.summary_json:
                summary_path = Path(args.summary_json).resolve()
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        finally:
            if opened_here:
                safe_com_call(document, "Close")

    metadata = summary.get("document", {})
    changes = summary.get("parameters", [])
    print("CATIA length-parameter agent completed.")
    print("Document: {}".format(metadata.get("document_name") or "unknown"))
    print("Document type: {}".format(metadata.get("document_type") or "unknown"))
    if summary.get("tolerance_mode"):
        print("Tolerance mode: {}".format(summary["tolerance_mode"]))
    if summary.get("resolved_part_path"):
        print("Resolved part path: {}".format(summary["resolved_part_path"]))
    for change in changes:
        print(
            "{}: {} -> {}".format(
                change["action"].capitalize(),
                change["name"],
                change["resolved_value"] or change["assigned_text"],
            )
        )
    return summary


if __name__ == "__main__":
    main()
