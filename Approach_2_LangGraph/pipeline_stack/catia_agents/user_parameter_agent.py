import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def as_dynamic_dispatch(obj):
    if obj is None:
        return None
    try:
        import win32com.client
        import win32com.client.dynamic

        try:
            dynamic_dispatch = win32com.client.dynamic.DumbDispatch(obj)
            if dynamic_dispatch is not None:
                return dynamic_dispatch
        except Exception:
            pass
        if obj.__class__.__module__.startswith("win32com.client.dynamic"):
            return obj
        if obj.__class__.__module__.startswith("win32com.client"):
            return obj
        try:
            dispatched = win32com.client.Dispatch(obj)
            if dispatched is not None:
                return dispatched
        except Exception:
            pass
        return obj
    except Exception:
        return obj


def safe_com_get(obj, attr, default=None):
    try:
        return getattr(obj, attr)
    except Exception:
        dynamic_obj = as_dynamic_dispatch(obj)
        if dynamic_obj is not None and dynamic_obj is not obj:
            try:
                return getattr(dynamic_obj, attr)
            except Exception:
                return default
        return default


def safe_com_call(obj, method_name, *args, default=None):
    try:
        method = getattr(obj, method_name)
        return method(*args)
    except Exception:
        dynamic_obj = as_dynamic_dispatch(obj)
        if dynamic_obj is not None and dynamic_obj is not obj:
            try:
                method = getattr(dynamic_obj, method_name)
                return method(*args)
            except Exception:
                return default
        return default


def safe_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def safe_bool_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if not text:
        return None
    return text.lower()


def parse_csv_values(text):
    if text is None:
        return []
    return [item.strip() for item in str(text).split(",") if item.strip()]


def parse_number_selection(text, max_index):
    raw = safe_text(text)
    if not raw:
        return []
    lowered = raw.lower()
    if lowered in {"all", "*"}:
        return list(range(1, max_index + 1))

    selected = set()
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start = safe_int(start_text)
            end = safe_int(end_text)
            if start is None or end is None or start < 1 or end < start or end > max_index:
                raise ValueError("Invalid range: {}".format(piece))
            selected.update(range(start, end + 1))
            continue
        index = safe_int(piece)
        if index is None or not (1 <= index <= max_index):
            raise ValueError("Invalid selection: {}".format(piece))
        selected.add(index)
    return sorted(selected)


def iter_com_collection(collection):
    if collection is None:
        return
    try:
        for item in collection:
            dynamic_item = as_dynamic_dispatch(item)
            if dynamic_item is not None:
                yield dynamic_item
            elif item is not None:
                yield item
        return
    except Exception:
        pass
    count = safe_int(safe_com_get(collection, "Count"), default=0)
    for index in range(1, count + 1):
        item = safe_com_call(collection, "Item", index)
        if item is not None:
            yield item


def get_catia_document_kind(document):
    if document is None:
        return None

    document_type = (safe_text(safe_com_get(document, "Type")) or "").strip().lower()
    if document_type in {"part", "partdocument"}:
        return "part"
    if document_type in {"product", "productdocument"}:
        return "product"

    full_name = safe_text(safe_com_get(document, "FullName"))
    if full_name:
        suffix = Path(full_name).suffix.lower()
        if suffix == ".catpart":
            return "part"
        if suffix == ".catproduct":
            return "product"

    name = safe_text(safe_com_get(document, "Name"))
    if name:
        suffix = Path(name).suffix.lower()
        if suffix == ".catpart":
            return "part"
        if suffix == ".catproduct":
            return "product"

    return None


def is_catia_root_document(document):
    return get_catia_document_kind(document) in {"product", "part"}


def find_open_document_by_path(catia, document_path):
    resolved = str(Path(document_path).resolve()).lower()
    active_document = safe_com_get(catia, "ActiveDocument")
    active_full_name = safe_text(safe_com_get(active_document, "FullName"))
    if active_full_name and str(Path(active_full_name).resolve()).lower() == resolved:
        return active_document
    documents = safe_com_get(catia, "Documents")
    for document in iter_com_collection(documents):
        full_name = safe_text(safe_com_get(document, "FullName"))
        if full_name and str(Path(full_name).resolve()).lower() == resolved:
            return document
    return None


def get_child_products(product):
    return list(iter_com_collection(safe_com_get(product, "Products")))


def strip_instance_suffix(name):
    text = safe_text(name)
    if not text:
        return None
    return re.sub(r"\.\d+$", "", text)


def normalize_identity_key(text):
    cleaned = strip_instance_suffix(text) or safe_text(text)
    if not cleaned:
        return None
    cleaned = cleaned.strip()
    match = re.match(r"^(.*?)-\d.*$", cleaned)
    if match:
        cleaned = match.group(1).strip()
    return cleaned or None


def build_document_file_index(source_document_path):
    if not source_document_path:
        return {}
    root_dir = Path(source_document_path).resolve().parent
    index = {}
    for path in root_dir.rglob("*"):
        if path.suffix.lower() not in {".catproduct", ".catpart"}:
            continue
        key = normalize_identity_key(path.stem)
        if not key:
            continue
        index.setdefault(key.lower(), []).append(path.resolve())
    return index


def choose_candidate_file(node_kind, candidates):
    if not candidates:
        return None
    preferred_suffix = ".catproduct" if node_kind == "assembly" else ".catpart"
    for candidate in candidates:
        if candidate.suffix.lower() == preferred_suffix:
            return candidate
    return candidates[0]


def resolve_source_file_for_node(node_kind, instance_name, part_number, file_index):
    identity_candidates = []
    for value in (part_number, instance_name):
        key = normalize_identity_key(value)
        if key and key.lower() not in identity_candidates:
            identity_candidates.append(key.lower())

    for identity_key in identity_candidates:
        matched = choose_candidate_file(node_kind, file_index.get(identity_key, []))
        if matched is not None:
            return matched
    return None


def normalize_parameter_selector(text):
    value = safe_text(text)
    if not value:
        return None
    value = value.replace("/", "\\")
    return value.lower()


def parameter_leaf_name(text):
    value = safe_text(text)
    if not value:
        return None
    value = value.replace("/", "\\")
    return value.split("\\")[-1]


def sanitize_filename(text):
    value = safe_text(text) or "catia_document"
    return re.sub(r'[<>:"/\\|?*]+', "_", value)


def find_parameter_value(parameters, candidate_names):
    normalized_candidates = {normalize_parameter_selector(name) for name in candidate_names if normalize_parameter_selector(name)}
    for parameter in parameters or []:
        candidates = [
            parameter.parameter_path,
            parameter.parameter_name,
            parameter_leaf_name(parameter.parameter_path),
            parameter_leaf_name(parameter.parameter_name),
        ]
        for candidate in candidates:
            normalized = normalize_parameter_selector(candidate)
            if normalized and normalized in normalized_candidates:
                return parameter.value_text
    return None


def compose_display_name(part_number=None, definition=None, life_cycle_status=None, revision=None, part_type=None, fallback_name=None):
    base_name = safe_text(part_number) or safe_text(fallback_name)
    if not base_name:
        return None
    if part_type:
        base_name = "{}-{}".format(part_type, base_name)

    pieces = [base_name]
    if definition:
        pieces.append('"{}"'.format(definition))
    if life_cycle_status:
        pieces.append(life_cycle_status)
    if revision:
        pieces.append(str(revision))
    return " ".join(piece for piece in pieces if piece)


def derive_display_name(product, parameters=None, fallback_name=None):
    part_number = safe_text(safe_com_get(product, "PartNumber")) or strip_instance_suffix(safe_text(safe_com_get(product, "Name")))
    definition = safe_text(safe_com_get(product, "Definition"))
    revision = safe_text(safe_com_get(product, "Revision"))
    part_type = find_parameter_value(parameters, ["SCANIA Part Type"])
    life_cycle_status = find_parameter_value(parameters, ["Life Cycle Status"])
    return compose_display_name(
        part_number=part_number,
        definition=definition,
        life_cycle_status=life_cycle_status,
        revision=revision,
        part_type=part_type,
        fallback_name=fallback_name,
    ) or fallback_name or part_number


@dataclass
class ProductNode:
    tree_path: str
    depth: int
    node_kind: str
    display_name: str | None
    instance_name: str | None
    resolved_name: str | None
    part_number: str | None
    reference_name: str | None
    document_name: str | None
    document_type: str | None
    source_file_name: str | None
    source_file_path: str | None
    source_file_type: str | None


@dataclass
class ParameterRow:
    tree_path: str
    parameter_path: str | None
    parameter_name: str | None
    value_text: str | None
    formula_text: str | None
    active_text: str | None
    user_access_mode: int | None
    parameter_type: str | None


@dataclass
class SelectableTarget:
    product: object
    tree_path: str
    depth: int
    node_kind: str
    display_name: str | None
    instance_name: str | None
    part_number: str | None
    child_count: int


def connect_catia():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise RuntimeError("This agent requires pywin32. Install it in the active Python environment.") from exc

    pythoncom.CoInitialize()
    errors = []

    try:
        catia = win32com.client.GetObject(Class="CATIA.Application")
        if catia is not None:
            catia = as_dynamic_dispatch(catia)
            if catia is not None:
                return catia
    except Exception as exc:
        errors.append("GetObject: {}".format(exc))

    try:
        catia = win32com.client.Dispatch("CATIA.Application")
        if catia is not None:
            catia = as_dynamic_dispatch(catia)
            if catia is not None:
                return catia
    except Exception as exc:
        errors.append("Dispatch: {}".format(exc))

    try:
        catia = win32com.client.gencache.EnsureDispatch("CATIA.Application")
        if catia is not None:
            catia = as_dynamic_dispatch(catia)
            if catia is not None:
                return catia
    except Exception as exc:
        errors.append("EnsureDispatch: {}".format(exc))

    try:
        catia = pythoncom.GetActiveObject("CATIA.Application")
        if catia is not None:
            catia = as_dynamic_dispatch(catia)
            if catia is not None:
                return catia
    except Exception as exc:
        errors.append("GetActiveObject: {}".format(exc))

    raise RuntimeError(
        "Could not connect to CATIA. Make sure CATIA is installed and running. Attempts: {}".format(
            " | ".join(errors) if errors else "none"
        )
    )


def resolve_document(catia, document_path=None):
    opened_here = False
    if document_path:
        resolved = str(Path(document_path).resolve())
        suffix = Path(resolved).suffix.lower()
        if suffix not in {".catproduct", ".catpart"}:
            raise RuntimeError(
                "Unsupported CATIA document type: {}. Pass a .CATProduct or .CATPart file.".format(suffix or "<no extension>")
            )
        existing_document = find_open_document_by_path(catia, resolved)
        if existing_document is not None:
            return existing_document, opened_here
        documents = safe_com_get(catia, "Documents")
        if documents is None:
            raise RuntimeError("CATIA.Documents is not available.")
        document = safe_com_call(documents, "Open", resolved)
        if document is None:
            existing_document = find_open_document_by_path(catia, resolved)
            if existing_document is not None:
                return existing_document, opened_here
            raise RuntimeError("CATIA could not open document: {}".format(resolved))
        opened_here = True
        return document, opened_here

    document = safe_com_get(catia, "ActiveDocument")
    if is_catia_root_document(document):
        return document, opened_here

    documents = safe_com_get(catia, "Documents")
    for candidate in iter_com_collection(documents):
        if is_catia_root_document(candidate):
            return candidate, opened_here

    if document is None:
        raise RuntimeError(
            "No active CATIA CATProduct/CATPart document was found. Open the product/part window in CATIA, or pass --catia_root_document with a .CATProduct/.CATPart path."
        )
    raise RuntimeError(
        "CATIA is running, but the active/open documents are not CATProduct/CATPart documents. Open the product/part window in CATIA, or pass --catia_root_document with a .CATProduct/.CATPart path."
    )
    return document, opened_here


def read_document_background(catia, document_path):
    resolved = str(Path(document_path).resolve())
    suffix = Path(resolved).suffix.lower()
    if suffix not in {".catproduct", ".catpart"}:
        raise RuntimeError(
            "Unsupported CATIA document type: {}. Pass a .CATProduct or .CATPart file.".format(suffix or "<no extension>")
        )
    documents = safe_com_get(catia, "Documents")
    if documents is None:
        raise RuntimeError("CATIA.Documents is not available.")
    document = safe_com_call(documents, "Read", resolved)
    if document is None:
        raise RuntimeError("CATIA could not read document: {}".format(resolved))
    return document, True


def get_document_product(document):
    document_name = safe_text(safe_com_get(document, "Name")) or "unknown"
    document_type = safe_text(safe_com_get(document, "Type")) or "unknown"
    document_path = safe_text(safe_com_get(document, "FullName"))

    dynamic_document = as_dynamic_dispatch(document)

    product = safe_com_get(dynamic_document, "Product")
    if product is not None:
        return product

    product = safe_com_call(dynamic_document, "GetItem", "Product")
    if product is not None:
        return product

    part = safe_com_get(dynamic_document, "Part")
    if part is not None:
        parent = safe_com_get(part, "Parent")
        product = safe_com_get(parent, "Product")
        if product is not None:
            return product

    raise RuntimeError(
        "The selected CATIA document does not expose a Product object. "
        "CATIA attached to document name={!r}, type={!r}, path={!r}. "
        "Open the target assembly/product window in CATIA or pass --document with a .CATProduct/.CATPart file.".format(
            document_name,
            document_type,
            document_path,
        )
    )


def get_document_metadata(document):
    return {
        "document_name": safe_text(safe_com_get(document, "Name")),
        "document_path": safe_text(safe_com_get(document, "FullName")),
        "document_type": safe_text(safe_com_get(document, "Type")),
    }


def collect_relation_text(parameter):
    relation = safe_com_get(parameter, "OptionalRelation")
    if relation is None:
        return None
    for attr in ("Text", "Name", "Value", "Comment"):
        value = safe_com_get(relation, attr)
        text = safe_text(value)
        if text:
            return text
    return None


def collect_active_text(parameter):
    relation = safe_com_get(parameter, "OptionalRelation")
    for source in (parameter, relation):
        if source is None:
            continue
        for attr in ("Activity", "Activated", "IsActive"):
            value = safe_com_get(source, attr)
            text = safe_bool_text(value)
            if text is not None:
                return text
    return None


def collect_parameter_value_text(parameter):
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


def collect_parameter_type(parameter):
    for attr in ("Type", "Dimension", "Dimensionality"):
        value = safe_com_get(parameter, attr)
        text = safe_text(value)
        if text:
            return text
    return safe_text(parameter.__class__.__name__)


def is_user_parameter(parameter):
    parameter_name = safe_text(safe_com_get(parameter, "Name")) or ""
    user_access_mode = safe_int(safe_com_get(parameter, "UserAccessMode"))
    if user_access_mode is not None:
        return user_access_mode == 2

    return "\\Properties\\" not in parameter_name


def iter_parameter_sets(parameter_set):
    if parameter_set is None:
        return

    direct_parameters = safe_com_get(parameter_set, "DirectParameters")
    for parameter in iter_com_collection(direct_parameters):
        yield parameter

    child_sets = safe_com_get(parameter_set, "ParameterSets")
    for child_set in iter_com_collection(child_sets):
        yield from iter_parameter_sets(child_set)


def iter_user_parameters(product):
    parameters = safe_com_get(product, "Parameters")
    if parameters is None:
        return []

    yielded = []
    seen_names = set()

    root_parameter_set = safe_com_get(parameters, "RootParameterSet")
    if root_parameter_set is not None:
        for parameter in iter_parameter_sets(root_parameter_set):
            parameter_name = safe_text(safe_com_get(parameter, "Name"))
            if not parameter_name or parameter_name in seen_names:
                continue
            if is_user_parameter(parameter):
                yielded.append(parameter)
                seen_names.add(parameter_name)
        if yielded:
            return yielded

    for parameter in iter_com_collection(parameters):
        parameter_name = safe_text(safe_com_get(parameter, "Name"))
        if not parameter_name or parameter_name in seen_names:
            continue
        if is_user_parameter(parameter):
            yielded.append(parameter)
            seen_names.add(parameter_name)
    return yielded


def iter_all_parameters(product):
    parameters = safe_com_get(product, "Parameters")
    if parameters is None:
        return []

    yielded = []
    seen_names = set()

    root_parameter_set = safe_com_get(parameters, "RootParameterSet")
    if root_parameter_set is not None:
        for parameter in iter_parameter_sets(root_parameter_set):
            parameter_name = safe_text(safe_com_get(parameter, "Name"))
            if not parameter_name or parameter_name in seen_names:
                continue
            yielded.append(parameter)
            seen_names.add(parameter_name)
        if yielded:
            return yielded

    for parameter in iter_com_collection(parameters):
        parameter_name = safe_text(safe_com_get(parameter, "Name"))
        if not parameter_name or parameter_name in seen_names:
            continue
        yielded.append(parameter)
        seen_names.add(parameter_name)
    return yielded


def build_product_node(product, tree_path, depth, document_name=None, document_type=None):
    children = get_child_products(product)
    instance_name = safe_text(safe_com_get(product, "Name"))
    part_number = safe_text(safe_com_get(product, "PartNumber"))
    reference_product = safe_com_get(product, "ReferenceProduct")
    reference_name = (
        safe_text(safe_com_get(reference_product, "Name"))
        or part_number
        or strip_instance_suffix(instance_name)
        or instance_name
    )
    return ProductNode(
        tree_path=tree_path,
        depth=depth,
        node_kind="assembly" if children else "part",
        display_name=compose_display_name(
            part_number=part_number or strip_instance_suffix(instance_name),
            definition=safe_text(safe_com_get(product, "Definition")),
            revision=safe_text(safe_com_get(product, "Revision")),
            fallback_name=strip_instance_suffix(instance_name) or instance_name,
        ),
        instance_name=instance_name,
        resolved_name=strip_instance_suffix(instance_name) or part_number or reference_name,
        part_number=part_number or strip_instance_suffix(instance_name),
        reference_name=reference_name,
        document_name=document_name,
        document_type=document_type,
        source_file_name=None,
        source_file_path=None,
        source_file_type=None,
    )


def collect_selectable_targets(product, tree_path, depth=0, include_self=True):
    selections = []
    children = get_child_products(product)
    node_kind = "assembly" if children else "part"

    if include_self:
        selections.append(
            SelectableTarget(
                product=product,
                tree_path=tree_path,
                depth=depth,
                node_kind=node_kind,
                display_name=compose_display_name(
                    part_number=safe_text(safe_com_get(product, "PartNumber")) or strip_instance_suffix(safe_text(safe_com_get(product, "Name"))),
                    definition=safe_text(safe_com_get(product, "Definition")),
                    revision=safe_text(safe_com_get(product, "Revision")),
                    fallback_name=strip_instance_suffix(safe_text(safe_com_get(product, "Name"))) or safe_text(safe_com_get(product, "Name")),
                ),
                instance_name=safe_text(safe_com_get(product, "Name")),
                part_number=safe_text(safe_com_get(product, "PartNumber")),
                child_count=len(children),
            )
        )

    for child_index, child in enumerate(children, start=1):
        child_name = safe_text(safe_com_get(child, "Name")) or "child_{}".format(child_index)
        child_tree_path = "{} > {}".format(tree_path, child_name)
        selections.extend(collect_selectable_targets(child, child_tree_path, depth + 1, include_self=True))
    return selections


def enrich_selectable_target_display(target, catia=None, file_index=None, document_cache=None, source_document_path=None):
    if file_index is None:
        file_index = {}
    if document_cache is None:
        document_cache = {}

    source_file = resolve_source_file_for_node(target.node_kind, target.instance_name, target.part_number, file_index)
    if source_file is not None:
        resolved_source_file = str(source_file.resolve())
        should_load = (
            catia is not None
            and (source_document_path is None or resolved_source_file.lower() != str(Path(source_document_path).resolve()).lower())
        )
        if should_load:
            snapshot = extract_referenced_document_snapshot(catia, resolved_source_file, document_cache)
            source_node = snapshot["node"]
            target.display_name = source_node.display_name or target.display_name
            target.part_number = target.part_number or source_node.part_number
            return target

    target.display_name = target.display_name or target.part_number or strip_instance_suffix(target.instance_name) or target.instance_name
    return target


def choose_target_nodes(root_product, root_tree_path, requested_tree_path=None, requested_target_kind=None, catia=None, file_index=None, document_cache=None, source_document_path=None):
    selections = collect_selectable_targets(root_product, root_tree_path, depth=0, include_self=True)
    selections = [
        enrich_selectable_target_display(
            selection,
            catia=catia,
            file_index=file_index,
            document_cache=document_cache,
            source_document_path=source_document_path,
        )
        for selection in selections
    ]
    assembly_selections = [selection for selection in selections if selection.node_kind == "assembly"]
    part_selections = [selection for selection in selections if selection.node_kind == "part"]

    if len(selections) == 1:
        return [selections[0]]

    if requested_tree_path:
        requested_paths = parse_csv_values(requested_tree_path)
        normalized_kind = safe_text(requested_target_kind)
        if normalized_kind:
            normalized_kind = normalized_kind.lower()
        selected = []
        missing = []
        for requested_path in requested_paths:
            matched = None
            for selection in selections:
                if selection.tree_path != requested_path:
                    continue
                if normalized_kind and selection.node_kind != normalized_kind:
                    continue
                matched = selection
                break
            if matched is None:
                missing.append(requested_path)
            else:
                selected.append(matched)
        if missing:
            raise RuntimeError("Requested target path not found: {}".format(", ".join(missing)))
        return selected

    if not selections:
        return [SelectableTarget(
            product=root_product,
            tree_path=root_tree_path,
            depth=0,
            node_kind="part",
            display_name=compose_display_name(
                part_number=safe_text(safe_com_get(root_product, "PartNumber")) or strip_instance_suffix(safe_text(safe_com_get(root_product, "Name"))),
                definition=safe_text(safe_com_get(root_product, "Definition")),
                revision=safe_text(safe_com_get(root_product, "Revision")),
                fallback_name=strip_instance_suffix(safe_text(safe_com_get(root_product, "Name"))) or safe_text(safe_com_get(root_product, "Name")),
            ),
            instance_name=safe_text(safe_com_get(root_product, "Name")),
            part_number=safe_text(safe_com_get(root_product, "PartNumber")),
            child_count=0,
        )]

    if not sys.stdin.isatty():
        raise RuntimeError(
            "Run interactively to choose which assembly or part to extract, or pass --target_path with an exact tree path."
        )

    print("")
    print("CATIA product content summary:")
    print("  Assemblies: {}".format(len(assembly_selections)))
    print("  Parts: {}".format(len(part_selections)))

    available_kinds = []
    if assembly_selections:
        available_kinds.append("assembly")
    if part_selections:
        available_kinds.append("part")

    if len(available_kinds) == 1:
        target_kind = available_kinds[0]
    else:
        while True:
            target_kind = input("Extract assemblies, parts, or both? [assembly/part/both]: ").strip().lower()
            if target_kind in {"assembly", "part", "both"}:
                break
            print("Please enter assembly, part, or both.")

    if target_kind == "assembly":
        active_selections = assembly_selections
    elif target_kind == "part":
        active_selections = part_selections
    else:
        active_selections = assembly_selections + part_selections
    print("")
    print("{} options found: {}".format(target_kind.capitalize(), len(active_selections)))
    for index, selection in enumerate(active_selections, start=1):
        indent = "  " * selection.depth
        label = "{}{} ({})".format(indent, selection.display_name or selection.tree_path, selection.tree_path)
        print(
            "  {}. [{}] {} [part_number={}, child_count={}]".format(
                index,
                selection.node_kind,
                label,
                selection.part_number or "unknown",
                selection.child_count,
            )
        )

    while True:
        try:
            choice = input(
                "Choose one or more targets to extract [1-{}, e.g. 1,3-5 or all]: ".format(len(active_selections))
            ).strip()
        except EOFError as exc:
            raise RuntimeError("Target selection cancelled.") from exc
        try:
            selected_indexes = parse_number_selection(choice, len(active_selections))
        except ValueError as exc:
            print(str(exc))
            continue
        if selected_indexes:
            return [active_selections[index - 1] for index in selected_indexes]
        print("Please enter at least one selection.")


def build_parameter_row(tree_path, parameter):
    return ParameterRow(
        tree_path=tree_path,
        parameter_path=safe_text(safe_com_get(parameter, "Name")),
        parameter_name=safe_text(safe_com_get(parameter, "RenamedName")) or safe_text(safe_com_get(parameter, "Name")),
        value_text=collect_parameter_value_text(parameter),
        formula_text=collect_relation_text(parameter),
        active_text=collect_active_text(parameter),
        user_access_mode=safe_int(safe_com_get(parameter, "UserAccessMode")),
        parameter_type=collect_parameter_type(parameter),
    )


def refresh_catia_document(catia, document, root_product):
    safe_com_call(document, "Activate")
    updated = False
    for target in (root_product, document):
        if safe_com_call(target, "Update") is not None:
            updated = True
            break
    safe_com_get(catia, "RefreshDisplay")
    return updated


def collect_available_parameter_selectors(nodes_with_parameters):
    selectors = {}
    for _, parameters in nodes_with_parameters:
        for parameter in parameters:
            for raw_name in (parameter.parameter_name, parameter.parameter_path):
                normalized_full = normalize_parameter_selector(raw_name)
                leaf_name = parameter_leaf_name(raw_name)
                normalized_leaf = normalize_parameter_selector(leaf_name)
                if normalized_leaf:
                    entry = selectors.setdefault(
                        normalized_leaf,
                        {
                            "leaf_name": leaf_name,
                            "sample_names": set(),
                            "tree_paths": set(),
                        },
                    )
                    if raw_name:
                        entry["sample_names"].add(raw_name)
                    entry["tree_paths"].add(parameter.tree_path)
                if normalized_full and normalized_full != normalized_leaf:
                    entry = selectors.setdefault(
                        normalized_full,
                        {
                            "leaf_name": raw_name,
                            "sample_names": set(),
                            "tree_paths": set(),
                        },
                    )
                    entry["sample_names"].add(raw_name)
                    entry["tree_paths"].add(parameter.tree_path)
    return selectors


def choose_required_parameters(nodes_with_parameters, requested_names=None):
    selectors = collect_available_parameter_selectors(nodes_with_parameters)
    if requested_names:
        selected = []
        missing = []
        for raw_name in requested_names:
            normalized = normalize_parameter_selector(raw_name)
            if normalized and normalized in selectors:
                selected.append(selectors[normalized]["leaf_name"])
            else:
                missing.append(raw_name)
        if missing:
            raise RuntimeError("Requested parameter names not found: {}".format(", ".join(missing)))
        return sorted({name for name in selected if name}, key=str.lower)
    return []


def filter_nodes_to_required_parameters(nodes_with_parameters, required_names):
    if not required_names:
        return nodes_with_parameters
    normalized_required = {normalize_parameter_selector(name) for name in required_names if normalize_parameter_selector(name)}
    filtered = []
    for index, (node, parameters) in enumerate(nodes_with_parameters):
        kept = []
        for parameter in parameters:
            candidates = {
                normalize_parameter_selector(parameter.parameter_name),
                normalize_parameter_selector(parameter.parameter_path),
                normalize_parameter_selector(parameter_leaf_name(parameter.parameter_name)),
                normalize_parameter_selector(parameter_leaf_name(parameter.parameter_path)),
            }
            if normalized_required.intersection(candidate for candidate in candidates if candidate):
                kept.append(parameter)
        if kept or index == 0:
            filtered.append((node, kept))
    return filtered


def merge_nodes_with_parameters(collections):
    merged = {}
    order = []
    for nodes_with_parameters in collections:
        for node, parameters in nodes_with_parameters:
            key = node.tree_path
            if key not in merged:
                merged[key] = (node, [])
                order.append(key)
            merged[key][1].extend(parameters)

    output = []
    for key in order:
        node, parameters = merged[key]
        output.append((node, dedupe_parameter_rows(parameters)))
    return output


def clone_parameter_rows_for_tree_path(parameters, tree_path):
    return [
        ParameterRow(
            tree_path=tree_path,
            parameter_path=parameter.parameter_path,
            parameter_name=parameter.parameter_name,
            value_text=parameter.value_text,
            formula_text=parameter.formula_text,
            active_text=parameter.active_text,
            user_access_mode=parameter.user_access_mode,
            parameter_type=parameter.parameter_type,
        )
        for parameter in parameters
    ]


def dedupe_parameter_rows(parameters):
    deduped = []
    seen_keys = set()
    for parameter in parameters:
        key = (
            parameter.parameter_path or "",
            parameter.parameter_name or "",
            parameter.value_text or "",
            parameter.formula_text or "",
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(parameter)
    return deduped


def extract_referenced_document_snapshot(catia, document_path, cache):
    resolved_path = str(Path(document_path).resolve())
    cached = cache.get(resolved_path)
    if cached is not None:
        return cached

    document, opened_here = read_document_background(catia, resolved_path)
    try:
        metadata = get_document_metadata(document)
        product = get_document_product(document)
        node = build_product_node(
            product,
            safe_text(safe_com_get(product, "Name")) or Path(resolved_path).stem,
            depth=0,
            document_name=metadata.get("document_name"),
            document_type=metadata.get("document_type"),
        )
        node.source_file_name = Path(resolved_path).name
        node.source_file_path = resolved_path
        node.source_file_type = Path(resolved_path).suffix.lower().lstrip(".")
        display_parameters = [build_parameter_row(node.tree_path, parameter) for parameter in iter_all_parameters(product)]
        node.display_name = derive_display_name(product, parameters=display_parameters, fallback_name=node.display_name or node.resolved_name)
        parameters = [build_parameter_row(node.tree_path, parameter) for parameter in iter_user_parameters(product)]
        snapshot = {
            "node": node,
            "parameters": parameters,
        }
        cache[resolved_path] = snapshot
        return snapshot
    finally:
        if opened_here:
            safe_com_call(document, "Close")


def walk_product_tree(product, tree_path, depth, document_name=None, document_type=None, catia=None, file_index=None, document_cache=None, source_document_path=None):
    node = build_product_node(product, tree_path, depth, document_name=document_name, document_type=document_type)
    parameters = [build_parameter_row(tree_path, parameter) for parameter in iter_user_parameters(product)]

    if file_index is None:
        file_index = {}
    if document_cache is None:
        document_cache = {}

    source_file = resolve_source_file_for_node(node.node_kind, node.instance_name, node.part_number, file_index)
    if source_file is not None:
        resolved_source_file = str(source_file.resolve())
        node.source_file_name = source_file.name
        node.source_file_path = resolved_source_file
        node.source_file_type = source_file.suffix.lower().lstrip(".")

        should_load_referenced_document = (
            catia is not None
            and (source_document_path is None or resolved_source_file.lower() != str(Path(source_document_path).resolve()).lower())
        )
        if should_load_referenced_document:
            snapshot = extract_referenced_document_snapshot(catia, resolved_source_file, document_cache)
            source_node = snapshot["node"]
            node.display_name = source_node.display_name or node.display_name
            node.part_number = node.part_number or source_node.part_number
            node.reference_name = source_node.reference_name or node.reference_name
            node.resolved_name = source_node.resolved_name or node.resolved_name
            if node.document_name is None:
                node.document_name = source_node.document_name
            if node.document_type is None:
                node.document_type = source_node.document_type
            parameters.extend(clone_parameter_rows_for_tree_path(snapshot["parameters"], tree_path))

    parameters = dedupe_parameter_rows(parameters)
    yield node, parameters

    children = get_child_products(product)
    for child_index, child in enumerate(children, start=1):
        child_name = safe_text(safe_com_get(child, "Name")) or "child_{}".format(child_index)
        child_tree_path = "{} > {}".format(tree_path, child_name)
        yield from walk_product_tree(
            child,
            child_tree_path,
            depth + 1,
            document_name=document_name,
            document_type=document_type,
            catia=catia,
            file_index=file_index,
            document_cache=document_cache,
            source_document_path=source_document_path,
        )


def create_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS extraction_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL,
            source_document_name TEXT,
            source_document_path TEXT,
            source_document_type TEXT,
            root_instance_name TEXT,
            root_part_number TEXT,
            root_reference_name TEXT
        );

        CREATE TABLE IF NOT EXISTS extraction_targets (
            target_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            selection_order INTEGER NOT NULL,
            target_kind TEXT,
            target_tree_path TEXT,
            target_display_name TEXT,
            target_part_number TEXT,
            nodes_table_name TEXT NOT NULL,
            parameters_table_name TEXT NOT NULL,
            product_node_count INTEGER NOT NULL DEFAULT 0,
            user_parameter_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES extraction_runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS product_nodes (
            node_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            parent_tree_path TEXT,
            tree_path TEXT NOT NULL,
            depth INTEGER NOT NULL,
            node_kind TEXT,
            display_name TEXT,
            instance_name TEXT,
            resolved_name TEXT,
            part_number TEXT,
            reference_name TEXT,
            document_name TEXT,
            document_type TEXT,
            source_file_name TEXT,
            source_file_path TEXT,
            source_file_type TEXT,
            parameter_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES extraction_runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_parameters (
            parameter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            tree_path TEXT NOT NULL,
            parameter_path TEXT,
            parameter_name TEXT,
            value_text TEXT,
            formula_text TEXT,
            active_text TEXT,
            user_access_mode INTEGER,
            parameter_type TEXT,
            FOREIGN KEY(run_id) REFERENCES extraction_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY(node_id) REFERENCES product_nodes(node_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_product_nodes_run_path
            ON product_nodes(run_id, tree_path);

        CREATE INDEX IF NOT EXISTS idx_extraction_targets_run_order
            ON extraction_targets(run_id, selection_order);

        CREATE INDEX IF NOT EXISTS idx_user_parameters_run_node
            ON user_parameters(run_id, node_id);
        """
    )
    for statement in (
        "ALTER TABLE product_nodes ADD COLUMN node_kind TEXT",
        "ALTER TABLE product_nodes ADD COLUMN display_name TEXT",
        "ALTER TABLE product_nodes ADD COLUMN resolved_name TEXT",
        "ALTER TABLE product_nodes ADD COLUMN source_file_name TEXT",
        "ALTER TABLE product_nodes ADD COLUMN source_file_path TEXT",
        "ALTER TABLE product_nodes ADD COLUMN source_file_type TEXT",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass


def create_target_tables(conn, nodes_table_name, parameters_table_name):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS "{nodes_table_name}" (
            node_id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_tree_path TEXT,
            tree_path TEXT NOT NULL,
            depth INTEGER NOT NULL,
            node_kind TEXT,
            display_name TEXT,
            instance_name TEXT,
            resolved_name TEXT,
            part_number TEXT,
            reference_name TEXT,
            document_name TEXT,
            document_type TEXT,
            source_file_name TEXT,
            source_file_path TEXT,
            source_file_type TEXT,
            parameter_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS "{parameters_table_name}" (
            parameter_id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL,
            tree_path TEXT NOT NULL,
            parameter_path TEXT,
            parameter_name TEXT,
            value_text TEXT,
            formula_text TEXT,
            active_text TEXT,
            user_access_mode INTEGER,
            parameter_type TEXT
        );

        CREATE INDEX IF NOT EXISTS "idx_{nodes_table_name}_path"
            ON "{nodes_table_name}"(tree_path);

        CREATE INDEX IF NOT EXISTS "idx_{parameters_table_name}_node"
            ON "{parameters_table_name}"(node_id);
        """.format(
            nodes_table_name=nodes_table_name,
            parameters_table_name=parameters_table_name,
        )
    )


def insert_run(conn, metadata, root_node):
    cursor = conn.execute(
        """
        INSERT INTO extraction_runs (
            started_at_utc,
            source_document_name,
            source_document_path,
            source_document_type,
            root_instance_name,
            root_part_number,
            root_reference_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            metadata.get("document_name"),
            metadata.get("document_path"),
            metadata.get("document_type"),
            root_node.instance_name,
            root_node.part_number,
            root_node.reference_name,
        ),
    )
    return cursor.lastrowid


def parent_tree_path(tree_path):
    if " > " not in tree_path:
        return None
    return tree_path.rsplit(" > ", 1)[0]


def make_target_table_names(selected_target, selection_order):
    base_slug = "{}_{}".format(selection_order, selected_target.tree_path).lower()
    base_slug = re.sub(r"[^a-z0-9_]+", "_", base_slug)
    base_slug = re.sub(r"_+", "_", base_slug).strip("_")[:48] or "target_{}".format(selection_order)
    return "product_nodes__{}".format(base_slug), "user_parameters__{}".format(base_slug)


def insert_target_metadata(conn, run_id, selection_order, selected_target, nodes_table_name, parameters_table_name, product_node_count, user_parameter_count):
    cursor = conn.execute(
        """
        INSERT INTO extraction_targets (
            run_id,
            selection_order,
            target_kind,
            target_tree_path,
            target_display_name,
            target_part_number,
            nodes_table_name,
            parameters_table_name,
            product_node_count,
            user_parameter_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            selection_order,
            selected_target.node_kind,
            selected_target.tree_path,
            selected_target.display_name,
            selected_target.part_number,
            nodes_table_name,
            parameters_table_name,
            product_node_count,
            user_parameter_count,
        ),
    )
    return cursor.lastrowid


def insert_node(conn, table_name, node, parameter_count, run_id=None):
    if table_name == "product_nodes":
        sql = """
        INSERT INTO product_nodes (
            run_id,
            parent_tree_path,
            tree_path,
            depth,
            node_kind,
            display_name,
            instance_name,
            resolved_name,
            part_number,
            reference_name,
            document_name,
            document_type,
            source_file_name,
            source_file_path,
            source_file_type,
            parameter_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            run_id,
            parent_tree_path(node.tree_path),
            node.tree_path,
            node.depth,
            node.node_kind,
            node.display_name,
            node.instance_name,
            node.resolved_name,
            node.part_number,
            node.reference_name,
            node.document_name,
            node.document_type,
            node.source_file_name,
            node.source_file_path,
            node.source_file_type,
            parameter_count,
        )
    else:
        sql = """
        INSERT INTO "{table_name}" (
            parent_tree_path,
            tree_path,
            depth,
            node_kind,
            display_name,
            instance_name,
            resolved_name,
            part_number,
            reference_name,
            document_name,
            document_type,
            source_file_name,
            source_file_path,
            source_file_type,
            parameter_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """.format(table_name=table_name)
        params = (
            parent_tree_path(node.tree_path),
            node.tree_path,
            node.depth,
            node.node_kind,
            node.display_name,
            node.instance_name,
            node.resolved_name,
            node.part_number,
            node.reference_name,
            node.document_name,
            node.document_type,
            node.source_file_name,
            node.source_file_path,
            node.source_file_type,
            parameter_count,
        )
    cursor = conn.execute(sql, params)
    return cursor.lastrowid


def insert_parameter(conn, table_name, node_id, parameter, run_id=None):
    if table_name == "user_parameters":
        sql = """
        INSERT INTO user_parameters (
            run_id,
            node_id,
            tree_path,
            parameter_path,
            parameter_name,
            value_text,
            formula_text,
            active_text,
            user_access_mode,
            parameter_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            run_id,
            node_id,
            parameter.tree_path,
            parameter.parameter_path,
            parameter.parameter_name,
            parameter.value_text,
            parameter.formula_text,
            parameter.active_text,
            parameter.user_access_mode,
            parameter.parameter_type,
        )
    else:
        sql = """
        INSERT INTO "{table_name}" (
            node_id,
            tree_path,
            parameter_path,
            parameter_name,
            value_text,
            formula_text,
            active_text,
            user_access_mode,
            parameter_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """.format(table_name=table_name)
        params = (
            node_id,
            parameter.tree_path,
            parameter.parameter_path,
            parameter.parameter_name,
            parameter.value_text,
            parameter.formula_text,
            parameter.active_text,
            parameter.user_access_mode,
            parameter.parameter_type,
        )
    conn.execute(sql, params)


def export_to_sqlite(db_path, metadata, target_extractions):
    resolved_db_path = Path(db_path)
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved_db_path))
    try:
        create_tables(conn)
        root_node, _ = target_extractions[0]["nodes_with_parameters"][0]
        run_id = insert_run(conn, metadata, root_node)
        multi_target = len(target_extractions) > 1
        for selection_order, target_extraction in enumerate(target_extractions, start=1):
            selected_target = target_extraction["selected_target"]
            nodes_with_parameters = target_extraction["nodes_with_parameters"]
            if multi_target:
                nodes_table_name, parameters_table_name = make_target_table_names(selected_target, selection_order)
                create_target_tables(conn, nodes_table_name, parameters_table_name)
            else:
                nodes_table_name, parameters_table_name = "product_nodes", "user_parameters"

            for node, parameters in nodes_with_parameters:
                node_id = insert_node(conn, nodes_table_name, node, len(parameters), run_id=run_id)
                for parameter in parameters:
                    insert_parameter(conn, parameters_table_name, node_id, parameter, run_id=run_id)

            insert_target_metadata(
                conn,
                run_id,
                selection_order,
                selected_target,
                nodes_table_name,
                parameters_table_name,
                product_node_count=len(nodes_with_parameters),
                user_parameter_count=sum(len(parameters) for _, parameters in nodes_with_parameters),
            )
        conn.commit()
        return run_id, resolved_db_path.resolve()
    finally:
        conn.close()


def build_summary(target_extractions, metadata, run_id, db_path, required_parameter_names=None):
    flattened_nodes = [item for target_extraction in target_extractions for item in target_extraction["nodes_with_parameters"]]
    total_nodes = len(flattened_nodes)
    total_parameters = sum(len(parameters) for _, parameters in flattened_nodes)
    assembly_count = sum(1 for node, _ in flattened_nodes if node.node_kind == "assembly")
    part_count = sum(1 for node, _ in flattened_nodes if node.node_kind == "part")
    return {
        "run_id": run_id,
        "source_document_name": metadata.get("document_name"),
        "source_document_path": metadata.get("document_path"),
        "source_document_type": metadata.get("document_type"),
        "product_node_count": total_nodes,
        "assembly_count": assembly_count,
        "part_count": part_count,
        "user_parameter_count": total_parameters,
        "required_parameter_names": list(required_parameter_names or []),
        "db_path": str(db_path),
        "targets": [
            {
                "selected_target": {
                    "tree_path": target_extraction["selected_target"].tree_path,
                    "node_kind": target_extraction["selected_target"].node_kind,
                    "display_name": target_extraction["selected_target"].display_name,
                    "part_number": target_extraction["selected_target"].part_number,
                },
                "nodes": [
                    {
                        "tree_path": node.tree_path,
                        "node_kind": node.node_kind,
                        "display_name": node.display_name,
                        "instance_name": node.instance_name,
                        "resolved_name": node.resolved_name,
                        "part_number": node.part_number,
                        "reference_name": node.reference_name,
                        "source_file_name": node.source_file_name,
                        "source_file_type": node.source_file_type,
                        "parameter_count": len(parameters),
                    }
                    for node, parameters in target_extraction["nodes_with_parameters"]
                ],
            }
            for target_extraction in target_extractions
        ],
    }


def discover_document_paths(single_document=None, document_dir=None):
    discovered = []
    seen = set()

    if single_document:
        resolved = str(Path(single_document).resolve())
        if resolved.lower() not in seen:
            discovered.append(resolved)
            seen.add(resolved.lower())

    if document_dir:
        root_dir = Path(document_dir).resolve()
        if not root_dir.exists():
            raise RuntimeError("Document directory not found: {}".format(root_dir))
        for suffix in ("*.CATProduct", "*.CATPart", "*.catproduct", "*.catpart"):
            for path in sorted(root_dir.rglob(suffix)):
                resolved = str(path.resolve())
                if resolved.lower() in seen:
                    continue
                discovered.append(resolved)
                seen.add(resolved.lower())

    if not discovered:
        return []
    return discovered


def build_output_paths(base_db, base_summary_json, document_path, multi_mode):
    document_stem = sanitize_filename(Path(document_path).stem)

    if multi_mode:
        db_root = Path(base_db)
        if db_root.suffix.lower() == ".db":
            db_root = db_root.parent
        db_root.mkdir(parents=True, exist_ok=True)
        db_path = db_root / "{}.db".format(document_stem)

        if base_summary_json:
            summary_root = Path(base_summary_json)
            if summary_root.suffix.lower() == ".json":
                summary_root = summary_root.parent
        else:
            summary_root = db_root
        summary_root.mkdir(parents=True, exist_ok=True)
        summary_json = summary_root / "{}.json".format(document_stem)
        return str(db_path), str(summary_json)

    return base_db, base_summary_json


def process_single_document(catia, args, document_path=None, multi_mode=False):
    document, opened_here = resolve_document(catia, document_path)
    try:
        metadata = get_document_metadata(document)
        file_index = build_document_file_index(metadata.get("document_path"))
        document_cache = {}
        root_product = get_document_product(document)
        refresh_catia_document(catia, document, root_product)
        root_tree_path = safe_text(safe_com_get(root_product, "Name")) or metadata.get("document_name") or "ROOT_PRODUCT"
        requested_target_path = args.target_path or args.assembly_path
        selected_targets = choose_target_nodes(
            root_product,
            root_tree_path,
            requested_tree_path=requested_target_path,
            requested_target_kind=args.target_kind,
            catia=catia,
            file_index=file_index,
            document_cache=document_cache,
            source_document_path=metadata.get("document_path"),
        )
        target_extractions = [
            {
                "selected_target": selected_target,
                "nodes_with_parameters": list(
                    walk_product_tree(
                        selected_target.product,
                        selected_target.tree_path,
                        depth=0,
                        document_name=metadata.get("document_name"),
                        document_type=metadata.get("document_type"),
                        catia=catia,
                        file_index=file_index,
                        document_cache=document_cache,
                        source_document_path=metadata.get("document_path"),
                    )
                ),
            }
            for selected_target in selected_targets
        ]
        combined_nodes_with_parameters = merge_nodes_with_parameters(
            [target_extraction["nodes_with_parameters"] for target_extraction in target_extractions]
        )
        required_parameter_names = choose_required_parameters(
            combined_nodes_with_parameters,
            requested_names=parse_csv_values(args.required_params),
        )
        target_extractions = [
            {
                "selected_target": target_extraction["selected_target"],
                "nodes_with_parameters": filter_nodes_to_required_parameters(
                    target_extraction["nodes_with_parameters"],
                    required_parameter_names,
                ),
            }
            for target_extraction in target_extractions
        ]
        db_path, summary_json = build_output_paths(args.db, args.summary_json, metadata.get("document_path") or document_path or "catia_document", multi_mode)
        run_id, resolved_db_path = export_to_sqlite(db_path, metadata, target_extractions)
        summary = build_summary(
            target_extractions,
            metadata,
            run_id,
            resolved_db_path,
            required_parameter_names=required_parameter_names,
        )

        if summary_json:
            summary_path = Path(summary_json).resolve()
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print("CATIA user-parameter extraction complete.")
        print("Run ID: {}".format(run_id))
        print("Source document: {}".format(metadata.get("document_name") or "unknown"))
        print(
            "Selected targets: {}".format(
                ", ".join("{}: {}".format(target.node_kind, target.tree_path) for target in selected_targets)
            )
        )
        print(
            "Required parameters: {}".format(
                ", ".join(required_parameter_names) if required_parameter_names else "all user parameters"
            )
        )
        print("Assemblies: {}".format(summary["assembly_count"]))
        print("Parts: {}".format(summary["part_count"]))
        print("Product nodes: {}".format(summary["product_node_count"]))
        print("User parameters: {}".format(summary["user_parameter_count"]))
        print("Database: {}".format(resolved_db_path))
        return {
            "metadata": metadata,
            "summary": summary,
            "db_path": str(resolved_db_path),
        }
    finally:
        if opened_here:
            safe_com_call(document, "Close")


def parse_args():
    parser = argparse.ArgumentParser(description="Extract CATIA user parameters from an active CATIA product or a provided CATIA document.")
    parser.add_argument("--document", default=None, type=str, help="Optional CATIA document path (.CATProduct/.CATPart). If omitted, uses CATIA ActiveDocument.")
    parser.add_argument(
        "--document_dir",
        default=None,
        type=str,
        help="Optional directory of CATIA documents. Each .CATProduct/.CATPart is processed into its own database file.",
    )
    parser.add_argument(
        "--assembly_path",
        default=None,
        type=str,
        help="Legacy alias for --target_path. Optional exact assembly tree path to extract.",
    )
    parser.add_argument(
        "--target_path",
        default=None,
        type=str,
        help="Optional exact assembly or part tree path to extract. If omitted, the agent asks in the terminal.",
    )
    parser.add_argument(
        "--target_kind",
        default=None,
        type=str,
        choices=["assembly", "part"],
        help="Optional target kind when using --target_path.",
    )
    parser.add_argument(
        "--db",
        default="result/catia_user_parameters/catia_user_parameters.db",
        type=str,
        help="SQLite database path for extracted CATIA user parameters.",
    )
    parser.add_argument(
        "--summary_json",
        default=None,
        type=str,
        help="Optional JSON summary output path.",
    )
    parser.add_argument(
        "--required_params",
        default=None,
        type=str,
        help="Comma-separated parameter names to extract, for example: m,d1,pitch_g",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    catia = connect_catia()
    document_paths = discover_document_paths(single_document=args.document, document_dir=args.document_dir)
    multi_mode = len(document_paths) > 1 or bool(args.document_dir)

    if not document_paths:
        process_single_document(catia, args, document_path=None, multi_mode=False)
        return

    print("Documents queued: {}".format(len(document_paths)))
    for index, document_path in enumerate(document_paths, start=1):
        print("")
        print("[{}/{}] Processing {}".format(index, len(document_paths), Path(document_path).name))
        process_single_document(catia, args, document_path=document_path, multi_mode=multi_mode)


if __name__ == "__main__":
    main()
