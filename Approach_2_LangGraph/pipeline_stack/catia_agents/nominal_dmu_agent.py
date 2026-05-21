import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from pycatia.enumeration.enumeration_types import cat_clash_computation_type as CatClashComputationType
    from pycatia.enumeration.enumeration_types import cat_clash_interference_type as CatClashInterferenceType
    from pycatia.enumeration.enumeration_types import cat_conflict_comparison as CatConflictComparison
    from pycatia.enumeration.enumeration_types import cat_conflict_status as CatConflictStatus
    from pycatia.enumeration.enumeration_types import cat_conflict_type as CatConflictType
    from pycatia.product_structure_interfaces.product import Product
    from pycatia.space_analyses_interfaces.clashes import Clashes
    from pycatia.space_analyses_interfaces.conflict import Conflict
    from pycatia.navigator_interfaces.groups import Groups
except ImportError:
    from pycatia.enumeration.enums import (
        CatClashComputationType,
        CatClashInterferenceType,
        CatConflictComparison,
        CatConflictStatus,
        CatConflictType,
    )
    from pycatia.product_structure_interfaces.product import Product
    from pycatia.space_analyses_interfaces.clashes import Clashes
    from pycatia.space_analyses_interfaces.conflict import Conflict
    from pycatia.navigator_interfaces.groups import Groups

try:
    from .user_parameter_agent import (
        as_dynamic_dispatch,
        collect_selectable_targets,
        connect_catia,
        get_document_product,
        iter_com_collection,
        normalize_identity_key,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_int,
        safe_text,
        utc_now_iso,
    )
except ImportError:
    try:
        from openai_pipeline.pipeline_stack.catia_agents.user_parameter_agent import (
            as_dynamic_dispatch,
            collect_selectable_targets,
            connect_catia,
            get_document_product,
            iter_com_collection,
            normalize_identity_key,
            resolve_document,
            safe_com_call,
            safe_com_get,
            safe_int,
            safe_text,
            utc_now_iso,
        )
    except ImportError:
        PIPELINE_STACK_ROOT = Path(__file__).resolve().parents[1]
        if str(PIPELINE_STACK_ROOT) not in sys.path:
            sys.path.insert(0, str(PIPELINE_STACK_ROOT))
        from catia_agents.user_parameter_agent import (
            as_dynamic_dispatch,
            collect_selectable_targets,
            connect_catia,
            get_document_product,
            iter_com_collection,
            normalize_identity_key,
            resolve_document,
            safe_com_call,
            safe_com_get,
            safe_int,
            safe_text,
            utc_now_iso,
        )

try:
    from ..helpers.sqlite_importer import import_catia_dmu_results_to_sqlite
except ImportError:
    try:
        from openai_pipeline.pipeline_stack.helpers.sqlite_importer import import_catia_dmu_results_to_sqlite
    except ImportError:
        PIPELINE_STACK_ROOT = Path(__file__).resolve().parents[1]
        if str(PIPELINE_STACK_ROOT) not in sys.path:
            sys.path.insert(0, str(PIPELINE_STACK_ROOT))
        from helpers.sqlite_importer import import_catia_dmu_results_to_sqlite


MODE_SELECTION_AGAINST_ALL = "selection_against_all"
ANALYSIS_TYPE_LABEL = "contact+clash+clearance"
DMU_WORKBENCH_CANDIDATES = (
    "DMUSpaceAnalysisWorkbench",
    "SPAWorkbench",
)


@dataclass
class ConflictRecord:
    number: int
    product1: str | None
    product2: str | None
    conflict_type: str
    value: float
    status: str | None
    info: str | None
    keep: str | None
    comment: str | None
    location: str | None
    image_path: str | None


@dataclass
class PreparedScope:
    mode: str
    first_group: object | None
    second_group: object | None
    selected_targets: list
    comparison_targets: list
    temporary_group_names: list
    all_targets: list


def _print(message, progress_callback=None):
    if progress_callback:
        progress_callback(message)
    else:
        print(message)


def safe_bool_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = safe_text(value)
    return text.lower() if text else None


def parse_args():
    parser = argparse.ArgumentParser(description="Run nominal CATIA DMU clash/contact/clearance analysis.")
    parser.add_argument("--document", default=None, type=str, help="Optional CATIA root document path (.CATProduct/.CATPart).")
    parser.add_argument("--part_number", required=True, type=str, help="Part number for selection-against-all mode.")
    parser.add_argument("--clearance_mm", default=5.0, type=float, help="Clearance threshold in millimeters.")
    parser.add_argument("--output_dir", default="Results/dmu_agent", type=str, help="Base output folder for DMU outputs.")
    parser.add_argument("--show_rows", default=12, type=int, help="How many rows to print in the terminal summary table.")
    return parser.parse_args()


def enum_member_value(enum_source, member_name):
    try:
        return int(getattr(enum_source, member_name))
    except Exception:
        pass
    if isinstance(enum_source, (list, tuple)):
        try:
            return enum_source.index(member_name)
        except ValueError:
            pass
    raise AttributeError("Could not resolve enum member {} from {}".format(member_name, enum_source))


def normalize_part_number(text):
    normalized = normalize_identity_key(text) or safe_text(text)
    if not normalized:
        return None
    return normalized.strip().lower()


def get_document_summary(document):
    return {
        "name": safe_text(safe_com_get(document, "Name")) or "unknown",
        "type": safe_text(safe_com_get(document, "Type")) or "unknown",
        "path": safe_text(safe_com_get(document, "FullName")) or "",
        "analysis_mode": MODE_SELECTION_AGAINST_ALL,
    }


def get_current_workbench_id(catia):
    return safe_text(safe_com_call(catia, "GetWorkbenchId"))


def start_dmu_workbench(catia):
    for workbench_name in DMU_WORKBENCH_CANDIDATES:
        safe_com_call(catia, "StartWorkbench", workbench_name)
        current = get_current_workbench_id(catia)
        if current:
            return workbench_name, current
    raise RuntimeError(
        "Could not open a DMU workbench. Tried: {}.".format(", ".join(DMU_WORKBENCH_CANDIDATES))
    )


def wrap_dynamic(com_object):
    try:
        import win32com.client.dynamic

        return win32com.client.dynamic.DumbDispatch(com_object)
    except Exception:
        return as_dynamic_dispatch(com_object)


def get_clashes_collection(root_product):
    clashes_obj = safe_com_call(root_product, "GetTechnologicalObject", "Clashes")
    if clashes_obj is None:
        raise RuntimeError('CATIA could not retrieve technological object "Clashes".')
    return Clashes(wrap_dynamic(clashes_obj))


def get_groups_collection(root_product):
    groups_obj = safe_com_call(root_product, "GetTechnologicalObject", "Groups")
    if groups_obj is None:
        raise RuntimeError('CATIA could not retrieve technological object "Groups".')
    return Groups(wrap_dynamic(groups_obj))


def get_root_tree_path(root_product, document_summary):
    return safe_text(safe_com_get(root_product, "Name")) or document_summary["name"] or "ROOT_PRODUCT"


def collect_part_targets(root_product, root_tree_path):
    targets = collect_selectable_targets(root_product, root_tree_path, depth=0, include_self=True)
    return [target for target in targets if target.node_kind == "part"]


def iter_target_identity_candidates(target):
    seen = set()
    values = [
        safe_text(getattr(target, "part_number", None)),
        safe_text(getattr(target, "display_name", None)),
        safe_text(getattr(target, "instance_name", None)),
        safe_text(getattr(target, "tree_path", None)).rsplit(" > ", 1)[-1]
        if safe_text(getattr(target, "tree_path", None))
        else None,
    ]
    for value in values:
        if not value:
            continue
        normalized = normalize_part_number(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            yield normalized

        pieces = str(value).split()
        if pieces:
            first_token = normalize_part_number(pieces[0])
            if first_token and first_token not in seen:
                seen.add(first_token)
                yield first_token


def choose_target_from_matches(label, matches):
    if len(matches) == 1:
        return matches[0]
    if not sys.stdin.isatty():
        raise RuntimeError(
            "{} part number matched {} CATIA instances, and no interactive terminal was available.".format(
                label,
                len(matches),
            )
        )

    print("")
    print("{} matches:".format(label))
    for index, target in enumerate(matches, start=1):
        print(
            "  {}. {} | instance={} | tree_path={}".format(
                index,
                target.display_name or target.part_number or target.tree_path,
                target.instance_name or "unknown",
                target.tree_path,
            )
        )

    while True:
        raw = input("Choose {} instance [1-{}]: ".format(label.lower(), len(matches))).strip()
        try:
            selected_index = int(raw)
        except Exception:
            selected_index = None
        if selected_index is not None and 1 <= selected_index <= len(matches):
            return matches[selected_index - 1]
        print("Please enter a valid instance number.")


def find_target_by_part_number(targets, part_number, label):
    normalized = normalize_part_number(part_number)
    matches = [
        target
        for target in targets
        if normalized in set(iter_target_identity_candidates(target))
    ]
    if not matches:
        raise RuntimeError("No part instance found for {} part number '{}'.".format(label, part_number))
    return choose_target_from_matches(label, matches)


def create_group_from_targets(groups_collection, targets, group_name):
    group = groups_collection.add()
    group.name = group_name
    group.extract_mode = 1
    for target in targets:
        group.add_explicit(Product(as_dynamic_dispatch(target.product)))
    return group


def build_scope_for_selection_against_all(root_product, document_summary, part_number):
    root_tree_path = get_root_tree_path(root_product, document_summary)
    part_targets = collect_part_targets(root_product, root_tree_path)
    groups_collection = get_groups_collection(root_product)
    selected_target = find_target_by_part_number(part_targets, part_number, "Selection")
    comparison_targets = [target for target in part_targets if target.tree_path != selected_target.tree_path]
    if not comparison_targets:
        raise RuntimeError("No comparison part targets remain after excluding the selected component.")

    first_group = create_group_from_targets(groups_collection, [selected_target], "DMU_SELECTED")
    return PreparedScope(
        mode=MODE_SELECTION_AGAINST_ALL,
        first_group=first_group,
        second_group=None,
        selected_targets=[selected_target],
        comparison_targets=comparison_targets,
        temporary_group_names=[first_group.name],
        all_targets=part_targets,
    )


def resolve_computation_type():
    return enum_member_value(CatClashComputationType, "catClashComputationTypeAgainstAll")


def get_conflicts_from_clash(clash_object):
    conflicts = None
    for attr in ("conflicts", "Conflicts"):
        try:
            conflicts = getattr(clash_object, attr)
            if callable(conflicts):
                conflicts = conflicts()
        except Exception:
            conflicts = None
        if conflicts is not None:
            break
    if conflicts is None:
        raw_clash = getattr(clash_object, "com_object", None) or clash_object
        conflicts = safe_com_get(raw_clash, "Conflicts")
    return conflicts


def run_contact_plus_clash(clashes_collection, first_group, second_group=None):
    clash = clashes_collection.add()
    clash.name = "DMU_CONTACT_PLUS_CLASH"
    clash.computation_type = resolve_computation_type()
    clash.interference_type = enum_member_value(CatClashInterferenceType, "catClashInterferenceTypeContact")
    if first_group is not None:
        clash.first_group = first_group
    if second_group is not None:
        clash.second_group = second_group
    clash.compute()
    return clash


def run_clearance(clashes_collection, clearance_mm, first_group, second_group=None):
    clash = clashes_collection.add()
    clash.name = "DMU_CLEARANCE"
    clash.computation_type = resolve_computation_type()
    clash.interference_type = enum_member_value(CatClashInterferenceType, "catClashInterferenceTypeClearance")
    clash.clearance = float(clearance_mm)
    if first_group is not None:
        clash.first_group = first_group
    if second_group is not None:
        clash.second_group = second_group
    clash.compute()
    return clash


def get_conflict_com_object(conflict):
    return getattr(conflict, "com_object", None) or conflict


def map_conflict_type(conflict_type):
    mapping = {
        enum_member_value(CatConflictType, "catConflictTypeClash"): "clash",
        enum_member_value(CatConflictType, "catConflictTypeContact"): "contact",
        enum_member_value(CatConflictType, "catConflictTypeClearance"): "clearance",
    }
    try:
        return mapping.get(int(conflict_type), "unknown")
    except Exception:
        return "unknown"


def map_conflict_status(conflict_status):
    try:
        normalized = int(conflict_status)
    except Exception:
        return "Unknown"
    mapping = {
        enum_member_value(CatConflictStatus, "catConflictStatusNotInspected"): "Not inspected",
        enum_member_value(CatConflictStatus, "catConflictStatusRelevant"): "Relevant",
        enum_member_value(CatConflictStatus, "catConflictStatusIrrelevant"): "Irrelevant",
        enum_member_value(CatConflictStatus, "catConflictStatusSolved"): "Solved",
    }
    return mapping.get(normalized, "Unknown")


def map_conflict_comparison_info(comparison_info):
    try:
        normalized = int(comparison_info)
    except Exception:
        return "Unknown"
    mapping = {
        enum_member_value(CatConflictComparison, "catConflictComparisonNew"): "New",
        enum_member_value(CatConflictComparison, "catConflictComparisonOld"): "Old",
        enum_member_value(CatConflictComparison, "catConflictComparisonNo"): "No",
    }
    return mapping.get(normalized, "Unknown")


def safe_conflict_type(conflict):
    try:
        return int(conflict.type)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        value = safe_com_get(raw_conflict, "Type")
        try:
            return int(value)
        except Exception:
            return None


def safe_conflict_status(conflict):
    try:
        return int(conflict.status)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        return safe_com_get(raw_conflict, "Status")


def safe_conflict_comparison_info(conflict):
    try:
        return int(conflict.comparison_info)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        return safe_com_get(raw_conflict, "ComparisonInfo")


def safe_conflict_value(conflict):
    try:
        return float(conflict.value)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        value = safe_com_get(raw_conflict, "Value", 0.0)
        try:
            return float(value)
        except Exception:
            return 0.0


def safe_conflict_comment(conflict):
    try:
        return safe_text(conflict.comment)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        return safe_text(safe_com_get(raw_conflict, "Comment"))


def safe_conflict_keep(conflict):
    try:
        value = getattr(conflict, "keep")
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        value = safe_com_get(raw_conflict, "Keep")
    text = safe_bool_text(value)
    return text if text is not None else safe_text(value)


def safe_conflict_location(conflict):
    try:
        return safe_text(getattr(conflict, "location"))
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        value = safe_com_get(raw_conflict, "Location")
        if value is not None:
            return safe_text(value)
        first_product = safe_conflict_product(conflict, "first")
        second_product = safe_conflict_product(conflict, "second")
        if first_product is None and second_product is None:
            return None
        return "{} -> {}".format(
            resolve_instance_name(first_product) or "?",
            resolve_instance_name(second_product) or "?",
        )


def safe_conflict_product(conflict, side):
    property_name = "first_product" if side == "first" else "second_product"
    com_name = "FirstProduct" if side == "first" else "SecondProduct"
    try:
        return getattr(conflict, property_name)
    except Exception:
        raw_conflict = get_conflict_com_object(conflict)
        raw_product = safe_com_get(raw_conflict, com_name)
        if raw_product is None:
            return None
        try:
            return Product(raw_product)
        except Exception:
            return raw_product


def resolve_instance_name(product_obj):
    try:
        name_value = getattr(product_obj, "name")
        if callable(name_value):
            name_value = name_value()
        text = safe_text(name_value)
        if text:
            return text
    except Exception:
        pass
    return (
        safe_text(safe_com_get(product_obj, "Name"))
        or safe_text(safe_com_get(product_obj, "PartNumber"))
        or safe_text(product_obj)
    )


def build_info_text(conflict):
    return map_conflict_comparison_info(safe_conflict_comparison_info(conflict))


def infer_conflict_type(raw_conflict_type, conflict_value, source_label):
    resolved_type = map_conflict_type(raw_conflict_type)
    if resolved_type in ("contact", "clash", "clearance"):
        return resolved_type
    if source_label == "clearance":
        return "clearance"
    return "contact" if abs(float(conflict_value or 0.0)) <= 1e-9 else "clash"


def iter_conflict_items(conflicts):
    if conflicts is None:
        return
    count = safe_int(getattr(conflicts, "count", None), default=None)
    if count is not None and hasattr(conflicts, "item"):
        for index in range(1, count + 1):
            try:
                yield conflicts.item(index)
            except Exception:
                continue
        return
    for conflict in iter_com_collection(conflicts):
        yield conflict


def collect_conflict_records(contact_clash, clearance_clash):
    records = []
    number = 1
    sources = (
        (contact_clash, None, "contact+clash"),
        (clearance_clash, "clearance", "clearance"),
    )
    for clash_object, required_type, source_label in sources:
        conflicts = get_conflicts_from_clash(clash_object)
        if conflicts is None:
            continue
        for conflict in iter_conflict_items(conflicts):
            conflict_value = safe_conflict_value(conflict)
            conflict_type = infer_conflict_type(safe_conflict_type(conflict), conflict_value, source_label)
            if required_type is not None and conflict_type != required_type:
                continue
            first_product = safe_conflict_product(conflict, "first")
            second_product = safe_conflict_product(conflict, "second")
            records.append(
                ConflictRecord(
                    number=number,
                    product1=resolve_instance_name(first_product),
                    product2=resolve_instance_name(second_product),
                    conflict_type=conflict_type,
                    value=conflict_value,
                    status=map_conflict_status(safe_conflict_status(conflict)),
                    info=build_info_text(conflict),
                    keep=safe_conflict_keep(conflict) or "",
                    comment=safe_conflict_comment(conflict) or "",
                    location=safe_conflict_location(conflict) or "",
                    image_path="",
                )
            )
            number += 1
    return records


def create_output_paths(base_output_dir, document_name):
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    run_name = "{}__{}".format(Path(document_name).stem, timestamp)
    run_root = Path(base_output_dir).resolve() / run_name
    images_dir = run_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    return {
        "root": run_root,
        "images": images_dir,
        "json": run_root / "results.json",
        "csv": run_root / "results.csv",
        "txt": run_root / "results.txt",
    }


def build_result_rows(records):
    rows = []
    for record in records:
        rows.append(
            {
                "No": record.number,
                "Product1": record.product1,
                "Product2": record.product2,
                "Type": record.conflict_type,
                "Value": record.value,
                "Status": record.status,
                "Info": record.info,
                "Keep": record.keep,
                "Comment": record.comment,
                "Location": record.location,
                "Image": record.image_path,
            }
        )
    return rows


def write_results_json(json_path, document_summary, clearance_mm, selected_part_number, records, warnings=None):
    payload = {
        "document_name": document_summary["name"],
        "document_path": document_summary["path"],
        "analysis_mode": document_summary.get("analysis_mode"),
        "analysis_type": ANALYSIS_TYPE_LABEL,
        "clearance_mm": clearance_mm,
        "selected_part_number": selected_part_number,
        "warnings": list(warnings or []),
        "results": build_result_rows(records),
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_results_csv(csv_path, records):
    headers = ["No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location", "Image"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in build_result_rows(records):
            writer.writerow([row.get(header, "") for header in headers])


def write_results_txt(txt_path, records):
    headers = ["No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location", "Image"]
    rows = [[str(row.get(header, "")) for header in headers] for row in build_result_rows(records)]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))

    def format_row(values):
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(values))

    lines = [
        format_row(headers),
        "-+-".join("-" * width for width in widths),
    ]
    for row in rows:
        lines.append(format_row(row))
    txt_path.write_text("\n".join(lines), encoding="utf-8")


def cleanup_temporary_clashes(root_product, temporary_clash_names):
    if not temporary_clash_names:
        return
    try:
        clashes = get_clashes_collection(root_product)
    except Exception:
        return
    for clash_name in temporary_clash_names:
        safe_com_call(clashes, "Remove", clash_name)


def cleanup_temporary_groups(root_product, temporary_group_names):
    if not temporary_group_names:
        return
    try:
        groups = get_groups_collection(root_product)
    except Exception:
        return
    for group_name in temporary_group_names:
        safe_com_call(groups, "Remove", group_name)


def print_results_table(records, show_rows=12):
    if show_rows is None or show_rows <= 0:
        return
    headers = ["No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location"]
    rows = [[str(row.get(header, "")) for header in headers] for row in build_result_rows(records)[:show_rows]]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def format_row(values):
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(values))

    print("")
    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))


def run_nominal_dmu_analysis(
    document_path=None,
    part_number=None,
    clearance_mm=5.0,
    output_dir="Results/dmu_agent",
    show_rows=12,
    progress_callback=None,
):
    if not part_number:
        raise RuntimeError("A CATIA part number is required for selection-against-all nominal DMU.")

    catia = connect_catia()
    document = None
    document_opened_here = False
    temporary_clash_names = []
    scope = None
    created_at_utc = utc_now_iso()

    try:
        document, document_opened_here = resolve_document(catia, document_path)
        safe_com_call(document, "Activate")
        requested_workbench, current_workbench = start_dmu_workbench(catia)
        document_summary = get_document_summary(document)
        root_product = get_document_product(document)
        scope = build_scope_for_selection_against_all(root_product, document_summary, part_number)
        output_paths = create_output_paths(output_dir, document_summary["name"])
        clashes_collection = get_clashes_collection(root_product)

        contact_clash = run_contact_plus_clash(
            clashes_collection,
            first_group=scope.first_group,
            second_group=scope.second_group,
        )
        temporary_clash_names.append(contact_clash.name)

        clearance_clash = run_clearance(
            clashes_collection,
            clearance_mm=clearance_mm,
            first_group=scope.first_group,
            second_group=scope.second_group,
        )
        temporary_clash_names.append(clearance_clash.name)

        records = collect_conflict_records(contact_clash, clearance_clash)
        warnings = []
        write_results_json(
            output_paths["json"],
            document_summary,
            clearance_mm,
            part_number,
            records,
            warnings=warnings,
        )
        write_results_csv(output_paths["csv"], records)
        write_results_txt(output_paths["txt"], records)

        type_counts = {"clash": 0, "clearance": 0, "contact": 0}
        for record in records:
            type_counts[record.conflict_type] = int(type_counts.get(record.conflict_type, 0)) + 1

        _print("CATIA nominal DMU analysis complete.", progress_callback)
        _print("Active document: {}".format(document_summary["name"]), progress_callback)
        _print("Document path: {}".format(document_summary["path"] or "<unknown>"), progress_callback)
        _print("Requested workbench: {}".format(requested_workbench), progress_callback)
        _print("Current workbench: {}".format(current_workbench or "unknown"), progress_callback)
        _print("Mode: {}".format(MODE_SELECTION_AGAINST_ALL), progress_callback)
        _print("Analysis type: {}".format(ANALYSIS_TYPE_LABEL), progress_callback)
        _print("Clearance mm: {}".format(clearance_mm), progress_callback)
        _print("Selected targets: {}".format(", ".join(
            target.display_name or target.part_number or target.tree_path for target in scope.selected_targets
        )), progress_callback)
        _print("Total result rows: {}".format(len(records)), progress_callback)
        _print("JSON output: {}".format(output_paths["json"]), progress_callback)
        _print("CSV output: {}".format(output_paths["csv"]), progress_callback)
        _print("TXT output: {}".format(output_paths["txt"]), progress_callback)
        _print("Images folder: {}".format(output_paths["images"]), progress_callback)
        print_results_table(records, show_rows=show_rows)

        return {
            "status": "completed",
            "analysis_mode": MODE_SELECTION_AGAINST_ALL,
            "analysis_type": ANALYSIS_TYPE_LABEL,
            "part_number": part_number,
            "document_name": document_summary["name"],
            "document_path": document_summary["path"],
            "run_id": output_paths["root"].name,
            "created_at_utc": created_at_utc,
            "results_json_path": str(output_paths["json"]),
            "results_csv_path": str(output_paths["csv"]),
            "results_txt_path": str(output_paths["txt"]),
            "images_dir": str(output_paths["images"]),
            "result_row_count": len(records),
            "clash_count": int(type_counts.get("clash", 0)),
            "clearance_count": int(type_counts.get("clearance", 0)),
            "contact_count": int(type_counts.get("contact", 0)),
        }
    finally:
        if document is not None:
            try:
                root_product = get_document_product(document)
            except Exception:
                root_product = None
            if root_product is not None:
                cleanup_temporary_clashes(root_product, temporary_clash_names)
                if scope is not None:
                    cleanup_temporary_groups(root_product, scope.temporary_group_names)
        if document_opened_here and document is not None:
            safe_com_call(document, "Close")


def resolve_summary_json_path(summary_json, default_file_name="catia_layer3_summary.json"):
    if not summary_json:
        return None
    summary_path = Path(summary_json).resolve()
    if summary_path.exists() and summary_path.is_dir():
        return summary_path / default_file_name
    return summary_path


def resolve_drawing_identity_from_db(db_path, document_id=None):
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.exists():
        raise RuntimeError("SQLite database not found: {}".format(resolved_db_path))

    conn = sqlite3.connect(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT document_id, part_number FROM documents"
        params = []
        if document_id:
            sql += " WHERE document_id = ?"
            params.append(document_id)
        sql += " ORDER BY rowid LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None and document_id:
            row = conn.execute(
                "SELECT document_id, part_number FROM documents ORDER BY rowid LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "No drawing document row was found in {}. Run Layer 1/Layer 2 first so Layer 3 has a document owner.".format(
                    resolved_db_path
                )
            )
        return {
            "document_id": row["document_id"],
            "drawing_part_number": row["part_number"],
        }
    finally:
        conn.close()


def run_nominal_dmu_to_sqlite(
    db_path,
    document_id,
    drawing_part_number,
    target_part_number,
    root_document_path=None,
    clearance_mm=5.0,
    output_dir="Results/dmu_agent",
    summary_json=None,
    progress_callback=None,
):
    if not target_part_number:
        return {
            "status": "skipped",
            "reason": "no CATIA DMU target part number was provided or inferred",
            "analysis_mode": MODE_SELECTION_AGAINST_ALL,
            "analysis_type": ANALYSIS_TYPE_LABEL,
            "runs_table_name": "catia_dmu_runs_nominal",
            "table_name": "catia_dmu_results_nominal",
        }
    drawing_identity = resolve_drawing_identity_from_db(db_path, document_id=document_id)
    document_id = drawing_identity["document_id"]
    drawing_part_number = drawing_part_number or drawing_identity["drawing_part_number"]

    _print(
        "Layer 3 Stage 01/03 - DMU Mode Selection: using {} for {}".format(
            MODE_SELECTION_AGAINST_ALL,
            target_part_number,
        ),
        progress_callback,
    )
    _print(
        "Layer 3 Stage 02/03 - Nominal DMU Analysis: running {} for CATIA nominal values".format(
            ANALYSIS_TYPE_LABEL
        ),
        progress_callback,
    )
    analysis_summary = run_nominal_dmu_analysis(
        document_path=root_document_path,
        part_number=target_part_number,
        clearance_mm=clearance_mm,
        output_dir=output_dir,
        show_rows=0,
        progress_callback=progress_callback,
    )
    _print(
        "Layer 3 Stage 03/03 - DMU Nominal Output Writer: saving {} nominal row(s) to SQLite".format(
            analysis_summary.get("result_row_count", 0)
        ),
        progress_callback,
    )
    sqlite_summary = import_catia_dmu_results_to_sqlite(
        db_path=db_path,
        tolerance_mode="nominal",
        document_id=document_id,
        drawing_part_number=drawing_part_number,
        dmu_inputs={"part_number": target_part_number},
        results_json_path=analysis_summary["results_json_path"],
        created_at_utc=analysis_summary["created_at_utc"],
        run_metadata={},
    )

    result = {
        "status": "completed",
        "analysis_mode": MODE_SELECTION_AGAINST_ALL,
        "analysis_type": ANALYSIS_TYPE_LABEL,
        "target_part_number": target_part_number,
        "runs_table_name": sqlite_summary.get("runs_table_name"),
        "table_name": sqlite_summary.get("table_name"),
        "run_id": sqlite_summary.get("run_id"),
        "stored_row_count": sqlite_summary.get("stored_row_count", 0),
        "clash_count": analysis_summary.get("clash_count", 0),
        "clearance_count": analysis_summary.get("clearance_count", 0),
        "contact_count": analysis_summary.get("contact_count", 0),
        "results_json_path": sqlite_summary.get("results_json_path"),
        "results_csv_path": sqlite_summary.get("results_csv_path"),
        "results_txt_path": sqlite_summary.get("results_txt_path"),
        "images_dir": analysis_summary.get("images_dir"),
        "created_at_utc": analysis_summary.get("created_at_utc"),
    }
    summary_path = resolve_summary_json_path(summary_json)
    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["summary_json_path"] = str(summary_path)
    _print(
        "Layer 3 Completed 03/03 - Nominal DMU finished: rows={} | clash={} | contact={} | clearance={}".format(
            result["stored_row_count"],
            result["clash_count"],
            result["contact_count"],
            result["clearance_count"],
        ),
        progress_callback,
    )
    return result


def main():
    args = parse_args()
    run_nominal_dmu_analysis(
        document_path=args.document,
        part_number=args.part_number,
        clearance_mm=args.clearance_mm,
        output_dir=args.output_dir,
        show_rows=args.show_rows,
    )


if __name__ == "__main__":
    main()
