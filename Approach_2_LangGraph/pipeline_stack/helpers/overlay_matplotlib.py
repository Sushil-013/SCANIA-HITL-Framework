from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle


CATEGORY_STYLES = {
    "dimension": {"color": "#f30ad4", "linewidth": 1.8},
    "feature_control_frame": {"color": "#8338ec", "linewidth": 1.8},
    "datum": {"color": "#fb8500", "linewidth": 1.8},
}


def _iter_dimension_rows(result):
    dimension_counter = 1
    for view in result.get("views", []):
        for dimension in view.get("dimensions", []):
            yield {
                "category": "dimension",
                "bbox": dimension.get("bbox"),
                "label": "D{}".format(dimension_counter),
                "item": dimension,
            }
            dimension_counter += 1
        for datum in view.get("datums", []):
            yield {
                "category": "datum",
                "bbox": datum.get("bbox"),
                "label": datum.get("label") or datum.get("datum_id") or "DATUM",
                "item": datum,
            }
        for fcf in view.get("feature_control_frames", []):
            yield {
                "category": "feature_control_frame",
                "bbox": fcf.get("bbox"),
                "label": fcf.get("raw_text") or fcf.get("fcf_id") or "FCF",
                "item": fcf,
            }

    for dimension in result.get("unassigned", {}).get("dimensions", []):
        yield {
            "category": "dimension",
            "bbox": dimension.get("bbox"),
            "label": "D{}".format(dimension_counter),
            "item": dimension,
        }
        dimension_counter += 1
    for datum in result.get("unassigned", {}).get("datums", []):
        yield {
            "category": "datum",
            "bbox": datum.get("bbox"),
            "label": datum.get("label") or datum.get("datum_id") or "DATUM",
            "item": datum,
        }
    for fcf in result.get("unassigned", {}).get("feature_control_frames", []):
        yield {
            "category": "feature_control_frame",
            "bbox": fcf.get("bbox"),
            "label": fcf.get("raw_text") or fcf.get("fcf_id") or "FCF",
            "item": fcf,
        }


def _valid_bbox(bbox):
    return isinstance(bbox, list) and len(bbox) == 4


def save_result_overlay(
    prepared_image_path,
    result,
    output_path,
    output_dpi=None,
    show_labels=True,
    include_feature_control_frames=False,
    include_datums=False,
):
    image = mpimg.imread(str(prepared_image_path))
    page_width = int(result.get("page_image_width_px") or image.shape[1])
    page_height = int(result.get("page_image_height_px") or image.shape[0])
    render_dpi = int(output_dpi or result.get("render_dpi") or 400)

    included_categories = {"dimension"}
    if include_feature_control_frames:
        included_categories.add("feature_control_frame")
    if include_datums:
        included_categories.add("datum")

    fig = plt.figure(figsize=(page_width / float(render_dpi), page_height / float(render_dpi)), dpi=render_dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(image, origin="upper", extent=[0, page_width, page_height, 0])

    legend_handles = {}
    label_fontsize = max(6, min(10, int(round(max(page_width, page_height) / 900.0))))

    for row in _iter_dimension_rows(result):
        category = row["category"]
        if category not in included_categories:
            continue
        bbox = row["bbox"]
        item = row["item"]
        if not _valid_bbox(bbox):
            continue
        if item.get("annotation_ready") is False:
            continue

        x1, y1, x2, y2 = bbox
        style = CATEGORY_STYLES[category]
        rect = Rectangle((x1, y1), max(1, x2 - x1), max(1, y2 - y1), fill=False, edgecolor=style["color"], linewidth=style["linewidth"])
        ax.add_patch(rect)

        if category not in legend_handles:
            legend_handles[category] = Patch(facecolor="none", edgecolor=style["color"], linewidth=style["linewidth"], label=category)

        if show_labels:
            text_y = y1 - 8 if y1 > 20 else y2 + 14
            ax.text(
                x1,
                text_y,
                str(row["label"])[:34],
                fontsize=label_fontsize,
                color=style["color"],
                bbox={"facecolor": "white", "edgecolor": style["color"], "alpha": 0.85, "pad": 1.5},
            )

    if legend_handles:
        ax.legend(handles=list(legend_handles.values()), loc="upper right", fontsize=max(7, label_fontsize), framealpha=0.95)

    ax.set_xlim(0, page_width)
    ax.set_ylim(page_height, 0)
    ax.set_axis_off()

    resolved_output = Path(output_path).resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(resolved_output, dpi=render_dpi)
    plt.close(fig)
    return resolved_output
