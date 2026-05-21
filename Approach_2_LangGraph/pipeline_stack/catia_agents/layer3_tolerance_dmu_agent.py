import json
import math
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

try:
    from pycatia.enumeration.enumeration_types import cat_projection_mode as CatProjectionMode
    from pycatia.in_interfaces.viewpoint_3d import ViewPoint3D
except ImportError:
    from pycatia.enumeration.enums import CatProjectionMode
    from pycatia.in_interfaces.viewpoint_3d import ViewPoint3D

try:
    from .create_length_parameters_agent import (
        find_parameter_by_name,
        get_parameters_collection,
        get_part_from_document,
        read_parameter_value_text,
        resolve_target_part_document,
        set_length_parameter_value,
    )
    from .nominal_dmu_agent import (
        ANALYSIS_TYPE_LABEL,
        MODE_SELECTION_AGAINST_ALL,
        build_scope_for_selection_against_all,
        enum_member_value,
        get_clashes_collection,
        get_document_summary,
        get_groups_collection,
        map_conflict_status,
        map_conflict_type,
        map_conflict_comparison_info,
        resolve_instance_name,
        resolve_drawing_identity_from_db,
        resolve_summary_json_path,
        run_clearance,
        run_contact_plus_clash,
        safe_conflict_comment,
        safe_conflict_comparison_info,
        safe_conflict_product,
        safe_conflict_status,
        safe_conflict_type,
        safe_conflict_value,
        start_dmu_workbench,
    )
    from .user_parameter_agent import (
        connect_catia,
        get_document_product,
        iter_all_parameters,
        iter_com_collection,
        resolve_document,
        safe_com_call,
        safe_com_get,
        safe_int,
        safe_text,
        utc_now_iso,
    )
except ImportError:
    try:
        from openai_pipeline.pipeline_stack.catia_agents.create_length_parameters_agent import (
            find_parameter_by_name,
            get_parameters_collection,
            get_part_from_document,
            read_parameter_value_text,
            resolve_target_part_document,
            set_length_parameter_value,
        )
        from openai_pipeline.pipeline_stack.catia_agents.nominal_dmu_agent import (
            ANALYSIS_TYPE_LABEL,
            MODE_SELECTION_AGAINST_ALL,
            build_scope_for_selection_against_all,
            enum_member_value,
            get_clashes_collection,
            get_document_summary,
            get_groups_collection,
            map_conflict_status,
            map_conflict_type,
            map_conflict_comparison_info,
            resolve_instance_name,
            resolve_drawing_identity_from_db,
            resolve_summary_json_path,
            run_clearance,
            run_contact_plus_clash,
            safe_conflict_comment,
            safe_conflict_comparison_info,
            safe_conflict_product,
            safe_conflict_status,
            safe_conflict_type,
            safe_conflict_value,
            start_dmu_workbench,
        )
        from openai_pipeline.pipeline_stack.catia_agents.user_parameter_agent import (
            connect_catia,
            get_document_product,
            iter_all_parameters,
            iter_com_collection,
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
        from catia_agents.create_length_parameters_agent import (
            find_parameter_by_name,
            get_parameters_collection,
            get_part_from_document,
            read_parameter_value_text,
            resolve_target_part_document,
            set_length_parameter_value,
        )
        from catia_agents.nominal_dmu_agent import (
            ANALYSIS_TYPE_LABEL,
            MODE_SELECTION_AGAINST_ALL,
            build_scope_for_selection_against_all,
            enum_member_value,
            get_clashes_collection,
            get_document_summary,
            get_groups_collection,
            map_conflict_status,
            map_conflict_type,
            map_conflict_comparison_info,
            resolve_instance_name,
            resolve_drawing_identity_from_db,
            resolve_summary_json_path,
            run_clearance,
            run_contact_plus_clash,
            safe_conflict_comment,
            safe_conflict_comparison_info,
            safe_conflict_product,
            safe_conflict_status,
            safe_conflict_type,
            safe_conflict_value,
            start_dmu_workbench,
        )
        from catia_agents.user_parameter_agent import (
            connect_catia,
            get_document_product,
            iter_all_parameters,
            iter_com_collection,
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


DEFAULT_LAYER3_RESULT_ROOT = "Results/layer3_dmu"
CAPTURE_FORMAT_BMP = 4
VIS_SHOW = 0
VIS_HIDE = 1
VIEW_SPECS = (
    ("iso", 32.0, -24.0),
    ("front", 0.0, 0.0),
    ("top", 0.0, -88.0),
    ("right", -90.0, 0.0),
)
VIEW_DISPLAY_NAMES = {
    "iso": "View 1",
    "front": "View 2",
    "top": "View 3",
    "right": "View 4",
}
PROJECTION_CONIC = enum_member_value(CatProjectionMode, "catProjectionConic")
PROJECTION_CYLINDRIC = enum_member_value(CatProjectionMode, "catProjectionCylindric")


@dataclass
class LinkedDimensionRow:
    document_id: str
    drawing_part_number: str
    dimension_id: str
    dimension_raw_text: str
    nominal_value: float | None
    tolerance_plus: float | None
    tolerance_minus: float | None
    catia_part_number: str
    catia_instance_name: str | None
    catia_tree_path: str | None
    publication_name: str
    parameter_path: str | None
    parameter_name: str | None
    parameter_value_text: str | None
    link_status: str | None


@dataclass
class CaptureConflictRecord:
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
    first_product: object | None
    second_product: object | None
    first_point: tuple | None
    second_point: tuple | None
    views: dict | None = None


def _print(message, progress_callback=None):
    if progress_callback:
        progress_callback(message)
    else:
        print(message)


def slugify(text):
    raw = str(text or "").strip()
    cleaned = []
    for char in raw:
        if char.isalnum():
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "_":
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "item"


def resolve_mode_target_value(linked_row, tolerance_mode):
    nominal = float(linked_row.nominal_value if linked_row.nominal_value is not None else 0.0)
    if tolerance_mode == "nominal":
        return nominal
    if tolerance_mode == "max":
        return nominal + float(linked_row.tolerance_plus if linked_row.tolerance_plus is not None else 0.0)
    if tolerance_mode == "min":
        return nominal + float(linked_row.tolerance_minus if linked_row.tolerance_minus is not None else 0.0)
    raise RuntimeError("Unsupported tolerance mode: {}".format(tolerance_mode))


def resolve_parameter_candidates(linked_row):
    candidates = []
    for value in (
        linked_row.parameter_path,
        linked_row.parameter_name,
        linked_row.publication_name,
    ):
        text = safe_text(value)
        if not text:
            continue
        candidates.append(text)
        if "\\" in text:
            candidates.append(text.split("\\")[-1])
    deduped = []
    seen = set()
    for value in candidates:
        normalized = value.strip().lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value)
    return deduped


def find_linked_parameter(parameters, linked_row):
    for candidate in resolve_parameter_candidates(linked_row):
        parameter = find_parameter_by_name(parameters, candidate)
        if parameter is not None:
            return parameter
    for parameter in iter_com_collection(parameters):
        parameter_name = safe_text(safe_com_get(parameter, "Name")) or ""
        parameter_leaf = parameter_name.split("\\")[-1].strip().lower()
        if parameter_leaf == (safe_text(linked_row.parameter_name) or "").strip().lower():
            return parameter
    raise RuntimeError(
        "Could not find CATIA parameter for dimension {} using path/name/publication {} / {} / {}.".format(
            linked_row.dimension_id,
            linked_row.parameter_path,
            linked_row.parameter_name,
            linked_row.publication_name,
        )
    )


def load_linked_dimension_rows(db_path, document_id=None):
    resolved_db_path = Path(db_path).resolve()
    if not resolved_db_path.exists():
        raise RuntimeError("SQLite database not found: {}".format(resolved_db_path))

    conn = sqlite3.connect(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT
                document_id,
                drawing_part_number,
                dimension_id,
                dimension_raw_text,
                nominal_value,
                tolerance_plus,
                tolerance_minus,
                catia_part_number,
                catia_instance_name,
                catia_tree_path,
                publication_name,
                parameter_path,
                parameter_name,
                parameter_value_text,
                link_status
            FROM catia_dimension_pairs
            WHERE COALESCE(link_status, 'linked') = 'linked'
        """
        params = []
        if document_id:
            sql += " AND document_id = ?"
            params.append(document_id)
        sql += """
            ORDER BY
                CASE
                    WHEN dimension_id GLOB 'd[0-9]*' THEN CAST(SUBSTR(dimension_id, 2) AS INTEGER)
                    ELSE 999999
                END,
                dimension_id
        """
        rows = conn.execute(sql, params).fetchall()
        return [LinkedDimensionRow(**dict(row)) for row in rows]
    finally:
        conn.close()


def ensure_root_product_updated(root_document):
    safe_com_call(root_document, "Activate")
    root_product = get_document_product(root_document)
    safe_com_call(root_product, "Update")


def apply_linked_parameter_scenario(catia, root_document, linked_rows, tolerance_mode, actuated_row=None):
    part_document_cache = {}
    for linked_row in linked_rows:
        target_document = part_document_cache.get(linked_row.catia_part_number)
        if target_document is None:
            target_document, _opened_here, _resolved_part_path = resolve_target_part_document(
                catia,
                root_document,
                linked_row.catia_part_number,
            )
            part_document_cache[linked_row.catia_part_number] = target_document

        part = get_part_from_document(target_document)
        parameters = get_parameters_collection(part)
        parameter = find_linked_parameter(parameters, linked_row)
        mode_for_row = tolerance_mode if actuated_row and linked_row.dimension_id == actuated_row.dimension_id else "nominal"
        target_value = resolve_mode_target_value(linked_row, mode_for_row)
        set_length_parameter_value(parameter, target_value)
        safe_com_call(part, "Update")
    ensure_root_product_updated(root_document)


def get_conflict_comparison_text(conflict):
    return map_conflict_comparison_info(safe_conflict_comparison_info(conflict))


def safe_conflict_point(conflict, side):
    try:
        from pycatia.space_analyses_interfaces.conflict import Conflict

        wrapped_conflict = Conflict(getattr(conflict, "com_object", conflict))
        if side == "first":
            point = wrapped_conflict.get_first_point_coordinates()
        else:
            point = wrapped_conflict.get_second_point_coordinates()
        values = tuple(float(value) for value in point[:3])
        return values if len(values) == 3 else None
    except Exception:
        return None


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


def infer_conflict_type(raw_conflict_type, conflict_value, source_label):
    resolved_type = map_conflict_type(raw_conflict_type)
    if resolved_type in ("contact", "clash", "clearance"):
        return resolved_type
    if source_label == "clearance":
        return "clearance"
    return "contact" if abs(float(conflict_value or 0.0)) <= 1e-9 else "clash"


def collect_conflict_records(contact_clash, clearance_clash):
    records = []
    number = 1
    for clash_object, required_type, source_label in (
        (contact_clash, None, "contact+clash"),
        (clearance_clash, "clearance", "clearance"),
    ):
        conflicts = getattr(clash_object, "conflicts", None)
        if callable(conflicts):
            conflicts = conflicts()
        if conflicts is None:
            conflicts = safe_com_get(getattr(clash_object, "com_object", clash_object), "Conflicts")
        for conflict in iter_conflict_items(conflicts):
            conflict_value = safe_conflict_value(conflict)
            conflict_type = infer_conflict_type(safe_conflict_type(conflict), conflict_value, source_label)
            if required_type is not None and conflict_type != required_type:
                continue
            first_product = safe_conflict_product(conflict, "first")
            second_product = safe_conflict_product(conflict, "second")
            comment = safe_conflict_comment(conflict) or ""
            records.append(
                CaptureConflictRecord(
                    number=number,
                    product1=resolve_instance_name(first_product),
                    product2=resolve_instance_name(second_product),
                    conflict_type=conflict_type,
                    value=conflict_value,
                    status=map_conflict_status(safe_conflict_status(conflict)),
                    info=get_conflict_comparison_text(conflict),
                    keep="",
                    comment=comment,
                    location="{} -> {}".format(
                        resolve_instance_name(first_product) or "?",
                        resolve_instance_name(second_product) or "?",
                    ),
                    image_path=None,
                    first_product=first_product,
                    second_product=second_product,
                    first_point=safe_conflict_point(conflict, "first"),
                    second_point=safe_conflict_point(conflict, "second"),
                    views={},
                )
            )
            number += 1
    return records


def set_products_visibility(document, products, show_value):
    selection = safe_com_get(document, "Selection")
    if selection is None or not products:
        return
    safe_com_call(selection, "Clear")
    try:
        for product in products:
            if product is None:
                continue
            safe_com_call(selection, "Add", safe_com_get(product, "com_object", product))
        vis_properties = safe_com_get(selection, "VisProperties")
        if vis_properties is not None:
            safe_com_call(vis_properties, "SetShow", show_value)
    finally:
        safe_com_call(selection, "Clear")


def set_targets_visibility(document, targets, show_value):
    products = [safe_com_get(target, "product", None) for target in targets]
    set_products_visibility(document, products, show_value)


def select_products_for_capture(document, first_product, second_product):
    selection = safe_com_get(document, "Selection")
    if selection is None:
        return None
    safe_com_call(selection, "Clear")
    if first_product is not None:
        safe_com_call(selection, "Add", safe_com_get(first_product, "com_object", first_product))
    if second_product is not None:
        safe_com_call(selection, "Add", safe_com_get(second_product, "com_object", second_product))
    return selection


def vector_subtract(left, right):
    return tuple(left[index] - right[index] for index in range(3))


def vector_add(left, right):
    return tuple(left[index] + right[index] for index in range(3))


def vector_scale(vector, scalar):
    return tuple(component * scalar for component in vector)


def vector_dot(left, right):
    return sum(left[index] * right[index] for index in range(3))


def vector_cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def vector_length(vector):
    return math.sqrt(vector_dot(vector, vector))


def normalize_vector(vector):
    length = vector_length(vector)
    if length <= 1e-9:
        return None
    return tuple(component / length for component in vector)


def midpoint_from_record(record):
    if record.first_point and record.second_point:
        return tuple((record.first_point[index] + record.second_point[index]) / 2.0 for index in range(3))
    return record.first_point or record.second_point


def get_viewpoint(viewer):
    viewpoint = safe_com_get(viewer, "Viewpoint3D")
    if viewpoint is None:
        return None
    return ViewPoint3D(viewpoint)


def capture_viewpoint_state(viewer):
    viewpoint = get_viewpoint(viewer)
    if viewpoint is None:
        return None
    try:
        return {
            "origin": tuple(viewpoint.get_origin()),
            "sight": tuple(viewpoint.get_sight_direction()),
            "up": tuple(viewpoint.get_up_direction()),
            "focus_distance": float(viewpoint.focus_distance),
            "projection_mode": int(viewpoint.projection_mode),
            "field_of_view": float(safe_com_get(viewpoint, "field_of_view", 0.0) or 0.0),
            "zoom": float(safe_com_get(viewpoint, "zoom", 1.0) or 1.0),
        }
    except Exception:
        return None


def restore_viewpoint_state(viewer, state):
    if not state:
        return
    viewpoint = get_viewpoint(viewer)
    if viewpoint is None:
        return
    try:
        viewpoint.put_origin(state["origin"])
        viewpoint.put_sight_direction(state["sight"])
        viewpoint.put_up_direction(state["up"])
        viewpoint.focus_distance = state["focus_distance"]
        if state["projection_mode"] == PROJECTION_CONIC:
            viewpoint.field_of_view = state["field_of_view"]
        elif state["projection_mode"] == PROJECTION_CYLINDRIC:
            viewpoint.zoom = state["zoom"]
    except Exception:
        return


def focus_viewpoint_on_record(viewer, record):
    midpoint = midpoint_from_record(record)
    if midpoint is None:
        return False
    viewpoint = get_viewpoint(viewer)
    if viewpoint is None:
        return False
    try:
        sight = normalize_vector(tuple(viewpoint.get_sight_direction()))
        if sight is None:
            return False
        focus_distance = float(viewpoint.focus_distance)
        new_origin = vector_subtract(midpoint, vector_scale(sight, focus_distance))
        viewpoint.put_origin(new_origin)
        projection_mode = int(viewpoint.projection_mode)
        if projection_mode == PROJECTION_CONIC:
            viewpoint.field_of_view = max(2.0, float(viewpoint.field_of_view) * 0.55)
        elif projection_mode == PROJECTION_CYLINDRIC:
            current_zoom = float(viewpoint.zoom)
            viewpoint.zoom = max(current_zoom * 4.0, current_zoom + 0.002)
        return True
    except Exception:
        return False


def rotate_vector_around_axis(vector, axis, angle_degrees):
    normalized_axis = normalize_vector(axis)
    if normalized_axis is None:
        return vector
    radians = math.radians(angle_degrees)
    cos_theta = math.cos(radians)
    sin_theta = math.sin(radians)
    axis_dot_vector = vector_dot(normalized_axis, vector)
    first = vector_scale(vector, cos_theta)
    second = vector_scale(vector_cross(normalized_axis, vector), sin_theta)
    third = vector_scale(normalized_axis, axis_dot_vector * (1.0 - cos_theta))
    return vector_add(vector_add(first, second), third)


def apply_view_variant(viewer, base_state, record, yaw_degrees=0.0, pitch_degrees=0.0):
    midpoint = midpoint_from_record(record)
    viewpoint = get_viewpoint(viewer)
    if viewpoint is None:
        return False
    try:
        sight = normalize_vector(base_state["sight"])
        up = normalize_vector(base_state["up"])
        if sight is None or up is None:
            restore_viewpoint_state(viewer, base_state)
            return False
        right = normalize_vector(vector_cross(sight, up))
        if right is None:
            restore_viewpoint_state(viewer, base_state)
            return False
        rotated_sight = sight
        rotated_up = up
        if yaw_degrees:
            rotated_sight = rotate_vector_around_axis(rotated_sight, rotated_up, yaw_degrees)
            right = normalize_vector(vector_cross(rotated_sight, rotated_up)) or right
        if pitch_degrees:
            rotated_sight = rotate_vector_around_axis(rotated_sight, right, pitch_degrees)
            rotated_up = rotate_vector_around_axis(rotated_up, right, pitch_degrees)
        rotated_sight = normalize_vector(rotated_sight) or sight
        rotated_up = normalize_vector(rotated_up) or up
        viewpoint.put_sight_direction(rotated_sight)
        viewpoint.put_up_direction(rotated_up)
        if midpoint is not None:
            new_origin = vector_subtract(midpoint, vector_scale(rotated_sight, base_state["focus_distance"]))
        else:
            new_origin = base_state["origin"]
        viewpoint.put_origin(new_origin)
        viewpoint.focus_distance = base_state["focus_distance"]
        if base_state["projection_mode"] == PROJECTION_CONIC:
            viewpoint.field_of_view = base_state["field_of_view"]
        elif base_state["projection_mode"] == PROJECTION_CYLINDRIC:
            viewpoint.zoom = base_state["zoom"]
        return True
    except Exception:
        restore_viewpoint_state(viewer, base_state)
        return False


def project_world_point_to_image(world_point, view_state, image_size):
    if world_point is None or view_state is None:
        return None
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    sight = normalize_vector(view_state.get("sight"))
    up = normalize_vector(view_state.get("up"))
    if sight is None or up is None:
        return None
    right = normalize_vector(vector_cross(sight, up))
    if right is None:
        return None
    relative = vector_subtract(world_point, view_state.get("origin"))
    x_cam = vector_dot(relative, right)
    y_cam = vector_dot(relative, up)
    z_cam = vector_dot(relative, sight)
    if z_cam <= 1e-6:
        return None
    aspect = float(width) / float(max(1, height))
    projection_mode = int(view_state.get("projection_mode", PROJECTION_CONIC))
    if projection_mode == PROJECTION_CONIC:
        field_of_view = float(view_state.get("field_of_view", 0.0) or 0.0)
        tan_v = math.tan(math.radians(field_of_view))
        tan_h = tan_v * aspect
        if abs(tan_v) <= 1e-9 or abs(tan_h) <= 1e-9:
            return None
        x_norm = 0.5 + (x_cam / (z_cam * tan_h)) * 0.5
        y_norm = 0.5 - (y_cam / (z_cam * tan_v)) * 0.5
    elif projection_mode == PROJECTION_CYLINDRIC:
        zoom = float(view_state.get("zoom", 1.0) or 1.0)
        focus_distance = float(view_state.get("focus_distance", 1.0) or 1.0)
        half_vertical = focus_distance / max(zoom, 1e-9)
        half_horizontal = half_vertical * aspect
        if half_vertical <= 1e-9 or half_horizontal <= 1e-9:
            return None
        x_norm = 0.5 + (x_cam / half_horizontal) * 0.5
        y_norm = 0.5 - (y_cam / half_vertical) * 0.5
    else:
        return None
    if not (math.isfinite(x_norm) and math.isfinite(y_norm)):
        return None
    return (x_norm * width, y_norm * height)


def transform_projected_point(projected_point, crop_info):
    if projected_point is None or crop_info is None:
        return None
    left = float(crop_info.get("left", 0.0))
    top = float(crop_info.get("top", 0.0))
    cropped_width = float(crop_info.get("cropped_width", 0.0))
    cropped_height = float(crop_info.get("cropped_height", 0.0))
    output_width = float(crop_info.get("output_width", 0.0))
    output_height = float(crop_info.get("output_height", 0.0))
    if cropped_width <= 0.0 or cropped_height <= 0.0 or output_width <= 0.0 or output_height <= 0.0:
        return None
    raw_x, raw_y = projected_point
    local_x = raw_x - left
    local_y = raw_y - top
    if local_x < 0.0 or local_y < 0.0 or local_x > cropped_width or local_y > cropped_height:
        return None
    scale_x = output_width / cropped_width
    scale_y = output_height / cropped_height
    return (local_x * scale_x, local_y * scale_y)


def annotate_capture_image(image_path, record, view_label, show_conflict_label=True, note_text=None, marker_point=None):
    image_file = Path(image_path)
    if not image_file.exists():
        return

    with Image.open(image_file) as source_image:
        canvas = source_image.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.8, percent=140, threshold=2))

    image_rgba = canvas.convert("RGBA")
    overlay = Image.new("RGBA", image_rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    marker_color = {
        "clash": (220, 50, 47, 255),
        "contact": (203, 75, 22, 255),
        "clearance": (38, 139, 210, 255),
    }.get(record.conflict_type, (255, 214, 102, 255))

    display_view_label = VIEW_DISPLAY_NAMES.get(str(view_label).lower(), str(view_label).upper())

    view_box = (18, 18, 170, 56)
    draw.rounded_rectangle(view_box, radius=10, fill=(18, 24, 36, 215), outline=(110, 160, 220, 255), width=2)
    draw.text((view_box[0] + 12, view_box[1] + 10), display_view_label, fill=(235, 245, 255, 255))

    if show_conflict_label:
        info_box = (18, 66, 360, 130)
        draw.rounded_rectangle(info_box, radius=10, fill=(18, 24, 36, 220), outline=marker_color, width=2)
        draw.text(
            (info_box[0] + 12, info_box[1] + 10),
            "{} {:.3f} mm".format(str(record.conflict_type).upper(), float(record.value or 0.0)),
            fill=(255, 245, 210, 255),
        )
        draw.text(
            (info_box[0] + 12, info_box[1] + 34),
            "{} <-> {}".format(record.product1 or "?", record.product2 or "?"),
            fill=(220, 225, 235, 255),
        )

    if marker_point is not None:
        label_center_x, label_center_y = marker_point
        radius = max(18, min(42, int(min(image_rgba.size) * 0.055)))
        left = label_center_x - radius
        top = label_center_y - radius
        right = label_center_x + radius
        bottom = label_center_y + radius
        draw.ellipse((left, top, right, bottom), outline=marker_color, width=5)
        callout_width = 280
        callout_height = 50
        if label_center_x < image_rgba.width * 0.58:
            callout_left = min(image_rgba.width - callout_width - 18, label_center_x + 52)
            line_anchor_x = callout_left
        else:
            callout_left = max(18, label_center_x - callout_width - 52)
            line_anchor_x = callout_left + callout_width
        callout_top = max(142, min(image_rgba.height - callout_height - 18, label_center_y - 20))
        callout_box = (callout_left, callout_top, callout_left + callout_width, callout_top + callout_height)
        draw.rounded_rectangle(callout_box, radius=10, fill=(18, 24, 36, 220), outline=marker_color, width=2)
        draw.text(
            (callout_box[0] + 12, callout_box[1] + 15),
            "{} area".format(str(record.conflict_type).capitalize()),
            fill=(255, 245, 210, 255),
        )
        draw.line(
            (label_center_x, label_center_y, line_anchor_x, callout_box[1] + callout_height / 2.0),
            fill=marker_color,
            width=3,
        )

    composed = Image.alpha_composite(image_rgba, overlay).convert("RGB")
    composed.save(image_file, format="PNG", optimize=True)


def find_highlight_bbox(image):
    width, height = image.size
    pixels = image.load()
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    search_left = int(width * 0.18)
    search_right = width
    search_top = int(height * 0.06)
    search_bottom = int(height * 0.92)
    for y in range(search_top, search_bottom):
        for x in range(search_left, search_right):
            red, green, blue = pixels[x, y][:3]
            is_orange = red >= 140 and green >= 70 and blue <= 120 and red >= blue + 50
            is_yellow = red >= 170 and green >= 130 and blue <= 140
            if not (is_orange or is_yellow):
                continue
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    if max_x < min_x or max_y < min_y:
        return None
    return (min_x, min_y, max_x, max_y)


def extract_highlight_points(image):
    bbox = find_highlight_bbox(image)
    if bbox is None:
        return [], None
    min_x, min_y, max_x, max_y = bbox
    pixels = image.load()
    points = []
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            red, green, blue = pixels[x, y][:3]
            is_orange = red >= 140 and green >= 70 and blue <= 120 and red >= blue + 50
            is_yellow = red >= 170 and green >= 130 and blue <= 150
            if is_orange or is_yellow:
                points.append((x, y))
    return points, bbox


def estimate_approximate_conflict_zones(image):
    points, bbox = extract_highlight_points(image)
    if not points:
        return []

    count = float(len(points))
    mean_x = sum(point[0] for point in points) / count
    mean_y = sum(point[1] for point in points) / count
    cov_xx = sum((point[0] - mean_x) ** 2 for point in points) / count
    cov_yy = sum((point[1] - mean_y) ** 2 for point in points) / count
    cov_xy = sum((point[0] - mean_x) * (point[1] - mean_y) for point in points) / count
    angle = 0.5 * math.atan2(2.0 * cov_xy, cov_xx - cov_yy)
    axis = (math.cos(angle), math.sin(angle))
    perp = (-axis[1], axis[0])
    axis_length = math.sqrt(max(cov_xx, 0.0) + max(cov_yy, 0.0))
    if axis_length <= 1e-6:
        return []

    projections = []
    for x, y in points:
        projection = (x - mean_x) * axis[0] + (y - mean_y) * axis[1]
        perp_distance = (x - mean_x) * perp[0] + (y - mean_y) * perp[1]
        projections.append((projection, perp_distance, x, y))
    projections.sort(key=lambda item: item[0])

    point_set = set(points)
    radius = max(28, int(axis_length * 0.14))
    radius_sq = radius * radius
    min_projection = projections[0][0]
    max_projection = projections[-1][0]
    projection_span = max_projection - min_projection
    central_band = max(12.0, projection_span * 0.22)
    central_perp_distances = [
        abs(perp_distance)
        for projection, perp_distance, x, y in projections
        if abs(projection) <= central_band
    ]
    if not central_perp_distances:
        central_perp_distances = [
            abs((x - mean_x) * perp[0] + (y - mean_y) * perp[1])
            for x, y in points
        ]
    body_half_width = sorted(central_perp_distances)[max(0, int(len(central_perp_distances) * 0.80) - 1)] if central_perp_distances else 1.0
    body_half_width = max(body_half_width, 1.0)

    def boundary_ratio(center):
        cx, cy = center
        total = 0
        boundary = 0
        local_max_perp = 0.0
        for x, y in points:
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy > radius_sq:
                continue
            total += 1
            local_max_perp = max(local_max_perp, abs((x - mean_x) * perp[0] + (y - mean_y) * perp[1]))
            if (
                (x - 1, y) not in point_set
                or (x + 1, y) not in point_set
                or (x, y - 1) not in point_set
                or (x, y + 1) not in point_set
            ):
                boundary += 1
        if total <= 0:
            return 0.0, 0, 0.0
        return float(boundary) / float(total), total, local_max_perp

    def average_point(group):
        return (
            sum(item[2] for item in group) / float(len(group)),
            sum(item[3] for item in group) / float(len(group)),
        )

    # First, try to detect a local protrusion/feature zone along the body rather than
    # defaulting to an end face. This matches the visible screw/washer area much better.
    zones = []
    if projection_span > 1e-6:
        bin_count = 36
        bin_width = projection_span / float(bin_count)
        inner_min = min_projection + projection_span * 0.22
        inner_max = max_projection - projection_span * 0.22
        outer_feature_points = [
            item
            for item in projections
            if inner_min <= item[0] <= inner_max and abs(item[1]) > body_half_width * 1.08
        ]
        best_bin = None
        best_score = 0.0
        for bin_index in range(bin_count):
            start = min_projection + bin_index * bin_width
            end = start + bin_width
            center_t = (start + end) / 2.0
            if center_t < inner_min or center_t > inner_max:
                continue
            bin_points = [item for item in outer_feature_points if start <= item[0] < end]
            if len(bin_points) < 8:
                continue
            avg_excess = sum(max(0.0, abs(item[1]) - body_half_width) for item in bin_points) / float(len(bin_points))
            center_x = sum(item[2] for item in bin_points) / float(len(bin_points))
            center_y = sum(item[3] for item in bin_points) / float(len(bin_points))
            ratio, local_count, _local_max_perp = boundary_ratio((center_x, center_y))
            local_score = avg_excess * 3.5 + ratio * 1.5 + float(local_count) * 0.08
            if local_score > best_score:
                best_score = local_score
                best_bin = (center_x, center_y, avg_excess)
        if best_bin is not None:
            center_x, center_y, avg_excess = best_bin
            zone_radius = max(22, min(52, int(avg_excess * 1.8 + 18)))
            zones.append((center_x, center_y, zone_radius))

    # If no internal feature is obvious, fall back to the more generic end-based estimate.
    if not zones:
        sample_count = max(1, min(60, len(projections) // 8 or 1))
        low_group = projections[:sample_count]
        high_group = projections[-sample_count:]
        endpoint_a = average_point(low_group)
        endpoint_b = average_point(high_group)
        ratio_a, count_a, perp_a = boundary_ratio(endpoint_a)
        ratio_b, count_b, perp_b = boundary_ratio(endpoint_b)
        protrusion_a = max(0.0, (perp_a - body_half_width) / body_half_width)
        protrusion_b = max(0.0, (perp_b - body_half_width) / body_half_width)
        score_a = (ratio_a * max(1.0, math.sqrt(float(count_a)))) + (protrusion_a * 8.0)
        score_b = (ratio_b * max(1.0, math.sqrt(float(count_b)))) + (protrusion_b * 8.0)
        preferred = endpoint_a if score_a >= score_b else endpoint_b
        zones.append((preferred[0], preferred[1], radius))

    min_x, min_y, max_x, max_y = bbox
    clamped = []
    for cx, cy, r in zones[:2]:
        clamped.append(
            (
                max(min_x + r * 0.6, min(max_x - r * 0.6, cx)),
                max(min_y + r * 0.6, min(max_y - r * 0.6, cy)),
                r,
            )
        )
    return clamped


def crop_capture_to_focus_region(image):
    bbox = find_highlight_bbox(image)
    if bbox is None:
        return image, {
            "left": 0,
            "top": 0,
            "cropped_width": image.size[0],
            "cropped_height": image.size[1],
            "output_width": image.size[0],
            "output_height": image.size[1],
        }
    min_x, min_y, max_x, max_y = bbox
    width, height = image.size
    box_width = max_x - min_x + 1
    box_height = max_y - min_y + 1
    pad_x = max(60, int(box_width * 0.75))
    pad_y = max(60, int(box_height * 0.9))
    left_ui_boundary = int(width * 0.18)
    top_ui_boundary = int(height * 0.06)
    bottom_ui_boundary = int(height * 0.92)
    left = max(left_ui_boundary, min_x - pad_x)
    top = max(top_ui_boundary, min_y - pad_y)
    right = min(width, max_x + pad_x)
    bottom = min(bottom_ui_boundary, max_y + pad_y)
    cropped = image.crop((left, top, right, bottom))
    crop_width = cropped.width
    crop_height = cropped.height
    max_width = 1200
    max_height = 900
    if cropped.width > max_width or cropped.height > max_height:
        cropped.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    min_width = 900
    min_height = 620
    if cropped.width < min_width or cropped.height < min_height:
        scale = min(2.0, max(min_width / max(1, cropped.width), min_height / max(1, cropped.height)))
        resized_width = max(1, int(cropped.width * scale))
        resized_height = max(1, int(cropped.height * scale))
        cropped = cropped.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    return cropped, {
        "left": left,
        "top": top,
        "cropped_width": crop_width,
        "cropped_height": crop_height,
        "output_width": cropped.width,
        "output_height": cropped.height,
    }


def capture_single_view(viewer, output_path, record, view_label, view_state):
    with tempfile.NamedTemporaryFile(prefix="layer3_view_", suffix=".bmp", delete=False) as temp_handle:
        temp_path = Path(temp_handle.name)
    try:
        safe_com_call(viewer, "CaptureToFile", CAPTURE_FORMAT_BMP, str(temp_path))
        with Image.open(temp_path) as source_image:
            image = source_image.convert("RGB")
            projected_point = project_world_point_to_image(midpoint_from_record(record), view_state, image.size)
            focused, crop_info = crop_capture_to_focus_region(image)
            marker_point = transform_projected_point(projected_point, crop_info)
            focused.save(output_path, format="PNG", optimize=True)
            return marker_point
    finally:
        temp_path.unlink(missing_ok=True)


def capture_conflict_views(catia, document, records, all_targets):
    if not records:
        return []
    active_window = safe_com_get(catia, "ActiveWindow")
    viewer = safe_com_get(active_window, "ActiveViewer") if active_window is not None else None
    if viewer is None:
        return ["CATIA active viewer is not available for image capture."]

    warnings = []
    original_view_state = capture_viewpoint_state(viewer)
    try:
        set_targets_visibility(document, all_targets, VIS_HIDE)
        for record in records:
            set_products_visibility(document, [record.first_product, record.second_product], VIS_SHOW)
            selection = select_products_for_capture(document, record.first_product, record.second_product)
            safe_com_call(viewer, "Reframe")
            focus_viewpoint_on_record(viewer, record)
            safe_com_call(viewer, "Update")
            base_record_view_state = capture_viewpoint_state(viewer) or original_view_state
            row_views = {}
            try:
                for view_name, yaw_degrees, pitch_degrees in VIEW_SPECS:
                    variant_ok = apply_view_variant(
                        viewer,
                        base_record_view_state,
                        record,
                        yaw_degrees=yaw_degrees,
                        pitch_degrees=pitch_degrees,
                    )
                    if not variant_ok:
                        # CATIA does not always expose conflict points for every row. In that case,
                        # keep the isolated pair in view and still save the screenshots so the UI
                        # can show the affected products instead of an empty preview.
                        restore_viewpoint_state(viewer, base_record_view_state or original_view_state)
                    elif midpoint_from_record(record) is None and selection is not None:
                        # When CATIA does not expose precise conflict coordinates, use the current
                        # pair selection to refit the camera after each rotation so the part stays
                        # visible in the captured view.
                        safe_com_call(viewer, "Reframe")
                    safe_com_call(viewer, "Update")
                    output_path = Path(record.image_path).parent / "row_{:03d}_{}.png".format(record.number, view_name)
                    current_view_state = capture_viewpoint_state(viewer)
                    marker_point = capture_single_view(viewer, output_path, record, view_name, current_view_state)
                    row_views[view_name] = {
                        "path": str(output_path.resolve()),
                        "marker_point": marker_point,
                    }
            except Exception as exc:
                warnings.append(
                    "Preview capture failed for {} <-> {} ({}): {}".format(
                        record.product1,
                        record.product2,
                        record.conflict_type,
                        exc,
                    )
                )
            finally:
                restore_viewpoint_state(viewer, original_view_state or base_record_view_state)
            for view_name, view_details in row_views.items():
                annotate_capture_image(
                    view_details["path"],
                    record,
                    view_name,
                    show_conflict_label=True,
                    note_text=None,
                    marker_point=view_details.get("marker_point"),
                )
            record.views = {view_name: details["path"] for view_name, details in row_views.items()}
            record.image_path = record.views.get("iso") or next(iter(record.views.values()), None)
            set_products_visibility(document, [record.first_product, record.second_product], VIS_HIDE)
            if selection is not None:
                safe_com_call(selection, "Clear")
    finally:
        restore_viewpoint_state(viewer, original_view_state)
        set_targets_visibility(document, all_targets, VIS_SHOW)
    return warnings


def run_label_for_row(linked_row, tolerance_mode):
    if linked_row is None:
        return "nominal"
    return "{} {}".format(linked_row.dimension_id.upper(), tolerance_mode.upper())


def build_run_id(document_id, linked_row, tolerance_mode):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if linked_row is None:
        return "layer3__{}__nominal__{}".format(slugify(document_id), timestamp)
    return "layer3__{}__{}__{}__{}".format(
        slugify(document_id),
        slugify(linked_row.dimension_id),
        tolerance_mode,
        timestamp,
    )


def build_run_paths(base_output_root, document_id, linked_row, tolerance_mode):
    document_folder = Path(base_output_root).resolve() / slugify(document_id)
    if linked_row is None:
        run_root = document_folder / "nominal"
    else:
        dimension_folder = "{}_{}".format(
            slugify(linked_row.dimension_id),
            slugify(linked_row.parameter_name or linked_row.publication_name),
        )
        run_root = document_folder / dimension_folder / tolerance_mode
    images_dir = run_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    return {
        "root": run_root,
        "images": images_dir,
        "json": run_root / "results.json",
        "csv": run_root / "results.csv",
        "txt": run_root / "results.txt",
        "summary": run_root / "run_summary.json",
    }


def build_results_payload(run_id, document_summary, selected_part_number, clearance_mm, records, warnings):
    results = []
    for record in records:
        results.append(
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
                "Views": record.views or {},
            }
        )
    return {
        "run_id": run_id,
        "document_name": document_summary["name"],
        "document_path": document_summary["path"],
        "analysis_mode": MODE_SELECTION_AGAINST_ALL,
        "analysis_type": ANALYSIS_TYPE_LABEL,
        "clearance_mm": clearance_mm,
        "selected_part_number": selected_part_number,
        "warnings": list(warnings or []),
        "results": results,
    }


def write_results_files(paths, payload):
    paths["json"].write_text(json.dumps(payload, indent=2), encoding="utf-8")
    headers = ["No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location", "Image"]
    rows = [[str(result.get(header, "")) for header in headers] for result in payload.get("results", [])]
    import csv

    with paths["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def format_row(values):
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(values))

    lines = [format_row(headers), "-+-".join("-" * width for width in widths)]
    lines.extend(format_row(row) for row in rows)
    paths["txt"].write_text("\n".join(lines), encoding="utf-8")


def emit_terminal_results_table(payload, progress_callback=None, title=None):
    headers = ["No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location"]
    rows = [[str(result.get(header, "")) for header in headers] for result in payload.get("results", [])]
    if title:
        _print(title, progress_callback)
    if not rows:
        _print("  No DMU result rows were returned for this run.", progress_callback)
        return

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def format_row(values):
        return " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(values))

    _print(format_row(headers), progress_callback)
    _print("-+-".join("-" * width for width in widths), progress_callback)
    for row in rows:
        _print(format_row(row), progress_callback)


def build_run_summary_payload(run_id, linked_row, tolerance_mode, target_value_mm, selected_part_number, paths, payload):
    type_counts = {"clash": 0, "contact": 0, "clearance": 0}
    for row in payload.get("results", []):
        conflict_type = str(row.get("Type") or "").strip().lower()
        type_counts[conflict_type] = int(type_counts.get(conflict_type, 0)) + 1
    return {
        "run_id": run_id,
        "run_label": run_label_for_row(linked_row, tolerance_mode),
        "tolerance_mode": tolerance_mode,
        "dimension_id": linked_row.dimension_id if linked_row is not None else None,
        "dimension_raw_text": linked_row.dimension_raw_text if linked_row is not None else None,
        "nominal_value": linked_row.nominal_value if linked_row is not None else None,
        "tolerance_plus": linked_row.tolerance_plus if linked_row is not None else None,
        "tolerance_minus": linked_row.tolerance_minus if linked_row is not None else None,
        "catia_part_number": linked_row.catia_part_number if linked_row is not None else selected_part_number,
        "publication_name": linked_row.publication_name if linked_row is not None else None,
        "parameter_name": linked_row.parameter_name if linked_row is not None else None,
        "parameter_path": linked_row.parameter_path if linked_row is not None else None,
        "target_value_mm": target_value_mm,
        "analysis_mode": payload.get("analysis_mode"),
        "analysis_type": payload.get("analysis_type"),
        "selected_part_number": selected_part_number,
        "result_row_count": len(payload.get("results", [])),
        "clash_count": int(type_counts.get("clash", 0)),
        "contact_count": int(type_counts.get("contact", 0)),
        "clearance_count": int(type_counts.get("clearance", 0)),
        "results_json_path": str(paths["json"]),
        "results_csv_path": str(paths["csv"]),
        "results_txt_path": str(paths["txt"]),
        "images_dir": str(paths["images"]),
        "created_at_utc": utc_now_iso(),
    }


def open_run_preview_ui(results_json_path, run_summary_json_path, progress_callback=None):
    _print("Layer 3 - Run Results UI: opening detailed result viewer", progress_callback)
    ui_script_path = Path(__file__).resolve().parent / "layer3_run_ui.py"
    completed = subprocess.run(
        [sys.executable, str(ui_script_path), "--results_json", str(results_json_path), "--run_summary_json", str(run_summary_json_path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        warning = completed.stderr.strip() or completed.stdout.strip() or "unknown UI error"
        _print("Layer 3 - Run Results UI warning: {}".format(warning), progress_callback)


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


def run_dmu_cycle(
    catia,
    root_document,
    selected_part_number,
    paths,
    run_id,
    linked_row=None,
    tolerance_mode="nominal",
    target_value_mm=None,
    clearance_mm=5.0,
    progress_callback=None,
):
    safe_com_call(root_document, "Activate")
    requested_workbench, current_workbench = start_dmu_workbench(catia)
    document_summary = get_document_summary(root_document)
    root_product = get_document_product(root_document)
    scope = build_scope_for_selection_against_all(root_product, document_summary, selected_part_number)
    temporary_clash_names = []
    try:
        _print(
            "Layer 3 - DMU Setup: opening the DMU workbench and preparing selection-against-all for part {}".format(
                selected_part_number
            ),
            progress_callback,
        )
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
        _print(
            "Layer 3 - DMU Analysis: computing contact, clash, and clearance rows for the current CATIA state",
            progress_callback,
        )
        records = collect_conflict_records(contact_clash, clearance_clash)
        for record in records:
            record.image_path = str((paths["images"] / "row_{:03d}_iso.png".format(record.number)).resolve())
        _print(
            "Layer 3 - Image Capture: saving per-row screenshots for the DMU results and preparing the review UI",
            progress_callback,
        )
        warnings = capture_conflict_views(catia, root_document, records, scope.all_targets)
        payload = build_results_payload(run_id, document_summary, selected_part_number, clearance_mm, records, warnings)
        _print(
            "Layer 3 - Output Writer: saving DMU tables, JSON, TXT, CSV, and image paths for run {}".format(run_id),
            progress_callback,
        )
        write_results_files(paths, payload)
        summary_payload = build_run_summary_payload(
            run_id,
            linked_row,
            tolerance_mode,
            target_value_mm,
            selected_part_number,
            paths,
            payload,
        )
        paths["summary"].write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
        summary_payload["requested_workbench"] = requested_workbench
        summary_payload["current_workbench"] = current_workbench
        return summary_payload
    finally:
        cleanup_temporary_clashes(root_product, temporary_clash_names)
        cleanup_temporary_groups(root_product, scope.temporary_group_names)


def import_run_to_sqlite(db_path, tolerance_mode, document_id, drawing_part_number, selected_part_number, run_summary, linked_row=None):
    run_metadata = {
        "dimension_id": linked_row.dimension_id if linked_row is not None else None,
        "dimension_raw_text": linked_row.dimension_raw_text if linked_row is not None else None,
        "catia_part_number": linked_row.catia_part_number if linked_row is not None else None,
        "publication_name": linked_row.publication_name if linked_row is not None else None,
        "parameter_name": linked_row.parameter_name if linked_row is not None else None,
        "parameter_path": linked_row.parameter_path if linked_row is not None else None,
        "target_value_mm": run_summary.get("target_value_mm"),
        "ui_opened": False,
    }
    return import_catia_dmu_results_to_sqlite(
        db_path=db_path,
        tolerance_mode=tolerance_mode,
        document_id=document_id,
        drawing_part_number=drawing_part_number,
        dmu_inputs={"part_number": selected_part_number},
        results_json_path=run_summary["results_json_path"],
        created_at_utc=run_summary["created_at_utc"],
        run_metadata=run_metadata,
    )


def run_layer3_tolerance_sweep_to_sqlite(
    db_path,
    document_id,
    drawing_part_number,
    target_part_number,
    root_document_path=None,
    clearance_mm=5.0,
    output_root=DEFAULT_LAYER3_RESULT_ROOT,
    summary_json=None,
    open_result_ui=True,
    progress_callback=None,
):
    if not target_part_number:
        return {
            "status": "skipped",
            "reason": "no CATIA Layer 3 target part number was provided or inferred",
        }

    drawing_identity = resolve_drawing_identity_from_db(db_path, document_id=document_id)
    document_id = drawing_identity["document_id"]
    drawing_part_number = drawing_part_number or drawing_identity["drawing_part_number"]
    linked_rows = load_linked_dimension_rows(db_path, document_id=document_id)
    catia = connect_catia()
    root_document, _opened_here = resolve_document(catia, root_document_path)

    if not linked_rows:
        _print(
            "Layer 3 Stage 01/04 - Nominal DMU Baseline: no linked CATIA dimensions were found, so Layer 3 will only analyze the current nominal assembly",
            progress_callback,
        )
    else:
        _print(
            "Layer 3 Stage 01/04 - Nominal DMU Baseline: restoring all linked parameters to nominal values before the baseline DMU run",
            progress_callback,
        )
    apply_linked_parameter_scenario(catia, root_document, linked_rows, "nominal", actuated_row=None)
    nominal_paths = build_run_paths(output_root, document_id, None, "nominal")
    nominal_run_id = build_run_id(document_id, None, "nominal")
    nominal_summary = run_dmu_cycle(
        catia,
        root_document,
        target_part_number,
        nominal_paths,
        nominal_run_id,
        linked_row=None,
        tolerance_mode="nominal",
        target_value_mm=None,
        clearance_mm=clearance_mm,
        progress_callback=progress_callback,
    )
    nominal_import = import_run_to_sqlite(
        db_path,
        "nominal",
        document_id,
        drawing_part_number,
        target_part_number,
        nominal_summary,
        linked_row=None,
    )
    _print(
        "Layer 3 Result - Nominal Run: rows={} | clash={} | contact={} | clearance={} | table={} + {}".format(
            nominal_summary.get("result_row_count", 0),
            nominal_summary.get("clash_count", 0),
            nominal_summary.get("contact_count", 0),
            nominal_summary.get("clearance_count", 0),
            nominal_import.get("runs_table_name", "catia_dmu_runs_nominal"),
            nominal_import.get("table_name", "catia_dmu_results_nominal"),
        ),
        progress_callback,
    )
    emit_terminal_results_table(
        {
            "results": nominal_import.get("results", []),
        }
        if nominal_import.get("results") is not None
        else json.loads(Path(nominal_summary["results_json_path"]).read_text(encoding="utf-8")),
        progress_callback=progress_callback,
        title="Layer 3 Result Table - Nominal:",
    )

    max_runs = []
    min_runs = []
    total_max_rows = 0
    total_min_rows = 0

    for linked_row in linked_rows:
        for tolerance_mode, bucket, total_key in (
            ("max", max_runs, "max"),
            ("min", min_runs, "min"),
        ):
            target_value_mm = resolve_mode_target_value(linked_row, tolerance_mode)
            _print(
                "Layer 3 Stage 02/04 - {} {}: actuating {}={} while all other linked parameters stay nominal, then running DMU".format(
                    linked_row.dimension_id.upper(),
                    tolerance_mode.upper(),
                    linked_row.parameter_name or linked_row.publication_name,
                    target_value_mm,
                ),
                progress_callback,
            )
            apply_linked_parameter_scenario(catia, root_document, linked_rows, tolerance_mode, actuated_row=linked_row)
            run_paths = build_run_paths(output_root, document_id, linked_row, tolerance_mode)
            run_id = build_run_id(document_id, linked_row, tolerance_mode)
            run_summary = run_dmu_cycle(
                catia,
                root_document,
                target_part_number,
                run_paths,
                run_id,
                linked_row=linked_row,
                tolerance_mode=tolerance_mode,
                target_value_mm=target_value_mm,
                clearance_mm=clearance_mm,
                progress_callback=progress_callback,
            )
            sqlite_summary = import_run_to_sqlite(
                db_path,
                tolerance_mode,
                document_id,
                drawing_part_number,
                target_part_number,
                run_summary,
                linked_row=linked_row,
            )
            run_summary["stored_row_count"] = sqlite_summary.get("stored_row_count", run_summary.get("result_row_count", 0))
            bucket.append(run_summary)
            if tolerance_mode == "max":
                total_max_rows += int(run_summary.get("result_row_count", 0))
            else:
                total_min_rows += int(run_summary.get("result_row_count", 0))
            _print(
                "Layer 3 Result - {} {}: rows={} | clash={} | contact={} | clearance={} | table={} + {}".format(
                    linked_row.dimension_id.upper(),
                    tolerance_mode.upper(),
                    run_summary.get("result_row_count", 0),
                    run_summary.get("clash_count", 0),
                    run_summary.get("contact_count", 0),
                    run_summary.get("clearance_count", 0),
                    sqlite_summary.get("runs_table_name", "catia_dmu_runs_{}".format(tolerance_mode)),
                    sqlite_summary.get("table_name", "catia_dmu_results_{}".format(tolerance_mode)),
                ),
                progress_callback,
            )
            emit_terminal_results_table(
                json.loads(Path(run_summary["results_json_path"]).read_text(encoding="utf-8")),
                progress_callback=progress_callback,
                title="Layer 3 Result Table - {} {}:".format(
                    linked_row.dimension_id.upper(),
                    tolerance_mode.upper(),
                ),
            )
            if open_result_ui:
                open_run_preview_ui(
                    run_summary["results_json_path"],
                    str(Path(run_summary["results_json_path"]).with_name("run_summary.json")),
                    progress_callback=progress_callback,
                )

    _print("Layer 3 Stage 04/04 - Restoring all linked parameters to nominal values after max/min runs", progress_callback)
    apply_linked_parameter_scenario(catia, root_document, linked_rows, "nominal", actuated_row=None)
    ensure_root_product_updated(root_document)

    result = {
        "status": "completed",
        "analysis_mode": MODE_SELECTION_AGAINST_ALL,
        "analysis_type": ANALYSIS_TYPE_LABEL,
        "target_part_number": target_part_number,
        "linked_dimensions": len(linked_rows),
        "nominal_rows": nominal_summary.get("result_row_count", 0),
        "nominal_run_id": nominal_summary.get("run_id"),
        "nominal_results_txt_path": nominal_summary.get("results_txt_path"),
        "nominal_tables": {
            "runs": nominal_import.get("runs_table_name"),
            "results": nominal_import.get("table_name"),
        },
        "max_runs": len(max_runs),
        "max_dmu_rows": total_max_rows,
        "min_runs": len(min_runs),
        "min_dmu_rows": total_min_rows,
        "layer3_output_root": str((Path(output_root).resolve() / slugify(document_id))),
    }
    summary_path = resolve_summary_json_path(summary_json, default_file_name="catia_layer3_summary.json")
    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["summary_json_path"] = str(summary_path)
    return result
