"""
Pipeline stage ownership:

01. drawing_pipeline.py - main entry and orchestration
02. drawing_pipeline.py - OpenAI extraction stage
06. drawing_pipeline.py - output writing stage

This file intentionally contains multiple stages because it is the
top-level runner for the current pipeline.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from openai import APIConnectionError, APITimeoutError, OpenAI, Timeout
from PIL import Image, ImageDraw, ImageFont

try:
    from ..refinement.bbox_refiner import count_ready_target_annotations, enable_vlm_annotation_fallback, refine_result_bboxes
    from ..sub_agents import crop_agent as crop_agent_lib
    from ..helpers import drawing_preprocessor as drawing_preprocessor_lib
    from ..helpers.coordinate_space import apply_result_coordinate_space, build_coordinate_system_instruction, build_render_metadata
    from ..helpers.overlay_matplotlib import save_result_overlay
    from ..sub_agents.pdf_clarity_agent import prepare_pdf_clarity_image
    from ..refinement.pdf_text_bbox_refiner import refine_result_bboxes_from_pdf_text
    from ..refinement.dimension_verifier import build_dimension_verification
    from ..refinement.value_verifier import verify_dimension_values
    from ..helpers.sqlite_importer import import_extraction_json_to_sqlite
except ImportError:
    from openai_pipeline.pipeline_stack.refinement.bbox_refiner import count_ready_target_annotations, enable_vlm_annotation_fallback, refine_result_bboxes
    from openai_pipeline.pipeline_stack.sub_agents import crop_agent as crop_agent_lib
    from openai_pipeline.pipeline_stack.helpers import drawing_preprocessor as drawing_preprocessor_lib
    from openai_pipeline.pipeline_stack.helpers.coordinate_space import apply_result_coordinate_space, build_coordinate_system_instruction, build_render_metadata
    from openai_pipeline.pipeline_stack.helpers.overlay_matplotlib import save_result_overlay
    from openai_pipeline.pipeline_stack.sub_agents.pdf_clarity_agent import prepare_pdf_clarity_image
    from openai_pipeline.pipeline_stack.refinement.pdf_text_bbox_refiner import refine_result_bboxes_from_pdf_text
    from openai_pipeline.pipeline_stack.refinement.dimension_verifier import build_dimension_verification
    from openai_pipeline.pipeline_stack.refinement.value_verifier import verify_dimension_values
    from openai_pipeline.pipeline_stack.helpers.sqlite_importer import import_extraction_json_to_sqlite


DEFAULT_PROMPT_TEMPLATE = """
You are a conservative engineering drawing extraction agent.

Your task is to read a 2D mechanical engineering drawing image and return strict JSON only using the pipeline schema.

Core rule:
Extract only information that is directly visible in the drawing image.
Do not infer, normalize, complete, estimate, reconstruct, or guess values from geometry, symmetry, repetition, filename, engineering knowledge, standards, or context.

Global rules:
1. Return valid JSON only. No markdown. No explanation.
2. Use document_id exactly as provided: {document_id}
3. Use the uploaded source file name `{source_file_name}` and filename stem `{source_file_stem}` only as fallback hints for document number when the visible drawing number is unclear.
4. If a value is not clearly readable, set the parsed field to null.
5. If an item is partially readable but visibly present, preserve the visible text in `raw_text`, set unclear parsed fields to null, and set `status = "uncertain"`.
6. If you are not sure an item is visibly present, do not extract it.
7. Every extracted dimension, feature control frame, note, title-block field, and BOM row must preserve visible evidence in `raw_text`.
8. Delete any technical item created only from geometry, engineering assumptions, repetition, symmetry, or filename context.

Dimension and tolerance rules:
- Tolerance recall is critical, but only extract tolerances that are visibly present.
- Before finishing, do a final tolerance-only sweep over all visible dimensions, callouts, title-block tolerance areas, and note-based dimensional requirements.
- Look specifically for `+/-`, `+.../-...`, stacked plus/minus text, single-sided `+` or `-`, limit dimensions, MIN, MAX, and fit/class text visibly attached to a size.
- Extract dimensions in any orientation, including horizontal, vertical, diagonal, radial, angular, and stacked layouts.
- A real visible dimension must remain a single dimension item even if nominal and tolerance are split across lines.
- Every extracted dimension must include `raw_text`. If `raw_text` is missing, remove the dimension.
- Do not create dimensions from geometry alone.
- Do not infer repeated, mirrored, hidden, or standard dimensions.
- For `+/-X`, store `tolerance_plus = X` and `tolerance_minus = -X`.
- For `+A/-B`, store `tolerance_plus = A` and `tolerance_minus = -B`.
- For `+A` only, store `tolerance_plus = A` and `tolerance_minus = null`.
- For `-B` only, store `tolerance_plus = null` and `tolerance_minus = -B`.
- Do not invent a missing tolerance side.
- If tolerance text is visible but partly unreadable, preserve the visible `raw_text` and set unclear numeric fields to null.
- "h13", "H13", "6H", and "6h" are tolerance or thread classes, not standalone dimensions.
- If a fit or tolerance class is visibly attached to a size, keep it with that size in `raw_text`; do not split it into a separate dimension.
- Dimensions in parentheses are reference dimensions.

Strict exclusions:
- Do not treat feature control frames as dimensions.
- Do not treat a diameter symbol inside a feature control frame, positional tolerance, or profile tolerance as a diameter dimension.
- Do not treat datum letters such as A, B, or C as dimensions unless they are part of visible dimension text.
- Do not extract title block values, BOM values, revision values, scale values, sheet numbers, or note references such as `See Note 4` as dimensions.

GD&T and datum rules:
- Extract only visible feature control frames and visible datum identifiers.
- Every extracted feature control frame must include visible `raw_text`.
- Do not create GD&T from ordinary dimensions or drawing conventions.
- Do not treat ordinary letters as datums unless visibly shown as datum symbols or datum callouts.

Notes, title block, and BOM rules:
- Extract only visibly present notes, title-block fields, and BOM rows.
- Preserve exact visible text in `raw_text` whenever that field exists in the schema.
- If a field label is visible but the value is unreadable, keep the item and set the parsed value to an empty string or null only where the schema allows it, with `status = "uncertain"`.

Bounding box rules:
- Return bbox fields accurately whenever the item is visibly locatable, using [x1, y1, x2, y2].
- If the location is uncertain, return the closest defensible bbox only when the visible evidence is still clear enough to support extraction. Otherwise omit the item.
""".strip()

STRUCTURE_PASS_PROMPT_TEMPLATE = """
You are a conservative mechanical drawing parser.

Analyze the engineering drawing image and return strict JSON only for document structure.

Focus only on visibly present:
- drawing_name from the title block when visible
- drawing_number when visible in the title block, boxed side callout, or another clear document-number label
- unit only when visibly supported; if unclear, set `unit = null` and `unit_inferred = false`
- part_description only when supported by visible text; otherwise leave it empty
- title_block region and fields
- bill of materials region and rows
- notes and note blocks
- major drawing views with view_id, view_type, view_bbox, and view_description
- other annotations such as section labels, detail labels, and standalone reference callouts that are not boxed datums

Rules:
- Do not infer part function, material, units, or missing title-block fields.
- Do not extract dimensions, tolerances, feature control frames, or datums in this pass.
- Do not count or estimate dimensions in this pass.
- View bounding boxes must cover only the visible view geometry, not nearby notes or dimensions.
- Every extracted note, title-block field, and BOM row must preserve visible evidence in `raw_text`.
- If something is not clearly visible, return null, empty arrays, or `status = "uncertain"` rather than guessing.
- Use document_id exactly as provided: {document_id}
- The uploaded source file name is `{source_file_name}` and the filename stem is `{source_file_stem}`.
- Use the filename stem only as a fallback hint for `drawing_number` when the visible drawing number cannot be read confidently from the image.
""".strip()


DIMENSION_PASS_PROMPT_TEMPLATE = """
You are a conservative mechanical drawing parser.

Analyze the engineering drawing image and return strict JSON only for dimensions.

Focus only on visible dimensions and visible textual dimensional requirements.
Extract dimensions in any orientation including horizontal, vertical, diagonal, inclined, radial, angular, and stacked layouts.
Assign each dimension to one of the provided views when possible. If not, place it in unassigned.
Use the provided view list as the only allowed view_id set.
Do not extract any dimensions from notes, note blocks, note leaders, specification paragraphs, or general note callouts.
If a value appears inside a note region, omit it from this pass even if it looks measurable.

Evidence rules:
- Every extracted dimension must have direct visible evidence and must include `raw_text`.
- If `raw_text` is missing, remove the dimension.
- Do not create dimensions from geometry alone.
- Do not infer repeated, symmetric, hidden, mirrored, or standard dimensions.
- If a real visible dimension is present but partly unclear, keep it with `status = "uncertain"`, preserve the visible `raw_text`, and set unclear parsed fields to null.

Strict exclusions:
- Do not extract feature control frames as dimensions.
- Do not treat a diameter symbol inside a feature control frame, positional tolerance, or profile tolerance as a diameter dimension.
- Do not extract datum letters such as A, B, or C as dimensions.
- Do not extract note-reference numbers such as "See Note 4" as dimensions.
- Do not extract any dimension, tolerance, MIN/MAX requirement, or textual size requirement from note regions.
- Do not extract title block values, BOM values, sheet numbers, revision values, or scale values as dimensions.
- If an item is not clearly a measurable geometric size or textual dimensional requirement, omit it.

Tolerance rules:
- Every explicit tolerance-bearing dimension is high priority and must be extracted.
- Before finishing, do a second pass that looks only for tolerances and fit/class markings attached to dimensions.
- Look specifically for `+/-`, `+.../-...`, stacked plus/minus layouts, single-sided `+` or `-`, limit dimensions, diameter/radius tolerances, MIN, MAX, and fit/class text such as `H7`, `h9`, `6H`, or `6h` attached to a size.
- Do not drop a dimension just because its tolerance text is smaller, offset, on the next line, or printed above/below the nominal value.
- Extract nominal_value, tolerance_class, tolerance_format, tolerance_pattern, tolerance_plus, and tolerance_minus only when visibly supported.
- For `+/-X`, set `tolerance_plus = X` and `tolerance_minus = -X`.
- For `+A/-B`, set `tolerance_plus = A` and `tolerance_minus = -B`.
- For `+A` only, set `tolerance_plus = A` and `tolerance_minus = null`.
- For `-B` only, set `tolerance_plus = null` and `tolerance_minus = -B`.
- If the tolerance is clearly visible in `raw_text` but one parsed numeric field is uncertain, preserve the full `raw_text`, keep the dimension, and set only the uncertain numeric field to null.
- Do not invent missing tolerance sides.
- Preserve the printed value in `raw_text`.

View context:
{view_context}

Note context:
{note_context}
""".strip()


DIMENSION_RECOVERY_PASS_PROMPT_TEMPLATE = """
You are a conservative mechanical drawing parser doing a dimension recovery pass.

Your job is to find only visible dimensions that are missing from the current extracted inventory.

Important rules:
- Return only additional missing dimensions.
- Do not repeat any dimension that already appears in the existing inventory below.
- Do not correct, replace, rename, or improve an already-listed dimension here.
- If you do not find any additional visible dimensions, return empty arrays.
- Focus on small, stacked, offset, unilateral-tolerance, repeated-hole, and dense-cluster dimensions that are easy to miss in the first pass.
- Keep the same conservative rules as the main dimension pass: no guessing, no inferred symmetry, no geometry-only dimensions, no FCFs as dimensions, and nothing from notes/title block/BOM.

View context:
{view_context}

Note context:
{note_context}

Existing extracted dimension inventory:
{existing_dimension_context}
""".strip()


GDT_PASS_PROMPT_TEMPLATE = """
You are a conservative mechanical drawing parser.

Analyze the engineering drawing image and return strict JSON only for GD&T items.

Focus only on visible:
- feature control frames
- datum identifiers

Assign each item to one of the provided views when possible. If not, place it in unassigned.
Use the provided view list as the only allowed view_id set.

Strict rules:
- Every extracted feature control frame must include visible `raw_text`.
- Every extracted datum must have direct visible datum-symbol evidence.
- A segmented GD&T box is a feature control frame, not a dimension.
- A diameter symbol used inside a feature control frame tolerance zone belongs to GD&T and must never be emitted as a diameter dimension.
- A datum letter such as A, B, or C in a boxed datum frame or datum callout is a datum, not a dimension.
- Do not treat ordinary letters or section labels as datums unless they are visibly boxed as datum callouts.
- If a feature control frame already contains a datum reference, do not create a second overlapping datum box for the same printed region.
- Do not extract anything from note regions or note text in this pass, including feature control frames or datums printed inside notes.
- Do not extract dimensions, notes, title block values, or BOM text in this pass.
- Prefer omitting an uncertain item over inventing one.

View context:
{view_context}
""".strip()


ANNOTATION_STYLES = {
    "title_block": {"outline": (0, 102, 255), "fill": (0, 102, 255, 36), "label": "TITLE"},
    "bill_of_materials": {"outline": (0, 102, 255), "fill": (0, 102, 255, 36), "label": "BOM"},
    "note": {"outline": (0, 102, 255), "fill": (0, 102, 255, 28), "label": "NOTE"},
    "dimension": {"outline": (34, 197, 94), "fill": (34, 197, 94, 44), "label": "DIM"},
    "datum": {"outline": (255, 105, 180), "fill": (255, 105, 180, 28), "label": "DATUM"},
    "feature_control_frame": {"outline": (220, 38, 38), "fill": (220, 38, 38, 32), "label": "FCF"},
    "other_annotation": {"outline": (0, 102, 255), "fill": (0, 102, 255, 28), "label": "OTHER"},
}

DEFAULT_HYBRID_PRIMARY_MODEL = "gpt-5.4-mini"
DEFAULT_HYBRID_FALLBACK_MODEL = "gpt-5.5"


def parse_args():
    parser = argparse.ArgumentParser(description="Drawing extraction pipeline runner")
    parser.add_argument("--image", default="Test5.jpg", type=str, help="path to the drawing image or PDF")
    parser.add_argument("--document_id", default=None, type=str, help="stable document id for outputs")
    parser.add_argument("--drawing_role", default="part", type=str, help="drawing role such as part, assembly, or detail")
    parser.add_argument("--drawing_name", default=None, type=str, help="human-readable drawing name stored with the output")
    parser.add_argument("--model", default="gpt-5.5", type=str, help="OpenAI extraction model name")
    parser.add_argument(
        "--model_strategy",
        choices=("ask", "direct", "hybrid"),
        default="hybrid",
        help="model selection strategy. 'hybrid' tries a cheaper model first and falls back to the main model only when needed.",
    )
    parser.add_argument(
        "--primary_model",
        default=DEFAULT_HYBRID_PRIMARY_MODEL,
        type=str,
        help="primary model used first when --model_strategy hybrid is enabled",
    )
    parser.add_argument("--result_folder", default="Results/drawing_pipeline", type=str, help="base folder for pipeline outputs")
    parser.add_argument(
        "--db",
        default=None,
        type=str,
        help="SQLite database path for automatic import; defaults to drawing_data.db inside each output folder",
    )
    parser.add_argument("--openai_api_key", default=None, type=str, help="OpenAI API key; defaults to OPENAI_API_KEY env var")
    parser.add_argument("--pdf_page", default=1, type=int, help="1-based PDF page number to render when input is a PDF")
    parser.add_argument("--pdf_dpi", default=450, type=int, help="render DPI used when input is a PDF")
    parser.add_argument(
        "--extraction_mode",
        choices=("ask", "single", "multi"),
        default="ask",
        help="OpenAI extraction mode. Use 'ask' to choose in the terminal.",
    )
    parser.add_argument(
        "--use_multi_pass_extraction",
        default=False,
        action="store_true",
        help="shortcut for --extraction_mode multi; skips the terminal prompt",
    )
    parser.add_argument(
        "--value_verification_mode",
        choices=("ask", "skip", "local"),
        default="ask",
        help="value verification mode. Use 'local' for CRAFT/GLM-OCR or 'skip' to avoid local OCR.",
    )
    parser.add_argument(
        "--crop_preprocessing_mode",
        choices=("ask", "skip", "dynamic", "red_boxes"),
        default="ask",
        help="crop preprocessing mode. Use 'dynamic' for estimated sheet regions or 'red_boxes' for user-marked red rectangles.",
    )
    parser.add_argument(
        "--enable_bbox_refinement",
        default=False,
        action="store_true",
        help="enable the local CRAFT + OCR bbox refinement stage; off by default for VLM-only mode",
    )
    parser.add_argument(
        "--disable_bbox_refinement",
        default=False,
        action="store_true",
        help="skip local CRAFT + OCR bbox refinement before saving outputs",
    )
    parser.add_argument(
        "--bbox_refiner_glm_model",
        default="zai-org/GLM-OCR",
        type=str,
        help="local OCR model used for bbox refinement",
    )
    parser.add_argument(
        "--bbox_refiner_max_new_tokens",
        default=512,
        type=int,
        help="maximum OCR decode tokens used during bbox refinement",
    )
    parser.add_argument(
        "--prompt_file",
        default=None,
        type=str,
        help="optional text file containing a custom prompt template; may include {document_id}",
    )
    parser.add_argument(
        "--fallback_min_extracted_dimensions",
        default=1,
        type=int,
        help="hybrid fallback threshold: rerun with the stronger model when extracted dimensions fall below this floor",
    )
    parser.add_argument(
        "--fallback_min_green_box_dimensions",
        default=1,
        type=int,
        help="hybrid fallback threshold: rerun with the stronger model when verified green-box dimensions fall below this floor",
    )
    parser.add_argument(
        "--fallback_min_green_box_ratio",
        default=0.55,
        type=float,
        help="hybrid fallback threshold: rerun when green-box dimensions / extracted dimensions falls below this ratio",
    )
    parser.add_argument(
        "--fallback_max_bbox_mismatches",
        default=3,
        type=int,
        help="hybrid fallback threshold: rerun when bbox mismatches exceed this count",
    )
    return parser.parse_args()


def choose_multi_pass_extraction(args):
    if args.use_multi_pass_extraction:
        return True
    if args.extraction_mode == "single":
        return False
    if args.extraction_mode == "multi":
        return True
    if not sys.stdin.isatty():
        return False

    print("")
    print("OpenAI extraction mode:")
    print("  1. Single-pass (faster, default)")
    print("  2. Multi-pass (slower, separates structure, dimensions, and GD&T)")
    while True:
        try:
            choice = input("Choose extraction mode [1/single or 2/multi, default single]: ").strip().lower()
        except EOFError:
            return False
        if choice in ("", "1", "s", "single", "single-pass", "single pass"):
            return False
        if choice in ("2", "m", "multi", "multi-pass", "multi pass"):
            return True
        print("Please enter 1/single or 2/multi.")


def choose_value_verification(args):
    if args.value_verification_mode == "skip":
        return False
    if args.value_verification_mode == "local":
        return True
    if not sys.stdin.isatty():
        return True

    print("")
    print("Value verification mode:")
    print("  1. Skip local CRAFT/GLM-OCR verification (faster)")
    print("  2. Run local CRAFT/GLM-OCR verification (slower, checks values)")
    while True:
        try:
            choice = input("Run local value verification? [1/skip or 2/local, default skip]: ").strip().lower()
        except EOFError:
            return False
        if choice in ("", "1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "l", "local", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/local.")


def choose_crop_preprocessing(args):
    if args.crop_preprocessing_mode == "skip":
        return "skip"
    if args.crop_preprocessing_mode == "dynamic":
        return "dynamic"
    if args.crop_preprocessing_mode == "red_boxes":
        return "red_boxes"
    if not sys.stdin.isatty():
        return "skip"

    print("")
    print("Crop preprocessing agent:")
    print("  1. Skip crop preprocessing")
    print("  2. Run dynamic crop preprocessing for notes, title/BOM, and drawing views")
    print("  3. Use detected red markup boxes for notes, title/BOM, and drawing views")
    while True:
        try:
            choice = input("Run crop preprocessing? [1/skip, 2/dynamic, or 3/red_boxes, default skip]: ").strip().lower()
        except EOFError:
            return "skip"
        if choice in ("", "1", "s", "skip", "no", "n"):
            return "skip"
        if choice in ("2", "d", "dynamic", "yes", "y"):
            return "dynamic"
        if choice in ("3", "r", "red", "red_boxes", "red-boxes", "boxes"):
            return "red_boxes"
        print("Please enter 1/skip, 2/dynamic, or 3/red_boxes.")


def choose_model_strategy(args):
    if args.model_strategy == "direct":
        return "direct"
    if args.model_strategy == "hybrid":
        return "hybrid"
    if not sys.stdin.isatty():
        return "hybrid"

    print("")
    print("Model strategy:")
    print("  1. Hybrid (default) - try {} first, then fall back to {} only if needed".format(args.primary_model, args.model))
    print("  2. Direct - use {} immediately".format(args.model))
    while True:
        try:
            choice = input("Choose model strategy [1/hybrid or 2/direct, default hybrid]: ").strip().lower()
        except EOFError:
            return "hybrid"
        if choice in ("", "1", "h", "hybrid", "auto"):
            return "hybrid"
        if choice in ("2", "d", "direct"):
            return "direct"
        print("Please enter 1/hybrid or 2/direct.")


def get_client(api_key=None):
    resolved_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_key:
        raise RuntimeError("OpenAI API key not found. Set OPENAI_API_KEY or pass --openai_api_key.")
    return OpenAI(
        api_key=resolved_key,
        max_retries=5,
        timeout=Timeout(timeout=900, connect=20.0),
    )


def build_model_attempt_summary(model_name, result, fallback_assessment=None, accepted=True):
    return {
        "model": model_name,
        "accepted": bool(accepted),
        "document_id": result.get("document_id"),
        "extracted_dimension_count": int(result.get("extracted_dimension_count", 0) or 0),
        "green_box_dimension_count": int(result.get("dimension_count", 0) or 0),
        "bbox_mismatch_count": int(result.get("bbox_mismatch_count", 0) or 0),
        "dimension_value_review_required": bool(result.get("dimension_value_review_required", False)),
        "fallback_assessment": fallback_assessment or {"should_fallback": False, "reasons": []},
    }


def assess_fallback_need(
    result,
    min_extracted_dimensions=1,
    min_green_box_dimensions=1,
    min_green_box_ratio=0.55,
    max_bbox_mismatches=3,
):
    extracted = int(result.get("extracted_dimension_count", 0) or 0)
    green_box = int(result.get("dimension_count", 0) or 0)
    bbox_mismatches = int(result.get("bbox_mismatch_count", 0) or 0)
    view_count = len(result.get("views", []))
    manual_review = bool(result.get("dimension_value_review_required", False))
    green_ratio = (float(green_box) / float(extracted)) if extracted > 0 else 0.0
    expected_min_extracted = max(int(min_extracted_dimensions), view_count if view_count >= 2 else 0)
    reasons = []

    if view_count == 0:
        reasons.append("no views were extracted")
    if extracted == 0:
        reasons.append("no dimensions were extracted")
    elif extracted < expected_min_extracted:
        reasons.append(
            "only {} dimensions were extracted, below the expected floor of {}".format(
                extracted,
                expected_min_extracted,
            )
        )
    if extracted >= 2 and green_box < int(min_green_box_dimensions):
        reasons.append(
            "only {} green-box dimensions were verified, below the floor of {}".format(
                green_box,
                int(min_green_box_dimensions),
            )
        )
    if extracted >= max(4, expected_min_extracted) and green_ratio < float(min_green_box_ratio):
        reasons.append(
            "green-box ratio {:.2f} is below the fallback threshold of {:.2f}".format(
                green_ratio,
                float(min_green_box_ratio),
            )
        )
    if bbox_mismatches > int(max_bbox_mismatches):
        reasons.append(
            "{} bbox mismatches exceeded the fallback threshold of {}".format(
                bbox_mismatches,
                int(max_bbox_mismatches),
            )
        )
    if manual_review:
        reasons.append("value verification flagged manual review")

    return {
        "should_fallback": bool(reasons),
        "reasons": reasons,
        "metrics": {
            "view_count": view_count,
            "extracted_dimension_count": extracted,
            "green_box_dimension_count": green_box,
            "green_box_ratio": round(green_ratio, 4),
            "bbox_mismatch_count": bbox_mismatches,
            "manual_review_required": manual_review,
            "expected_min_extracted_dimensions": expected_min_extracted,
        },
    }


def preprocess_drawing_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=1)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.25)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(1.35)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=1.2, percent=165, threshold=2))

    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1600:
        scale = min(2.0, 1600.0 / float(shortest_side))
        resized = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
        return resized.convert("RGB")
    return grayscale.convert("RGB")


def enhance_crop_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=0)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.55)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(1.75)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=1.0, percent=190, threshold=1))
    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1300:
        scale = min(3.0, 1300.0 / float(max(1, shortest_side)))
        grayscale = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
    return grayscale.convert("RGB")


def enhance_dimension_view_image(image):
    grayscale = ImageOps.grayscale(image)
    grayscale = ImageOps.autocontrast(grayscale, cutoff=0)
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.85)
    grayscale = ImageEnhance.Sharpness(grayscale).enhance(2.1)
    grayscale = grayscale.filter(ImageFilter.UnsharpMask(radius=0.8, percent=230, threshold=1))
    width, height = grayscale.size
    shortest_side = min(width, height)
    if shortest_side < 1800:
        scale = min(3.2, 1800.0 / float(max(1, shortest_side)))
        grayscale = grayscale.resize(
            (int(round(width * scale)), int(round(height * scale))),
            resample=Image.Resampling.LANCZOS,
        )
    return grayscale.convert("RGB")


def load_input_drawing(image_path, pdf_page=1, pdf_dpi=450, preprocess=True):
    path = Path(image_path)
    if path.suffix.lower() != ".pdf":
        image = Image.open(path).convert("RGB")
        return preprocess_drawing_image(image) if preprocess else image

    if pdf_page < 1:
        raise ValueError("pdf_page must be 1 or greater.")
    if pdf_dpi < 72:
        raise ValueError("pdf_dpi must be at least 72.")

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError(
            "PDF input requires PyMuPDF. Install it with `pip install PyMuPDF` or update the environment from requirements.txt."
        ) from exc

    with fitz.open(str(path)) as document:
        if document.page_count < 1:
            raise RuntimeError("PDF input does not contain any pages: {}".format(path))
        page_index = pdf_page - 1
        if page_index >= document.page_count:
            raise RuntimeError(
                "Requested PDF page {} but {} only has {} page(s).".format(pdf_page, path.name, document.page_count)
            )
        page = document.load_page(page_index)
        scale = float(pdf_dpi) / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return preprocess_drawing_image(image) if preprocess else image


def detect_pdf_sheet_info(image_path, pdf_page=1):
    path = Path(image_path)
    if path.suffix.lower() != ".pdf":
        return {"source_type": "image", "sheet_size": "unknown", "orientation": "unknown"}

    try:
        import fitz
    except ImportError:
        return {"source_type": "pdf", "sheet_size": "unknown", "orientation": "unknown"}

    iso_sizes = {
        "A0": (841.0, 1189.0),
        "A1": (594.0, 841.0),
        "A2": (420.0, 594.0),
        "A3": (297.0, 420.0),
        "A4": (210.0, 297.0),
    }
    with fitz.open(str(path)) as document:
        page_index = max(0, min(pdf_page - 1, document.page_count - 1))
        rect = document.load_page(page_index).rect
        width_mm = float(rect.width) * 25.4 / 72.0
        height_mm = float(rect.height) * 25.4 / 72.0

    orientation = "landscape" if width_mm >= height_mm else "portrait"
    short_side = min(width_mm, height_mm)
    long_side = max(width_mm, height_mm)
    best_name = "unknown"
    best_error = None
    for name, (iso_short, iso_long) in iso_sizes.items():
        error = abs(short_side - iso_short) + abs(long_side - iso_long)
        if best_error is None or error < best_error:
            best_name = name
            best_error = error
    if best_error is not None and best_error > 35.0:
        best_name = "custom"

    return {
        "source_type": "pdf",
        "sheet_size": best_name,
        "orientation": orientation,
        "width_mm": round(width_mm, 2),
        "height_mm": round(height_mm, 2),
        "pdf_page": pdf_page,
    }


def image_to_data_url(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return "data:image/png;base64,{}".format(encoded)


def normalize_crop_preprocessing_mode(mode):
    if mode is True:
        return "dynamic"
    if mode in (False, None):
        return "skip"
    normalized = str(mode).strip().lower()
    if normalized in ("", "false", "none", "off", "0", "skip"):
        return "skip"
    if normalized in ("true", "1", "dynamic"):
        return "dynamic"
    if normalized in ("red", "red_boxes", "red-boxes", "boxes"):
        return "red_boxes"
    return "skip"


def build_input_image_data_url(image_path, pdf_page=1, pdf_dpi=450, preprocess=True):
    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=preprocess)
    try:
        return image_to_data_url(image)
    finally:
        image.close()


def find_foreground_bbox(image, margin=24):
    grayscale = ImageOps.grayscale(image)
    width, height = grayscale.size
    pixels = grayscale.load()
    xs = []
    ys = []
    step = max(1, min(width, height) // 900)
    for y in range(0, height, step):
        for x in range(0, width, step):
            if pixels[x, y] < 245:
                xs.append(x)
                ys.append(y)
    if not xs or not ys:
        return [0, 0, width, height]
    return [
        max(0, min(xs) - margin),
        max(0, min(ys) - margin),
        min(width, max(xs) + margin),
        min(height, max(ys) + margin),
    ]


def clamp_crop_box(box, width, height):
    x1, y1, x2, y2 = [int(round(value)) for value in box]
    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(x1 + 1, min(width, x2))
    y2 = max(y1 + 1, min(height, y2))
    return [x1, y1, x2, y2]


def expand_crop_box(box, width, height, margin_x, margin_y):
    x1, y1, x2, y2 = box
    return clamp_crop_box([x1 - margin_x, y1 - margin_y, x2 + margin_x, y2 + margin_y], width, height)


def crop_box_area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def crop_box_iou(a, b):
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    intersection = crop_box_area([x1, y1, x2, y2])
    union = crop_box_area(a) + crop_box_area(b) - intersection
    return intersection / float(union) if union > 0 else 0.0


def scale_crop_box(box, source_size, target_size):
    source_width, source_height = source_size
    target_width, target_height = target_size
    if source_width <= 0 or source_height <= 0:
        return [0, 0, target_width, target_height]
    scale_x = float(target_width) / float(source_width)
    scale_y = float(target_height) / float(source_height)
    return clamp_crop_box(
        [
            box[0] * scale_x,
            box[1] * scale_y,
            box[2] * scale_x,
            box[3] * scale_y,
        ],
        target_width,
        target_height,
    )


def mask_red_annotations(image):
    try:
        import numpy as np
    except ImportError:
        return image.copy()

    rgb = np.array(image.convert("RGB"))
    red = rgb[:, :, 0].astype("int16")
    green = rgb[:, :, 1].astype("int16")
    blue = rgb[:, :, 2].astype("int16")
    red_mask = (
        (red >= 150)
        & (green <= 170)
        & (blue <= 170)
        & ((red - green) >= 35)
        & ((red - blue) >= 35)
    )
    if not red_mask.any():
        return image.copy()
    cleaned = rgb.copy()
    cleaned[red_mask] = [255, 255, 255]
    return Image.fromarray(cleaned, mode="RGB")


def detect_red_markup_boxes(image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    rgb = np.array(image.convert("RGB"))
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)

    lower_red_1 = np.array([0, 70, 70], dtype=np.uint8)
    upper_red_1 = np.array([12, 255, 255], dtype=np.uint8)
    lower_red_2 = np.array([168, 70, 70], dtype=np.uint8)
    upper_red_2 = np.array([180, 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower_red_1, upper_red_1) | cv2.inRange(hsv, lower_red_2, upper_red_2)

    kernel_size = max(3, int(round(min(width, height) / 450.0)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    cleaned = cv2.dilate(cleaned, kernel, iterations=1)

    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    image_area = float(width * height)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        bbox_area = float(w * h)
        if bbox_area < image_area * 0.0035:
            continue
        if bbox_area > image_area * 0.85:
            continue
        if w < width * 0.08 or h < height * 0.06:
            continue
        contour_area = max(1.0, float(cv2.contourArea(contour)))
        fill_ratio = contour_area / max(1.0, bbox_area)
        if fill_ratio > 0.30:
            continue
        boxes.append(expand_crop_box([x, y, x + w, y + h], width, height, max(6, width // 300), max(6, height // 300)))

    return merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.18)


def build_red_box_crop_regions(source_image, target_size=None):
    source_width, source_height = source_image.size
    target_size = target_size or source_image.size
    boxes = detect_red_markup_boxes(source_image)
    if len(boxes) < 3:
        return []

    drawing_box = max(boxes, key=crop_box_area)
    remaining = [box for box in boxes if box != drawing_box]

    left_candidates = [
        box for box in remaining
        if ((box[0] + box[2]) / 2.0) < (source_width / 2.0) and ((box[1] + box[3]) / 2.0) > (source_height * 0.45)
    ]
    right_candidates = [
        box for box in remaining
        if ((box[0] + box[2]) / 2.0) >= (source_width / 2.0) and ((box[1] + box[3]) / 2.0) > (source_height * 0.45)
    ]

    notes_box = max(left_candidates, key=crop_box_area) if left_candidates else None
    bom_box = max(right_candidates, key=crop_box_area) if right_candidates else None

    if notes_box is None or bom_box is None:
        lower_boxes = sorted(
            remaining,
            key=lambda box: (((box[1] + box[3]) / 2.0), crop_box_area(box)),
            reverse=True,
        )[:2]
        if len(lower_boxes) < 2:
            return []
        lower_boxes = sorted(lower_boxes, key=lambda box: ((box[0] + box[2]) / 2.0))
        notes_box, bom_box = lower_boxes[0], lower_boxes[1]

    scaled_drawing_box = scale_crop_box(drawing_box, source_image.size, target_size)
    scaled_notes_box = scale_crop_box(notes_box, source_image.size, target_size)
    scaled_bom_box = scale_crop_box(bom_box, source_image.size, target_size)
    target_width, target_height = target_size

    return [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet for global context and coordinate consistency.",
            "box": [0, 0, target_width, target_height],
            "enhance": False,
            "mask_red": True,
        },
        {
            "name": "drawing_views_dimensions",
            "description": "User-marked drawing region with main views, dimensions, GD&T callouts, and detail views.",
            "box": scaled_drawing_box,
            "enhance": True,
            "enhance_mode": "dimension_view",
            "mask_red": True,
        },
        {
            "name": "notes_text_zone",
            "description": "User-marked notes/specifications region.",
            "box": scaled_notes_box,
            "enhance": True,
            "enhance_mode": "text_block",
            "mask_red": True,
        },
        {
            "name": "bom_title_block_zone",
            "description": "User-marked title block and BOM region.",
            "box": scaled_bom_box,
            "enhance": True,
            "enhance_mode": "text_block",
            "mask_red": True,
        },
    ]


def merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.08):
    merged = []
    for box in boxes:
        current = box
        changed = True
        while changed:
            changed = False
            kept = []
            for existing in merged:
                close_x = current[0] <= existing[2] and existing[0] <= current[2]
                close_y = current[1] <= existing[3] and existing[1] <= current[3]
                if crop_box_iou(current, existing) >= iou_threshold or (close_x and close_y):
                    current = clamp_crop_box(
                        [
                            min(current[0], existing[0]),
                            min(current[1], existing[1]),
                            max(current[2], existing[2]),
                            max(current[3], existing[3]),
                        ],
                        width,
                        height,
                    )
                    changed = True
                else:
                    kept.append(existing)
            merged = kept
        merged.append(current)
    return merged


def detect_dense_layout_boxes(image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    grayscale = np.array(ImageOps.grayscale(image))
    height, width = grayscale.shape[:2]
    foreground = cv2.threshold(grayscale, 245, 255, cv2.THRESH_BINARY_INV)[1]
    border_x = max(8, int(width * 0.025))
    border_y = max(8, int(height * 0.025))
    foreground[:border_y, :] = 0
    foreground[-border_y:, :] = 0
    foreground[:, :border_x] = 0
    foreground[:, -border_x:] = 0

    boxes = []
    kernels = [
        (max(24, width // 120), max(8, height // 260), 3),
        (max(40, width // 75), max(12, height // 180), 3),
        (max(70, width // 45), max(18, height // 130), 2),
    ]
    for kernel_width, kernel_height, iterations in kernels:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, kernel_height))
        grouped = cv2.dilate(foreground, kernel, iterations=iterations)
        contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            if area < (width * height) * 0.0005:
                continue
            if w < width * 0.035 or h < height * 0.012:
                continue
            if w > width * 0.985 and h > height * 0.92:
                continue
            near_side_border = x < width * 0.04 or (x + w) > width * 0.96
            near_bottom_border = (y + h) > height * 0.96
            if near_side_border and w < width * 0.10:
                continue
            if near_bottom_border and h < height * 0.08 and w < width * 0.18:
                continue
            boxes.append(expand_crop_box([x, y, x + w, y + h], width, height, width // 85, height // 85))

    return merge_overlapping_crop_boxes(boxes, width, height, iou_threshold=0.18)


def describe_crop_position(box, width, height):
    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    horizontal = "left" if center_x < width / 3.0 else "right" if center_x > width * 2.0 / 3.0 else "center"
    vertical = "top" if center_y < height / 3.0 else "bottom" if center_y > height * 2.0 / 3.0 else "middle"
    return "{}_{}".format(vertical, horizontal)


def build_dynamic_crop_regions(image):
    width, height = image.size
    content_x1, content_y1, content_x2, content_y2 = find_foreground_bbox(image)
    dense_boxes = detect_dense_layout_boxes(image)
    bottom_boxes = [
        box for box in dense_boxes
        if (
            box[1] >= int(height * 0.42)
            or ((box[1] + box[3]) / 2.0) >= height * 0.62
            or box[3] >= int(height * 0.78)
        )
        and crop_box_area(box) <= width * height * 0.35
    ]
    bottom_boxes = sorted(
        bottom_boxes,
        key=lambda box: ((box[1] + box[3]) / 2.0, crop_box_area(box)),
        reverse=True,
    )

    selected_admin_boxes = []
    for box in bottom_boxes:
        if all(crop_box_iou(box, kept) < 0.25 for kept in selected_admin_boxes):
            selected_admin_boxes.append(box)
        if len(selected_admin_boxes) >= 4:
            break
    selected_area = sum(crop_box_area(box) for box in selected_admin_boxes)
    if selected_area < width * height * 0.025:
        selected_admin_boxes = []

    admin_top = min([box[1] for box in selected_admin_boxes], default=int(height * 0.72))
    drawing_box = clamp_crop_box(
        [
            max(0, content_x1 - int(width * 0.025)),
            max(0, content_y1 - int(height * 0.025)),
            min(width, content_x2 + int(width * 0.025)),
            min(height, max(int(height * 0.55), admin_top + int(height * 0.04))),
        ],
        width,
        height,
    )

    regions = [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet for global context and coordinate consistency.",
            "box": [0, 0, width, height],
            "enhance": False,
        },
        {
            "name": "drawing_views_dimensions",
            "description": "Main drawing views, dimensions, GD&T callouts, and detail views. Region is content-aware.",
            "box": drawing_box,
            "enhance": True,
            "enhance_mode": "dimension_view",
        },
    ]
    for index, box in enumerate(selected_admin_boxes, start=1):
        position = describe_crop_position(box, width, height)
        regions.append(
            {
                "name": "candidate_text_table_{}_{}".format(index, position),
                "description": "Detected text/table candidate block. Classify from content: may be notes, title block, BOM, revision fields, material, or default tolerances.",
                "box": box,
                "enhance": True,
                "enhance_mode": "text_block",
            }
        )
    if not selected_admin_boxes:
        regions.extend(
            [
                {
                    "name": "bottom_left_text_zone",
                    "description": "Fallback lower-left/bottom text zone for notes, specifications, and default tolerances.",
                    "box": clamp_crop_box([0, int(height * 0.58), int(width * 0.46), height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
                {
                    "name": "bottom_center_text_zone",
                    "description": "Fallback bottom-center text/table zone for general notes, BOM continuations, and drawing references.",
                    "box": clamp_crop_box([int(width * 0.27), int(height * 0.58), int(width * 0.73), height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
                {
                    "name": "bottom_right_text_zone",
                    "description": "Fallback lower-right/bottom text zone for title block, BOM, revision/status fields, and material.",
                    "box": clamp_crop_box([int(width * 0.54), int(height * 0.58), width, height], width, height),
                    "enhance": True,
                    "enhance_mode": "text_block",
                },
            ]
        )
    return regions


def resolve_crop_regions(preprocessed_image, crop_mode, source_image=None):
    normalized_mode = normalize_crop_preprocessing_mode(crop_mode)
    if normalized_mode == "red_boxes" and source_image is not None:
        red_box_regions = build_red_box_crop_regions(source_image, target_size=preprocessed_image.size)
        if red_box_regions:
            return red_box_regions
    if normalized_mode == "dynamic" or normalized_mode == "red_boxes":
        return build_dynamic_crop_regions(preprocessed_image)
    width, height = preprocessed_image.size
    return [
        {
            "name": "full_sheet",
            "description": "Complete drawing sheet.",
            "box": [0, 0, width, height],
            "enhance": False,
            "mask_red": False,
        }
    ]


def prepare_region_crop(crop, region):
    working = mask_red_annotations(crop) if region.get("mask_red") else crop.copy()
    if not region.get("enhance"):
        return working

    if region.get("enhance_mode") == "dimension_view":
        prepared = enhance_dimension_view_image(working)
    else:
        prepared = enhance_crop_image(working)
    working.close()
    return prepared


def build_input_image_parts(image_path, pdf_page=1, pdf_dpi=450, preprocess=True, crop_preprocess=False):
    crop_mode = normalize_crop_preprocessing_mode(crop_preprocess)
    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=preprocess)
    source_image = None
    try:
        if crop_mode == "skip":
            return [{"name": "full_sheet", "description": "Complete drawing sheet.", "image_url": image_to_data_url(image)}]

        if crop_mode == "red_boxes":
            source_image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=False)

        parts = []
        for region in resolve_crop_regions(image, crop_mode, source_image=source_image):
            crop = image.crop(region["box"])
            prepared = None
            try:
                prepared = prepare_region_crop(crop, region)
                parts.append(
                    {
                        "name": region["name"],
                        "description": region["description"],
                        "bbox": region["box"],
                        "image_url": image_to_data_url(prepared),
                    }
                )
            finally:
                crop.close()
                if prepared is not None:
                    prepared.close()
        return parts
    finally:
        if source_image is not None:
            source_image.close()
        image.close()


def save_crop_preprocessing_outputs(image_path, output_dir, output_stem, pdf_page=1, pdf_dpi=450, sheet_info=None, crop_mode="dynamic"):
    crop_dir = Path(output_dir) / "{}_crop_preprocessing".format(output_stem)
    crop_dir.mkdir(parents=True, exist_ok=True)

    image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=True)
    source_image = None
    saved = []
    try:
        normalized_mode = normalize_crop_preprocessing_mode(crop_mode)
        if normalized_mode == "red_boxes":
            source_image = load_input_drawing(image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=False)
        regions = resolve_crop_regions(image, normalized_mode, source_image=source_image)

        full_path = crop_dir / "{}_preprocessed_full_sheet.png".format(output_stem)
        image.save(full_path)
        saved.append(
            {
                "name": "preprocessed_full_sheet",
                "description": "Full preprocessed drawing sheet.",
                "bbox": [0, 0, image.size[0], image.size[1]],
                "path": str(full_path.resolve()),
            }
        )

        overview = image.convert("RGBA")
        draw = ImageDraw.Draw(overview)
        font = get_annotation_font(max(18, int(round(max(image.size) / 120.0))))
        colors = {
            "drawing_views_dimensions": (34, 197, 94, 255),
            "bottom_left_text_zone": (0, 102, 255, 255),
            "bottom_center_text_zone": (168, 85, 247, 255),
            "bottom_right_text_zone": (220, 38, 38, 255),
            "notes_text_zone": (0, 102, 255, 255),
            "bom_title_block_zone": (220, 38, 38, 255),
            "full_sheet": (120, 120, 120, 255),
        }

        for region in regions:
            crop = image.crop(region["box"])
            prepared = None
            try:
                prepared = prepare_region_crop(crop, region)
                crop_path = crop_dir / "{}_{}.png".format(output_stem, region["name"])
                prepared.save(crop_path)
                saved.append(
                    {
                        "name": region["name"],
                        "description": region["description"],
                        "bbox": region["box"],
                        "enhance_mode": region.get("enhance_mode", "none"),
                        "path": str(crop_path.resolve()),
                    }
                )
            finally:
                crop.close()
                if prepared is not None:
                    prepared.close()

            if region["name"] != "full_sheet":
                x1, y1, x2, y2 = region["box"]
                outline = colors.get(
                    region["name"],
                    (0, 102, 255, 255) if region["name"].startswith("candidate_text_table") else (234, 88, 12, 255),
                )
                line_width = max(3, int(round(max(image.size) / 800.0)))
                draw.rectangle([x1, y1, x2, y2], outline=outline, width=line_width)
                label = make_annotation_label_ascii(region["name"])
                label_bbox = draw.textbbox((0, 0), label, font=font)
                label_width = label_bbox[2] - label_bbox[0]
                label_height = label_bbox[3] - label_bbox[1]
                pad = max(6, line_width * 2)
                label_y = max(0, y1 - label_height - (2 * pad))
                draw.rectangle([x1, label_y, x1 + label_width + (2 * pad), label_y + label_height + (2 * pad)], fill=outline)
                draw.text((x1 + pad, label_y + pad), label, fill=(255, 255, 255, 255), font=font)

        overview_path = crop_dir / "{}_crop_overview.png".format(output_stem)
        overview.convert("RGB").save(overview_path)
        saved.append(
            {
                "name": "crop_overview",
                "description": "Full preprocessed sheet with dynamic crop boxes overlaid.",
                "bbox": [0, 0, image.size[0], image.size[1]],
                "path": str(overview_path.resolve()),
            }
        )
        return {"status": "saved", "folder": str(crop_dir.resolve()), "sheet_info": sheet_info or {}, "outputs": saved}
    finally:
        if source_image is not None:
            source_image.close()
        image.close()


def sanitize_output_name(value, fallback="document"):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return text or fallback


def resolve_output_dir(image_path, result_folder, output_name=None):
    image_name = sanitize_output_name(output_name or Path(image_path).stem, fallback=Path(image_path).stem)
    output_dir = Path(result_folder) / image_name
    output_dir.mkdir(parents=True, exist_ok=True)
    return image_name, output_dir


def resolve_output_db_path(output_dir, db_path=None):
    resolved = Path(db_path) if db_path else Path(output_dir) / "drawing_data.db"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def get_bbox_schema():
    return {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 4,
        "maxItems": 4,
    }


def get_nullable_bbox_schema():
    return {
        "type": ["array", "null"],
        "items": {"type": "integer"},
        "minItems": 4,
        "maxItems": 4,
    }


def get_dimension_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "dimension_id",
            "raw_text",
            "dimension_type",
            "nominal_value",
            "tolerance_class",
            "tolerance_format",
            "tolerance_pattern",
            "tolerance_plus",
            "tolerance_minus",
            "bbox",
            "status",
        ],
        "properties": {
            "dimension_id": {"type": "string"},
            "raw_text": {"type": "string"},
            "dimension_type": {
                "type": "string",
                "enum": ["linear", "diameter", "radius", "angular", "depth", "thread", "unknown"],
            },
            "nominal_value": {"type": ["number", "null"]},
            "tolerance_class": {
                "type": "string",
                "enum": ["bilateral", "unilateral", "limit", "none", "uncertain"],
            },
            "tolerance_format": {
                "type": "string",
                "enum": ["symmetric", "asymmetric", "stacked", "limit", "single_sided", "none", "uncertain"],
            },
            "tolerance_pattern": {
                "type": "string",
                "enum": ["plus_minus", "plus_only", "minus_only", "none", "uncertain"],
            },
            "tolerance_plus": {"type": ["number", "null"]},
            "tolerance_minus": {"type": ["number", "null"]},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_feature_control_frame_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["fcf_id", "raw_text", "bbox", "status"],
        "properties": {
            "fcf_id": {"type": "string"},
            "raw_text": {"type": "string"},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_datum_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["datum_id", "label", "bbox", "status"],
        "properties": {
            "datum_id": {"type": "string"},
            "label": {"type": "string"},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_title_block_field_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["field_name", "field_value", "raw_text", "bbox", "status"],
        "properties": {
            "field_name": {"type": "string"},
            "field_value": {"type": "string"},
            "raw_text": {"type": "string"},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_title_block_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["raw_text", "bbox", "fields", "status"],
        "properties": {
            "raw_text": {"type": "string"},
            "bbox": get_nullable_bbox_schema(),
            "fields": {"type": "array", "items": get_title_block_field_object_schema()},
            "status": {"type": "string", "enum": ["parsed", "uncertain", "not_found"]},
        },
    }


def get_bom_cell_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["column_name", "text", "bbox", "status"],
        "properties": {
            "column_name": {"type": "string"},
            "text": {"type": "string"},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_bom_row_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["row_id", "raw_text", "bbox", "cells", "status"],
        "properties": {
            "row_id": {"type": "string"},
            "raw_text": {"type": "string"},
            "bbox": get_bbox_schema(),
            "cells": {"type": "array", "items": get_bom_cell_object_schema()},
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_bill_of_materials_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["table_title", "bbox", "columns", "rows", "status"],
        "properties": {
            "table_title": {"type": "string"},
            "bbox": get_nullable_bbox_schema(),
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": get_bom_row_object_schema()},
            "status": {"type": "string", "enum": ["parsed", "uncertain", "not_found"]},
        },
    }


def get_note_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["note_id", "note_label", "raw_text", "bbox", "status"],
        "properties": {
            "note_id": {"type": "string"},
            "note_label": {"type": "string"},
            "raw_text": {"type": "string"},
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_other_annotation_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["annotation_id", "label", "annotation_type", "bbox", "status"],
        "properties": {
            "annotation_id": {"type": "string"},
            "label": {"type": "string"},
            "annotation_type": {
                "type": "string",
                "enum": ["section_label", "detail_label", "reference_label", "other"],
            },
            "bbox": get_bbox_schema(),
            "status": {"type": "string", "enum": ["parsed", "uncertain"]},
        },
    }


def get_view_structure_object_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["view_id", "view_type", "view_bbox", "view_description"],
        "properties": {
            "view_id": {"type": "string"},
            "view_type": {
                "type": "string",
                "enum": ["front", "top", "side", "section", "detail", "isometric", "unknown"],
            },
            "view_bbox": get_bbox_schema(),
            "view_description": {"type": "string"},
        },
    }


def get_output_schema():
    view_object = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "view_id",
            "view_type",
            "view_bbox",
            "view_description",
            "dimensions",
            "size_tolerance_count",
            "other_tolerance_count",
            "feature_control_frames",
            "datums",
        ],
        "properties": {
            "view_id": {"type": "string"},
            "view_type": {
                "type": "string",
                "enum": ["front", "top", "side", "section", "detail", "isometric", "unknown"],
            },
            "view_bbox": get_bbox_schema(),
            "view_description": {"type": "string"},
            "dimensions": {"type": "array", "items": get_dimension_object_schema()},
            "size_tolerance_count": {"type": "integer"},
            "other_tolerance_count": {"type": "integer"},
            "feature_control_frames": {"type": "array", "items": get_feature_control_frame_object_schema()},
            "datums": {"type": "array", "items": get_datum_object_schema()},
        },
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_id",
            "drawing_name",
            "drawing_number",
            "unit",
            "unit_inferred",
            "part_description",
            "summary_text",
            "title_block",
            "bill_of_materials",
            "notes",
            "other_annotations",
            "views",
            "unassigned",
        ],
        "properties": {
            "document_id": {"type": "string"},
            "drawing_name": {"type": ["string", "null"]},
            "drawing_number": {"type": ["string", "null"]},
            "unit": {"type": ["string", "null"]},
            "unit_inferred": {"type": "boolean"},
            "part_description": {"type": "string"},
            "summary_text": {"type": "string"},
            "title_block": get_title_block_object_schema(),
            "bill_of_materials": get_bill_of_materials_object_schema(),
            "notes": {"type": "array", "items": get_note_object_schema()},
            "other_annotations": {"type": "array", "items": get_other_annotation_object_schema()},
            "views": {"type": "array", "items": view_object},
            "unassigned": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimensions", "feature_control_frames", "datums"],
                "properties": {
                    "dimensions": {"type": "array", "items": get_dimension_object_schema()},
                    "feature_control_frames": {"type": "array", "items": get_feature_control_frame_object_schema()},
                    "datums": {"type": "array", "items": get_datum_object_schema()},
                },
            },
        },
    }


def get_structure_pass_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_id",
            "drawing_name",
            "drawing_number",
            "unit",
            "unit_inferred",
            "part_description",
            "title_block",
            "bill_of_materials",
            "notes",
            "other_annotations",
            "views",
        ],
        "properties": {
            "document_id": {"type": "string"},
            "drawing_name": {"type": ["string", "null"]},
            "drawing_number": {"type": ["string", "null"]},
            "unit": {"type": ["string", "null"]},
            "unit_inferred": {"type": "boolean"},
            "part_description": {"type": "string"},
            "title_block": get_title_block_object_schema(),
            "bill_of_materials": get_bill_of_materials_object_schema(),
            "notes": {"type": "array", "items": get_note_object_schema()},
            "other_annotations": {"type": "array", "items": get_other_annotation_object_schema()},
            "views": {"type": "array", "items": get_view_structure_object_schema()},
        },
    }


def get_dimension_pass_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["views", "unassigned"],
        "properties": {
            "views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["view_id", "dimensions"],
                    "properties": {
                        "view_id": {"type": "string"},
                        "dimensions": {"type": "array", "items": get_dimension_object_schema()},
                    },
                },
            },
            "unassigned": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimensions"],
                "properties": {
                    "dimensions": {"type": "array", "items": get_dimension_object_schema()},
                },
            },
        },
    }


def get_gdt_pass_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["views", "unassigned"],
        "properties": {
            "views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["view_id", "feature_control_frames", "datums"],
                    "properties": {
                        "view_id": {"type": "string"},
                        "feature_control_frames": {"type": "array", "items": get_feature_control_frame_object_schema()},
                        "datums": {"type": "array", "items": get_datum_object_schema()},
                    },
                },
            },
            "unassigned": {
                "type": "object",
                "additionalProperties": False,
                "required": ["feature_control_frames", "datums"],
                "properties": {
                    "feature_control_frames": {"type": "array", "items": get_feature_control_frame_object_schema()},
                    "datums": {"type": "array", "items": get_datum_object_schema()},
                },
            },
        },
    }


def load_prompt_template(prompt_file=None):
    if not prompt_file:
        return DEFAULT_PROMPT_TEMPLATE
    return Path(prompt_file).read_text(encoding="utf-8").strip()


def get_prompt(document_id, source_file_name="", source_file_stem="", prompt_template=None):
    template = (prompt_template or DEFAULT_PROMPT_TEMPLATE).strip()
    return template.format(
        document_id=document_id,
        source_file_name=source_file_name,
        source_file_stem=source_file_stem,
    )


def extract_response_json(response):
    if hasattr(response, "output_text") and isinstance(response.output_text, str) and response.output_text.strip():
        return json.loads(response.output_text)

    response_dict = response.model_dump()
    output_text = response_dict.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return json.loads(output_text)

    texts = []
    for item in response_dict.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if texts:
        return json.loads("".join(texts))

    raise RuntimeError("OpenAI response did not contain JSON text output.")


def build_openai_image_content(image_parts):
    content = []
    for index, part in enumerate(image_parts, start=1):
        label = "Image {} - {}: {}".format(index, part.get("name", "drawing_region"), part.get("description", ""))
        if part.get("bbox"):
            label = "{} Crop bbox on preprocessed sheet: {}.".format(label, part["bbox"])
        content.append({"type": "input_text", "text": label})
        content.append(
            {
                "type": "input_image",
                "image_url": part["image_url"],
                "detail": "high",
            }
        )
    return content


def run_json_schema_request(client, model, image_data_url, prompt, schema_name, schema, image_parts=None):
    image_content = build_openai_image_content(image_parts) if image_parts else [
        {
            "type": "input_image",
            "image_url": image_data_url,
            "detail": "high",
        }
    ]
    last_error = None
    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}] + image_content,
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
            )
            return extract_response_json(response)
        except (APIConnectionError, APITimeoutError) as exc:
            last_error = exc
            if attempt >= 3:
                break
            wait_seconds = min(20, 5 * (2 ** (attempt - 1)))
            print(
                "OpenAI request retry {}/3 after {}. Waiting {} seconds...".format(
                    attempt,
                    exc.__class__.__name__,
                    wait_seconds,
                )
            )
            time.sleep(wait_seconds)

    raise last_error


def build_view_context_text(structure_result):
    view_items = []
    for view in structure_result.get("views", []):
        view_items.append(
            {
                "view_id": view.get("view_id"),
                "view_type": view.get("view_type"),
                "view_bbox": view.get("view_bbox"),
                "view_description": view.get("view_description"),
            }
        )
    if not view_items:
        return "[]"
    return json.dumps(view_items, ensure_ascii=False)


def build_note_context_text(structure_result):
    note_items = []
    for note in structure_result.get("notes", []):
        note_items.append(
            {
                "note_id": note.get("note_id"),
                "note_label": note.get("note_label"),
                "raw_text": note.get("raw_text"),
            }
        )
    if not note_items:
        return "[]"
    return json.dumps(note_items, ensure_ascii=False)


def iter_all_dimensions(result):
    for view in result.get("views", []):
        for dimension in view.get("dimensions", []):
            yield dimension
    for dimension in result.get("unassigned", {}).get("dimensions", []):
        yield dimension


def build_dimension_field_counts(result):
    counts = {
        "dimension_rows": 0,
        "dimension_id_count": 0,
        "raw_text_count": 0,
        "nominal_value_count": 0,
        "tolerance_upper_count": 0,
        "tolerance_lower_count": 0,
    }

    for dimension in iter_all_dimensions(result):
        counts["dimension_rows"] += 1
        if str(dimension.get("dimension_id") or "").strip():
            counts["dimension_id_count"] += 1
        if str(dimension.get("raw_text") or "").strip():
            counts["raw_text_count"] += 1
        if dimension.get("nominal_value") is not None:
            counts["nominal_value_count"] += 1
        if dimension.get("tolerance_plus") is not None:
            counts["tolerance_upper_count"] += 1
        if dimension.get("tolerance_minus") is not None:
            counts["tolerance_lower_count"] += 1

    return counts


def bbox_iou(bbox_a, bbox_b):
    if not bbox_a or not bbox_b:
        return 0.0
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0, inter_x2 - inter_x1)
    inter_h = max(0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union_area = area_a + area_b - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / float(union_area)


def normalize_dimension_text(text):
    normalized = str(text or "").strip()
    normalized = normalized.replace("Â±", "±").replace("âŒ€", "⌀")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def bbox_center_distance(bbox_a, bbox_b):
    if not bbox_a or not bbox_b:
        return float("inf")
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b
    acx = (ax1 + ax2) / 2.0
    acy = (ay1 + ay2) / 2.0
    bcx = (bx1 + bx2) / 2.0
    bcy = (by1 + by2) / 2.0
    return ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5


def parse_float_token(text):
    cleaned = str(text or "").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_tolerance_hints_from_text(raw_text):
    text = normalize_dimension_text(raw_text)
    if not text:
        return None

    normalized = (
        text.replace("\uFF0B", "+")
        .replace("\uFF0D", "-")
        .replace("\uFF0F", "/")
        .replace("\uFF0C", ".")
        .replace(" ", "")
    )
    number_pattern = r"\d+(?:\.\d+)?"

    plus_minus_match = re.search(
        r"(?P<nom>{0})(?:\u00B1|\+/-)(?P<tol>{0})".format(number_pattern),
        normalized,
    )
    if plus_minus_match:
        nominal_value = parse_float_token(plus_minus_match.group("nom"))
        tolerance_value = parse_float_token(plus_minus_match.group("tol"))
        if nominal_value is not None and tolerance_value is not None:
            return {
                "nominal_value": nominal_value,
                "tolerance_class": "bilateral",
                "tolerance_format": "symmetric",
                "tolerance_pattern": "plus_minus",
                "tolerance_plus": tolerance_value,
                "tolerance_minus": -tolerance_value,
            }

    asymmetric_match = re.search(
        r"(?P<nom>{0})\+(?P<plus>{0})(?:/)?-(?P<minus>{0})".format(number_pattern),
        normalized,
    )
    if asymmetric_match:
        nominal_value = parse_float_token(asymmetric_match.group("nom"))
        tolerance_plus = parse_float_token(asymmetric_match.group("plus"))
        tolerance_minus = parse_float_token(asymmetric_match.group("minus"))
        if nominal_value is not None and tolerance_plus is not None and tolerance_minus is not None:
            return {
                "nominal_value": nominal_value,
                "tolerance_class": "bilateral",
                "tolerance_format": "asymmetric",
                "tolerance_pattern": "plus_minus",
                "tolerance_plus": tolerance_plus,
                "tolerance_minus": -abs(tolerance_minus),
            }

    plus_only_match = re.search(
        r"(?P<nom>{0})\+(?P<plus>{0})(?![/-])".format(number_pattern),
        normalized,
    )
    if plus_only_match:
        nominal_value = parse_float_token(plus_only_match.group("nom"))
        tolerance_plus = parse_float_token(plus_only_match.group("plus"))
        if nominal_value is not None and tolerance_plus is not None:
            return {
                "nominal_value": nominal_value,
                "tolerance_class": "unilateral",
                "tolerance_format": "single_sided",
                "tolerance_pattern": "plus_only",
                "tolerance_plus": tolerance_plus,
                "tolerance_minus": None,
            }

    minus_only_match = re.search(
        r"(?P<nom>{0})-(?P<minus>{0})$".format(number_pattern),
        normalized,
    )
    if minus_only_match:
        nominal_value = parse_float_token(minus_only_match.group("nom"))
        tolerance_minus = parse_float_token(minus_only_match.group("minus"))
        if nominal_value is not None and tolerance_minus is not None:
            return {
                "nominal_value": nominal_value,
                "tolerance_class": "unilateral",
                "tolerance_format": "single_sided",
                "tolerance_pattern": "minus_only",
                "tolerance_plus": None,
                "tolerance_minus": -abs(tolerance_minus),
            }

    limit_match = re.fullmatch(
        r"[A-Za-z\u2300\u00D8]*?(?P<first>{0})/(?P<second>{0})".format(number_pattern),
        normalized,
    )
    if limit_match:
        return {
            "nominal_value": None,
            "tolerance_class": "limit",
            "tolerance_format": "limit",
            "tolerance_pattern": "uncertain",
            "tolerance_plus": None,
            "tolerance_minus": None,
        }

    return None


def repair_dimension_tolerance_fields(dimension):
    tolerance_hints = extract_tolerance_hints_from_text(dimension.get("raw_text"))
    if not tolerance_hints:
        return dimension

    if dimension.get("nominal_value") is None and tolerance_hints.get("nominal_value") is not None:
        dimension["nominal_value"] = tolerance_hints["nominal_value"]

    if dimension.get("tolerance_plus") is None and tolerance_hints.get("tolerance_plus") is not None:
        dimension["tolerance_plus"] = tolerance_hints["tolerance_plus"]

    if dimension.get("tolerance_minus") is None and tolerance_hints.get("tolerance_minus") is not None:
        dimension["tolerance_minus"] = tolerance_hints["tolerance_minus"]

    if dimension.get("tolerance_class") in (None, "", "none", "uncertain"):
        dimension["tolerance_class"] = tolerance_hints["tolerance_class"]

    if dimension.get("tolerance_format") in (None, "", "none", "uncertain"):
        dimension["tolerance_format"] = tolerance_hints["tolerance_format"]

    if dimension.get("tolerance_pattern") in (None, "", "none", "uncertain"):
        dimension["tolerance_pattern"] = tolerance_hints["tolerance_pattern"]

    return dimension


def repair_result_tolerance_fields(result):
    for view in result.get("views", []):
        dimensions = view.get("dimensions", [])
        for dimension in dimensions:
            repair_dimension_tolerance_fields(dimension)
        if "size_tolerance_count" in view:
            view["size_tolerance_count"] = count_size_tolerances(dimensions)

    for dimension in result.get("unassigned", {}).get("dimensions", []):
        repair_dimension_tolerance_fields(dimension)

    if "summary_text" in result:
        result["summary_text"] = generate_summary_text(result)

    return result


def is_obvious_false_dimension_text(raw_text):
    text = normalize_dimension_text(raw_text)
    lower = text.lower()

    if not text:
        return True

    if re.fullmatch(r"[A-Za-z]", text):
        return True

    if re.fullmatch(r"[A-Za-z]\s*\([A-Za-z0-9]+\)", text):
        return True

    measurable_keywords = (
        "min",
        "max",
        "minimum",
        "maximum",
        "thickness",
        "width",
        "height",
        "depth",
        "length",
        "pitch",
        "diameter",
        "radius",
        "axis",
        "gap",
        "spacing",
        "distance",
        "wall",
        "hole",
        "slot",
        "thread",
        "typ",
        "mm",
    )
    has_measurable_context = any(keyword in lower for keyword in measurable_keywords) and re.search(r"\d", text)

    exclusion_patterns = [
        r"\bscale\b",
        r"\brev(?:ision)?\b",
        r"\bsheet\b",
        r"\bitem\s+\d+\b",
        r"\bdetail\s+[A-Z]\b",
        r"\bsection\s+[A-Z](?:-[A-Z])?\b",
    ]
    if any(re.search(pattern, lower) for pattern in exclusion_patterns):
        return True

    if not has_measurable_context and any(
        re.search(pattern, lower)
        for pattern in (
            r"\bsee\s+note\b",
            r"\bref\.?\s*note\b",
            r"\brefer\s+to\s+note\b",
            r"\bper\s+note\b",
            r"\bnote\s+\d+\b",
        )
    ):
        return True

    if re.fullmatch(r"\(?\s*\d+\s*(?:holes?|places?)\s*\)?", lower):
        return True

    if re.fullmatch(r"\(?\s*\d+\s*x\s*\)?", lower):
        return True

    if not re.search(r"\d", text):
        return True

    return False


def is_feature_control_frame_like_text(raw_text):
    text = normalize_dimension_text(raw_text)
    if not text:
        return False

    if any(symbol in text for symbol in ("|", "⌖", "⌾", "⌒", "⌓", "⌿", "▱", "⊥", "∥")):
        return True

    compact = text.replace(" ", "")
    if re.fullmatch(r"[⌀Ø]?\d+(?:[.,]\d+)?(?:[A-Z](?:\([A-Z]\))?){1,3}", compact):
        return True

    datum_refs = re.findall(r"(?:(?<=\s)|^)([A-Z](?:\([A-Z]\))?)(?=(?:\s|$))", text)
    if not datum_refs:
        return False

    if re.search(r"[⌀Ø]\s*\d", text):
        return True

    if len(datum_refs) >= 2 and re.search(r"\d", text):
        return True

    gdt_keywords = ("datum", "position", "profile", "perpendicularity", "parallelism", "flatness", "runout")
    if any(keyword in text.lower() for keyword in gdt_keywords):
        return True

    return False


def is_feature_control_frame_like_text_strict(raw_text):
    text = normalize_dimension_text(raw_text)
    if not text:
        return False

    if is_feature_control_frame_like_text(text):
        return True

    modifier_text = (
        text.replace("Ⓜ", " M ")
        .replace("Ⓔ", " E ")
        .replace("Ⓛ", " L ")
        .replace("Ⓢ", " S ")
        .replace("\u24C2", " M ")
        .replace("\u24BA", " E ")
        .replace("\u24C1", " L ")
        .replace("\u24C8", " S ")
    )
    compact_modifier = modifier_text.replace(" ", "")

    # OCR/VLM can degrade a pure FCF tolerance-zone payload into strings like
    # "? 0,2(M)" or "0,2(M)". Those are not standalone size dimensions.
    if re.fullmatch(r"[?⌀Ø]?\d+(?:[.,]\d+)?\((?:M|L|S|P|E)\)", compact_modifier):
        return True

    if re.search(r"\(\s*(?:M|L|S|P|E)\s*\)", modifier_text) and re.search(r"\d", modifier_text):
        if not re.search(r"[±+\-]\s*\d", modifier_text):
            return True

    # FCF-style tolerance zones often look like diameter/number + modifiers/datum refs.
    if re.fullmatch(r"[âŒ€Ã˜]?\d+(?:[.,]\d+)?(?:[MELSP]|[A-Z](?:\([A-Z]\))?){2,4}", compact_modifier):
        return True

    datum_or_modifier_refs = re.findall(r"(?:(?<=\s)|^)([A-Z](?:\([A-Z]\))?)(?=(?:\s|$))", modifier_text)
    if len(datum_or_modifier_refs) >= 2 and re.search(r"\d", modifier_text):
        return True

    if re.search(r"[âŒ€Ã˜]\s*\d+(?:[.,]\d+)?\s*[MELS]\b", modifier_text):
        return True

    return False


def looks_dimension_like(dimension):
    raw_text = normalize_dimension_text(dimension.get("raw_text"))
    lower = raw_text.lower()
    dimension_type = dimension.get("dimension_type", "unknown")

    if is_obvious_false_dimension_text(raw_text):
        return False

    if is_feature_control_frame_like_text_strict(raw_text):
        return False

    if dimension_type in ("diameter", "radius", "angular", "depth", "thread"):
        return True

    if any(symbol in raw_text for symbol in ("⌀", "Ø", "R", "M", "°", "±", "+", "-", "/")):
        return True

    dimension_keywords = (
        "min",
        "max",
        "minimum",
        "maximum",
        "thickness",
        "width",
        "height",
        "depth",
        "length",
        "pitch",
        "diameter",
        "radius",
        "axis",
        "gap",
        "spacing",
        "distance",
        "wall",
        "hole",
        "slot",
        "thread",
        "typ",
        "mm",
    )
    if any(keyword in lower for keyword in dimension_keywords):
        return True

    nominal_value = dimension.get("nominal_value")
    if nominal_value is not None and re.search(r"\d", raw_text):
        return True

    if re.fullmatch(r"\(?\s*[⌀Ø]?\s*\d+(?:[.,]\d+)?\s*\)?", raw_text):
        return True

    return False


def overlaps_gdt_or_datum(dimension, feature_control_frames, datums):
    bbox = dimension.get("bbox")
    if not bbox:
        return False
    for fcf in feature_control_frames:
        if bbox_iou(bbox, fcf.get("bbox")) >= 0.35:
            return True
    for datum in datums:
        if bbox_iou(bbox, datum.get("bbox")) >= 0.35:
            return True
    return False


def bbox_contains(outer_bbox, inner_bbox):
    if not outer_bbox or not inner_bbox:
        return False
    ox1, oy1, ox2, oy2 = outer_bbox
    ix1, iy1, ix2, iy2 = inner_bbox
    return ox1 <= ix1 and oy1 <= iy1 and ox2 >= ix2 and oy2 >= iy2


def overlaps_feature_control_frame(item, feature_control_frames, threshold=0.2):
    bbox = item.get("bbox")
    if not bbox:
        return False
    for fcf in feature_control_frames:
        fcf_bbox = fcf.get("bbox")
        if not fcf_bbox:
            continue
        if bbox_iou(bbox, fcf_bbox) >= threshold or bbox_contains(fcf_bbox, bbox):
            return True
    return False


def overlaps_excluded_region(item, excluded_bboxes, threshold=0.2):
    bbox = item.get("bbox")
    if not bbox:
        return False
    for excluded_bbox in excluded_bboxes or []:
        if not excluded_bbox:
            continue
        if bbox_iou(bbox, excluded_bbox) >= threshold or bbox_contains(excluded_bbox, bbox):
            return True
    return False


def deduplicate_labeled_items(items, text_key):
    kept = []
    seen = []
    for item in items:
        label = normalize_dimension_text(item.get(text_key))
        bbox = item.get("bbox")
        duplicate_found = False
        for seen_label, seen_bbox in seen:
            if label == seen_label and bbox_iou(bbox, seen_bbox) >= 0.5:
                duplicate_found = True
                break
        if duplicate_found:
            continue
        kept.append(item)
        seen.append((label, bbox))
    return kept


def sanitize_datums(datums, feature_control_frames, other_annotations=None, excluded_bboxes=None):
    cleaned = []
    other_annotations = other_annotations or []
    for datum in datums:
        label = normalize_dimension_text(datum.get("label"))
        if not label:
            continue
        if overlaps_excluded_region(datum, excluded_bboxes, threshold=0.15):
            continue
        if overlaps_feature_control_frame(datum, feature_control_frames):
            continue
        if any(bbox_iou(datum.get("bbox"), other.get("bbox")) >= 0.5 for other in other_annotations if other.get("bbox")):
            continue
        cleaned.append(datum)
    return deduplicate_labeled_items(cleaned, "label")


def sanitize_feature_control_frames(feature_control_frames, excluded_bboxes=None):
    cleaned = []
    for fcf in feature_control_frames:
        raw_text = normalize_dimension_text(fcf.get("raw_text"))
        if not raw_text:
            continue
        if overlaps_excluded_region(fcf, excluded_bboxes, threshold=0.15):
            continue
        cleaned.append(fcf)
    return deduplicate_labeled_items(cleaned, "raw_text")


def sanitize_other_annotations(other_annotations, datums, feature_control_frames):
    cleaned = []
    for annotation in other_annotations:
        label = normalize_dimension_text(annotation.get("label"))
        if not label:
            continue
        if any(bbox_iou(annotation.get("bbox"), datum.get("bbox")) >= 0.5 for datum in datums if datum.get("bbox")):
            continue
        if overlaps_feature_control_frame(annotation, feature_control_frames, threshold=0.35):
            continue
        cleaned.append(annotation)
    return deduplicate_labeled_items(cleaned, "label")


def deduplicate_dimensions(dimensions):
    kept = []
    seen = []
    for dimension in dimensions:
        raw_text = normalize_dimension_text(dimension.get("raw_text"))
        bbox = dimension.get("bbox")
        duplicate_found = False
        for seen_text, seen_bbox in seen:
            if raw_text != seen_text:
                continue
            if bbox_iou(bbox, seen_bbox) >= 0.5:
                duplicate_found = True
                break
            if bbox and seen_bbox and bbox_center_distance(bbox, seen_bbox) <= 160.0:
                duplicate_found = True
                break
        if duplicate_found:
            continue
        kept.append(dimension)
        seen.append((raw_text, bbox))
    return kept


def sanitize_dimension_list(dimensions, feature_control_frames, datums, excluded_bboxes=None):
    cleaned = []
    for dimension in dimensions:
        if not looks_dimension_like(dimension):
            continue
        if overlaps_excluded_region(dimension, excluded_bboxes, threshold=0.15):
            continue
        if overlaps_gdt_or_datum(dimension, feature_control_frames, datums):
            continue
        cleaned.append(dimension)
    return deduplicate_dimensions(cleaned)


def build_existing_dimension_context_text(dimension_result):
    lines = []
    for view in dimension_result.get("views", []):
        items = [normalize_dimension_text(dimension.get("raw_text")) for dimension in view.get("dimensions", []) if dimension.get("raw_text")]
        if items:
            lines.append("{}: {}".format(view.get("view_id", "unassigned"), "; ".join(items)))
    unassigned_items = [
        normalize_dimension_text(dimension.get("raw_text"))
        for dimension in dimension_result.get("unassigned", {}).get("dimensions", [])
        if dimension.get("raw_text")
    ]
    if unassigned_items:
        lines.append("unassigned: {}".format("; ".join(unassigned_items)))
    return "\n".join(lines) if lines else "No dimensions extracted yet."


def _dimension_merge_duplicate(existing_dimension, candidate_dimension):
    if normalize_dimension_text(existing_dimension.get("raw_text")) != normalize_dimension_text(candidate_dimension.get("raw_text")):
        return False
    bbox_a = existing_dimension.get("bbox")
    bbox_b = candidate_dimension.get("bbox")
    if bbox_a and bbox_b:
        if bbox_iou(bbox_a, bbox_b) >= 0.2:
            return True
        if bbox_center_distance(bbox_a, bbox_b) <= 160.0:
            return True
        return False
    return True


def merge_dimension_pass_results(primary_dimension_result, recovery_dimension_result):
    merged = {
        "views": [],
        "unassigned": {
            "dimensions": list(primary_dimension_result.get("unassigned", {}).get("dimensions", [])),
        },
    }

    primary_map = {view.get("view_id"): list(view.get("dimensions", [])) for view in primary_dimension_result.get("views", [])}
    recovery_map = {view.get("view_id"): list(view.get("dimensions", [])) for view in recovery_dimension_result.get("views", [])}
    ordered_view_ids = []
    for view in primary_dimension_result.get("views", []):
        view_id = view.get("view_id")
        if view_id not in ordered_view_ids:
            ordered_view_ids.append(view_id)
    for view in recovery_dimension_result.get("views", []):
        view_id = view.get("view_id")
        if view_id not in ordered_view_ids:
            ordered_view_ids.append(view_id)

    def append_new(target_list, additions):
        for dimension in additions:
            if any(_dimension_merge_duplicate(existing, dimension) for existing in target_list):
                continue
            target_list.append(dimension)

    for view_id in ordered_view_ids:
        target_dimensions = list(primary_map.get(view_id, []))
        append_new(target_dimensions, recovery_map.get(view_id, []))
        merged["views"].append(
            {
                "view_id": view_id,
                "dimensions": target_dimensions,
            }
        )

    append_new(
        merged["unassigned"]["dimensions"],
        recovery_dimension_result.get("unassigned", {}).get("dimensions", []),
    )
    return merged


def count_size_tolerances(dimensions):
    count = 0
    for dimension in dimensions:
        if (
            dimension.get("tolerance_class") not in ("none", "uncertain")
            or dimension.get("tolerance_plus") is not None
            or dimension.get("tolerance_minus") is not None
        ):
            count += 1
    return count


def generate_summary_text(result):
    view_labels = ["{} ({})".format(view.get("view_id", "VIEW"), view.get("view_type", "unknown")) for view in result.get("views", [])]
    title_block = result.get("title_block", {})
    bill_of_materials = result.get("bill_of_materials", {})
    notes = result.get("notes", [])

    total_size_tolerances = sum(view.get("size_tolerance_count", 0) for view in result.get("views", []))
    total_other_tolerances = sum(view.get("other_tolerance_count", 0) for view in result.get("views", []))
    total_fcfs = sum(len(view.get("feature_control_frames", [])) for view in result.get("views", []))
    total_datums = sum(len(view.get("datums", [])) for view in result.get("views", []))

    return (
        "Found {} view(s): {}. Units are {}{}."
        " Title block {} with {} field(s); BOM {} with {} row(s)."
        " Extracted {} note(s), {} size tolerance-bearing dimension(s), {} other tolerance item(s), {} feature control frame(s), and {} datum occurrence(s)."
        " Visible geometry summary: {}"
    ).format(
        len(result.get("views", [])),
        ", ".join(view_labels) if view_labels else "none",
        result.get("unit") or "unknown",
        " (inferred)" if result.get("unit_inferred") else "",
        "found" if title_block.get("status") != "not_found" else "not found",
        len(title_block.get("fields", [])),
        "found" if bill_of_materials.get("status") != "not_found" else "not found",
        len(bill_of_materials.get("rows", [])),
        len(notes),
        total_size_tolerances,
        total_other_tolerances,
        total_fcfs,
        total_datums,
        result.get("part_description") or "no visible geometry summary",
    )


def merge_pass_results(document_id, structure_result, dimension_result, gdt_result):
    note_bboxes = [note.get("bbox") for note in structure_result.get("notes", []) if note.get("bbox")]
    merged = {
        "document_id": document_id,
        "drawing_name": structure_result.get("drawing_name"),
        "drawing_number": structure_result.get("drawing_number"),
        "unit": structure_result.get("unit"),
        "unit_inferred": structure_result.get("unit_inferred", False),
        "part_description": structure_result.get("part_description", ""),
        "summary_text": "",
        "title_block": structure_result.get(
            "title_block",
            {"raw_text": "", "bbox": None, "fields": [], "status": "not_found"},
        ),
        "bill_of_materials": structure_result.get(
            "bill_of_materials",
            {"table_title": "", "bbox": None, "columns": [], "rows": [], "status": "not_found"},
        ),
        "notes": structure_result.get("notes", []),
        "other_annotations": list(structure_result.get("other_annotations", [])),
        "views": [],
        "unassigned": {
            "dimensions": list(dimension_result.get("unassigned", {}).get("dimensions", [])),
            "feature_control_frames": list(gdt_result.get("unassigned", {}).get("feature_control_frames", [])),
            "datums": list(gdt_result.get("unassigned", {}).get("datums", [])),
        },
    }

    dimension_map = {view.get("view_id"): list(view.get("dimensions", [])) for view in dimension_result.get("views", [])}
    gdt_map = {
        view.get("view_id"): {
            "feature_control_frames": list(view.get("feature_control_frames", [])),
            "datums": list(view.get("datums", [])),
        }
        for view in gdt_result.get("views", [])
    }

    for view in structure_result.get("views", []):
        view_id = view.get("view_id")
        gdt_view = gdt_map.get(view_id, {})
        feature_control_frames = sanitize_feature_control_frames(
            gdt_view.get("feature_control_frames", []),
            excluded_bboxes=note_bboxes,
        )
        datums = sanitize_datums(
            gdt_view.get("datums", []),
            feature_control_frames,
            excluded_bboxes=note_bboxes,
        )
        dimensions = sanitize_dimension_list(
            dimension_map.get(view_id, []),
            feature_control_frames,
            datums,
            excluded_bboxes=note_bboxes,
        )
        merged["views"].append(
            {
                "view_id": view_id,
                "view_type": view.get("view_type", "unknown"),
                "view_bbox": view.get("view_bbox", [0, 0, 0, 0]),
                "view_description": view.get("view_description", ""),
                "dimensions": dimensions,
                "size_tolerance_count": count_size_tolerances(dimensions),
                "other_tolerance_count": len(feature_control_frames),
                "feature_control_frames": feature_control_frames,
                "datums": datums,
            }
        )

    merged["unassigned"]["dimensions"] = sanitize_dimension_list(
        merged["unassigned"]["dimensions"],
        merged["unassigned"]["feature_control_frames"],
        merged["unassigned"]["datums"],
        excluded_bboxes=note_bboxes,
    )
    merged["unassigned"]["feature_control_frames"] = sanitize_feature_control_frames(
        merged["unassigned"]["feature_control_frames"],
        excluded_bboxes=note_bboxes,
    )
    merged["unassigned"]["datums"] = sanitize_datums(
        merged["unassigned"]["datums"],
        merged["unassigned"]["feature_control_frames"],
        excluded_bboxes=note_bboxes,
    )
    merged["other_annotations"] = sanitize_other_annotations(
        merged["other_annotations"],
        [datum for view in merged["views"] for datum in view.get("datums", [])] + merged["unassigned"]["datums"],
        [fcf for view in merged["views"] for fcf in view.get("feature_control_frames", [])]
        + merged["unassigned"]["feature_control_frames"],
    )
    merged["summary_text"] = generate_summary_text(merged)
    return merged


def run_layer1_2d_extraction_stage(
    image_path,
    document_id,
    model,
    client,
    prompt_template=None,
    pdf_page=1,
    pdf_dpi=450,
    preprocess=True,
    crop_preprocess=False,
    render_metadata=None,
):
    crop_mode = crop_agent_lib.normalize_crop_preprocessing_mode(crop_preprocess)
    active_render_metadata = render_metadata or build_render_metadata(image_path, image_path, pdf_page=pdf_page, pdf_dpi=pdf_dpi)
    coordinate_instruction = "\n" + build_coordinate_system_instruction(active_render_metadata)
    image_parts = crop_agent_lib.build_input_image_parts(
        image_path,
        pdf_page=pdf_page,
        pdf_dpi=pdf_dpi,
        preprocess=preprocess,
        crop_preprocess=crop_mode,
    )
    image_data_url = image_parts[0]["image_url"]
    source_file_name = Path(image_path).name
    source_file_stem = Path(image_path).stem
    crop_instruction = ""
    if crop_mode != "skip":
        crop_instruction = (
            "\nYou are receiving multiple enhanced images of the same drawing sheet: the full sheet plus regional crops. "
            "Use the full sheet for global layout and use the crops to recover small text, notes, title-block fields, BOM, GD&T, and dimensions. "
            "Do not duplicate an item just because it appears in both the full sheet and a crop."
        )
    if prompt_template is not None:
        prompt = get_prompt(
            document_id,
            source_file_name=source_file_name,
            source_file_stem=source_file_stem,
            prompt_template=prompt_template,
        ) + coordinate_instruction + crop_instruction
        return run_json_schema_request(
            client=client,
            model=model,
            image_data_url=image_data_url,
            prompt=prompt,
            schema_name="engineering_drawing_analysis",
            schema=get_output_schema(),
            image_parts=image_parts,
        )

    structure_prompt = STRUCTURE_PASS_PROMPT_TEMPLATE.format(
        document_id=document_id,
        source_file_name=source_file_name,
        source_file_stem=source_file_stem,
    ) + coordinate_instruction + crop_instruction
    structure_result = run_json_schema_request(
        client=client,
        model=model,
        image_data_url=image_data_url,
        prompt=structure_prompt,
        schema_name="engineering_drawing_structure",
        schema=get_structure_pass_schema(),
        image_parts=image_parts,
    )

    view_context = build_view_context_text(structure_result)
    note_context = build_note_context_text(structure_result)
    dimension_prompt = DIMENSION_PASS_PROMPT_TEMPLATE.format(view_context=view_context, note_context=note_context) + coordinate_instruction
    dimension_result = run_json_schema_request(
        client=client,
        model=model,
        image_data_url=image_data_url,
        prompt=dimension_prompt,
        schema_name="engineering_drawing_dimensions",
        schema=get_dimension_pass_schema(),
        image_parts=image_parts,
    )
    recovery_dimension_result = {
        "views": [],
        "unassigned": {"dimensions": []},
    }
    recovery_count = 0
    if structure_result.get("views"):
        existing_dimension_context = build_existing_dimension_context_text(dimension_result)
        dimension_recovery_prompt = DIMENSION_RECOVERY_PASS_PROMPT_TEMPLATE.format(
            view_context=view_context,
            note_context=note_context,
            existing_dimension_context=existing_dimension_context,
        ) + coordinate_instruction
        recovery_dimension_result = run_json_schema_request(
            client=client,
            model=model,
            image_data_url=image_data_url,
            prompt=dimension_recovery_prompt,
            schema_name="engineering_drawing_dimension_recovery",
            schema=get_dimension_pass_schema(),
            image_parts=image_parts,
        )
        recovery_count = sum(len(view.get("dimensions", [])) for view in recovery_dimension_result.get("views", []))
        recovery_count += len(recovery_dimension_result.get("unassigned", {}).get("dimensions", []))
        dimension_result = merge_dimension_pass_results(dimension_result, recovery_dimension_result)

    gdt_prompt = GDT_PASS_PROMPT_TEMPLATE.format(view_context=view_context) + coordinate_instruction
    gdt_result = run_json_schema_request(
        client=client,
        model=model,
        image_data_url=image_data_url,
        prompt=gdt_prompt,
        schema_name="engineering_drawing_gdt",
        schema=get_gdt_pass_schema(),
        image_parts=image_parts,
    )

    merged_result = merge_pass_results(document_id, structure_result, dimension_result, gdt_result)
    merged_result["dimension_recovery_pass"] = {
        "status": "completed" if recovery_count else "no_additions",
        "engine": "openai_recovery_pass",
        "additional_candidates_returned": recovery_count,
    }
    return merged_result


def clamp_bbox(bbox, width, height):
    x1, y1, x2, y2 = [int(round(value)) for value in bbox[:4]]
    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(0, min(width - 1, x2))
    y2 = max(0, min(height - 1, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return [x1, y1, x2, y2]


def get_annotation_scale(width, height):
    reference = max(width, height)
    return {
        "line_width": max(2, int(round(reference / 700.0))),
        "font_size": max(12, int(round(reference / 95.0))),
        "label_padding_x": max(4, int(round(reference / 450.0))),
        "label_padding_y": max(2, int(round(reference / 650.0))),
        "label_gap": max(4, int(round(reference / 420.0))),
        "min_box_size": max(10, int(round(reference / 140.0))),
    }


def get_annotation_font(font_size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", font_size)
    except OSError:
        return ImageFont.load_default()


def expand_small_bbox(bbox, min_size, width, height):
    x1, y1, x2, y2 = bbox
    box_width = x2 - x1
    box_height = y2 - y1

    if box_width < min_size:
        delta = min_size - box_width
        left = delta // 2
        right = delta - left
        x1 = max(0, x1 - left)
        x2 = min(width - 1, x2 + right)

    if box_height < min_size:
        delta = min_size - box_height
        top = delta // 2
        bottom = delta - top
        y1 = max(0, y1 - top)
        y2 = min(height - 1, y2 + bottom)

    return [x1, y1, x2, y2]


def draw_box(draw, bbox, style, label, width, height, font, expand_to_min=True):
    scale = get_annotation_scale(width, height)
    if expand_to_min:
        bbox = expand_small_bbox(bbox, scale["min_box_size"], width, height)
    x1, y1, x2, y2 = bbox
    draw.rectangle([x1, y1, x2, y2], outline=style["outline"], fill=style["fill"], width=scale["line_width"])
    if label:
        text_bbox = draw.textbbox((0, 0), label, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        pad_x = scale["label_padding_x"]
        pad_y = scale["label_padding_y"]
        label_x1 = x1
        label_y1 = max(0, y1 - text_height - (2 * pad_y) - scale["label_gap"])
        label_x2 = min(width - 1, label_x1 + text_width + (2 * pad_x))
        label_y2 = min(height - 1, label_y1 + text_height + (2 * pad_y))
        draw.rectangle([label_x1, label_y1, label_x2, label_y2], fill=style["outline"])
        draw.text((label_x1 + pad_x, label_y1 + pad_y), label, fill=(255, 255, 255, 255), font=font)


def make_annotation_label_ascii(label):
    replacements = {
        "\u2300": "DIA",
        "\u00d8": "DIA",
        "\u00b1": "+/-",
        "\u00b0": "deg",
        "\u2264": "<=",
        "\u2265": ">=",
        "\u00b5": "u",
    }
    safe_label = str(label or "")
    for symbol, replacement in replacements.items():
        safe_label = safe_label.replace(symbol, replacement)
    return safe_label.encode("latin-1", errors="replace").decode("latin-1")


def format_annotation_label(text, fallback, max_length=48):
    label = normalize_dimension_text(text) or fallback
    label = make_annotation_label_ascii(label)
    label = re.sub(r"\s+", " ", label).strip()
    if len(label) <= max_length:
        return label
    return "{}...".format(label[: max_length - 3].rstrip())


def collect_annotation_items(result):
    items = []
    dimension_counter = 1

    for view in result.get("views", []):
        for dimension in view.get("dimensions", []):
            if not dimension.get("annotation_ready"):
                continue
            items.append(
                {
                    "bbox": dimension.get("bbox", [0, 0, 0, 0]),
                    "style": ANNOTATION_STYLES["dimension"],
                    "label": "D{}".format(dimension_counter),
                    "expand_to_min": False,
                }
            )
            dimension_counter += 1
        for datum in view.get("datums", []):
            if not datum.get("annotation_ready"):
                continue
            items.append(
                {
                    "bbox": datum.get("bbox", [0, 0, 0, 0]),
                    "style": ANNOTATION_STYLES["datum"],
                    "label": format_annotation_label(datum.get("label"), "DATUM"),
                }
            )
        for fcf in view.get("feature_control_frames", []):
            if not fcf.get("annotation_ready"):
                continue
            items.append(
                {
                    "bbox": fcf.get("bbox", [0, 0, 0, 0]),
                    "style": ANNOTATION_STYLES["feature_control_frame"],
                    "label": format_annotation_label(fcf.get("raw_text"), fcf.get("fcf_id", "FCF")),
                }
            )

    for dimension in result.get("unassigned", {}).get("dimensions", []):
        if not dimension.get("annotation_ready"):
            continue
        items.append(
            {
                "bbox": dimension.get("bbox", [0, 0, 0, 0]),
                "style": ANNOTATION_STYLES["dimension"],
                "label": "D{}".format(dimension_counter),
                "expand_to_min": False,
            }
        )
        dimension_counter += 1

    for fcf in result.get("unassigned", {}).get("feature_control_frames", []):
        if not fcf.get("annotation_ready"):
            continue
        items.append(
            {
                "bbox": fcf.get("bbox", [0, 0, 0, 0]),
                "style": ANNOTATION_STYLES["feature_control_frame"],
                "label": format_annotation_label(fcf.get("raw_text"), fcf.get("fcf_id", "UNASSIGNED_FCF")),
            }
        )

    for datum in result.get("unassigned", {}).get("datums", []):
        if not datum.get("annotation_ready"):
            continue
        items.append(
            {
                "bbox": datum.get("bbox", [0, 0, 0, 0]),
                "style": ANNOTATION_STYLES["datum"],
                "label": format_annotation_label(datum.get("label"), "UNASSIGNED_DATUM"),
            }
        )

    return items


def annotate_result_image(image_path, result, output_dir, output_stem=None, pdf_page=1, pdf_dpi=450):
    output_path = output_dir / "{}_annotated.png".format(output_stem or Path(image_path).stem)
    save_result_overlay(
        prepared_image_path=image_path,
        result=result,
        output_path=output_path,
        output_dpi=result.get("render_dpi") or pdf_dpi,
        show_labels=True,
        include_feature_control_frames=False,
        include_datums=False,
    )
    return output_path


def build_summary_text(result):
    lines = []
    verification = result.get("dimension_verification", {})
    value_verification = result.get("value_verification", {})
    field_counts = result.get("dimension_field_counts") or build_dimension_field_counts(result)
    lines.append("Document: {}".format(result.get("document_id", "unknown")))
    lines.append("Unit: {} (inferred: {})".format(result.get("unit"), result.get("unit_inferred")))
    lines.append("Dimension Count: {}".format(result.get("dimension_count", 0)))
    lines.append(
        "Visual Verification Counts: rows={}, ids={}, nominals={}, tol_upper={}, tol_lower={}".format(
            field_counts.get("dimension_rows", 0),
            field_counts.get("dimension_id_count", 0),
            field_counts.get("nominal_value_count", 0),
            field_counts.get("tolerance_upper_count", 0),
            field_counts.get("tolerance_lower_count", 0),
        )
    )
    lines.append("Verified Value Count: {}".format(result.get("verified_value_count", 0)))
    lines.append("Verified Location Count: {}".format(result.get("verified_location_count", 0)))
    lines.append("Views found: {}".format(len(result.get("views", []))))
    title_block = result.get("title_block", {})
    bill_of_materials = result.get("bill_of_materials", {})
    lines.append(
        "Title Block: {} (fields: {})".format(
            title_block.get("status", "not_found"),
            len(title_block.get("fields", [])),
        )
    )
    lines.append(
        "Bill Of Materials: {} (rows: {})".format(
            bill_of_materials.get("status", "not_found"),
            len(bill_of_materials.get("rows", [])),
        )
    )
    lines.append("Notes: {}".format(len(result.get("notes", []))))
    if verification:
        lines.append(
            "Dimension Verification: {} green-box dimension(s) out of {} extracted (missing: {}, status: {})".format(
                verification.get("green_box_dimension_count", 0),
                verification.get("extracted_dimension_count", 0),
                verification.get("missing_green_box_dimension_count", 0),
                verification.get("status", "unknown"),
            )
        )
    if value_verification:
        lines.append(
            "Value Verification: {} value(s) found in drawing, {} correctly located, {} bbox mismatch, {} not found (status: {})".format(
                value_verification.get("verified_value_count", 0),
                value_verification.get("verified_location_count", 0),
                value_verification.get("bbox_mismatch_count", 0),
                value_verification.get("not_found_in_drawing_count", 0),
                value_verification.get("status", "unknown"),
            )
        )
    lines.append("")

    if result.get("part_description"):
        lines.append("Part Description:")
        lines.append(result["part_description"])
        lines.append("")

    if title_block.get("fields"):
        lines.append("Title Block Fields:")
        for field in title_block.get("fields", []):
            lines.append("  {}: {}".format(field.get("field_name", ""), field.get("field_value", "")))
        lines.append("")

    if bill_of_materials.get("rows"):
        lines.append("Bill Of Materials:")
        for row in bill_of_materials.get("rows", []):
            lines.append("  {}: {}".format(row.get("row_id", "ROW"), row.get("raw_text", "")))
        lines.append("")

    if result.get("notes"):
        lines.append("Notes:")
        for note in result.get("notes", []):
            label = note.get("note_label") or note.get("note_id", "NOTE")
            lines.append("  {}: {}".format(label, note.get("raw_text", "")))
        lines.append("")

    if result.get("summary_text"):
        lines.append("Model Summary:")
        lines.append(result["summary_text"])
        lines.append("")

    total_dimensions = 0
    total_fcfs = 0
    total_datums = 0
    total_size_tolerances = 0
    total_other_tolerances = 0

    for view in result.get("views", []):
        dimension_count = len(view.get("dimensions", []))
        fcf_count = len(view.get("feature_control_frames", []))
        datum_count = len(view.get("datums", []))
        size_tol_count = view.get("size_tolerance_count", 0)
        other_tol_count = view.get("other_tolerance_count", 0)
        verified_view_count = 0
        for verified_view in verification.get("views", []):
            if verified_view.get("view_id") == view.get("view_id"):
                verified_view_count = verified_view.get("green_box_dimension_count", 0)
                break

        total_dimensions += dimension_count
        total_fcfs += fcf_count
        total_datums += datum_count
        total_size_tolerances += size_tol_count
        total_other_tolerances += other_tol_count

        lines.append("{} ({})".format(view.get("view_id", "VIEW"), view.get("view_type", "unknown")))
        if view.get("view_description"):
            lines.append("  Description: {}".format(view["view_description"]))
        lines.append("  Dimensions: {}".format(dimension_count))
        if verification:
            lines.append("  Green-box dimensions: {}".format(verified_view_count))
        lines.append("  Size tolerances: {}".format(size_tol_count))
        lines.append("  Other tolerances: {}".format(other_tol_count))
        lines.append("  Feature control frames: {}".format(fcf_count))
        lines.append("  Datums: {}".format(datum_count))
        lines.append("")

    unassigned = result.get("unassigned", {})
    lines.append("Totals:")
    lines.append("  Dimensions: {}".format(total_dimensions))
    lines.append("  Size tolerances: {}".format(total_size_tolerances))
    lines.append("  Other tolerances: {}".format(total_other_tolerances))
    lines.append("  Feature control frames: {}".format(total_fcfs))
    lines.append("  Datums: {}".format(total_datums))
    lines.append("")
    lines.append("Unassigned:")
    lines.append("  Dimensions: {}".format(len(unassigned.get("dimensions", []))))
    if verification:
        lines.append(
            "  Green-box dimensions: {}".format(
                verification.get("unassigned", {}).get("green_box_dimension_count", 0)
            )
        )
    lines.append("  Feature control frames: {}".format(len(unassigned.get("feature_control_frames", []))))
    lines.append("  Datums: {}".format(len(unassigned.get("datums", []))))

    return "\n".join(lines)


def save_pipeline_outputs(
    image_path,
    result,
    result_folder,
    output_name=None,
    pdf_page=1,
    pdf_dpi=450,
    save_crop_preprocessing=False,
):
    image_name, output_dir = resolve_output_dir(image_path, result_folder, output_name=output_name)
    crop_mode = crop_agent_lib.normalize_crop_preprocessing_mode(save_crop_preprocessing)

    if crop_mode != "skip":
        result["crop_preprocessing"]["saved_outputs"] = crop_agent_lib.save_crop_preprocessing_outputs(
            image_path,
            output_dir,
            image_name,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
            sheet_info=result.get("sheet_info"),
            crop_mode=crop_mode,
        )

    json_path = output_dir / "{}_drawing_features.json".format(image_name)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path = output_dir / "{}_summary.txt".format(image_name)
    summary_path.write_text(build_summary_text(result), encoding="utf-8")

    annotated_path = annotate_result_image(
        image_path,
        result,
        output_dir,
        output_stem=image_name,
        pdf_page=pdf_page,
        pdf_dpi=pdf_dpi,
    )
    manifest_path = output_dir / "{}_render_manifest.json".format(image_name)
    manifest_payload = {
        "document_id": result.get("document_id"),
        "source_file_name": result.get("source_file_name"),
        "prepared_image_name": result.get("prepared_image_name"),
        "prepared_image_path": result.get("prepared_image_path"),
        "render_dpi": result.get("render_dpi"),
        "page_image_width_px": result.get("page_image_width_px"),
        "page_image_height_px": result.get("page_image_height_px"),
        "pdf_page_width_pt": result.get("pdf_page_width_pt"),
        "pdf_page_height_pt": result.get("pdf_page_height_pt"),
        "bbox_coordinate_space": result.get("bbox_coordinate_space"),
        "bbox_origin": result.get("bbox_origin"),
        "bbox_format": result.get("bbox_format"),
        "annotated_path": str(annotated_path.resolve()),
        "json_path": str(json_path.resolve()),
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return json_path, summary_path, annotated_path, output_dir


def _run_layer1_2d_pipeline_core(
    image_path,
    document_id=None,
    drawing_role="part",
    drawing_name=None,
    model="gpt-5.5",
    result_folder="Results/drawing_pipeline",
    db_path=None,
    api_key=None,
    prompt_file=None,
    prompt_template=None,
    use_multi_pass_extraction=False,
    pdf_page=1,
    pdf_dpi=450,
    enable_crop_preprocessing=False,
    enable_value_verification=True,
    enable_bbox_refinement=False,
    bbox_refiner_glm_model="zai-org/GLM-OCR",
    bbox_refiner_max_new_tokens=512,
    client=None,
    output_name=None,
    progress_callback=None,
):
    resolved_image_path = str(Path(image_path).resolve())
    requested_document_id = document_id or Path(resolved_image_path).stem
    resolved_drawing_name = drawing_name or Path(resolved_image_path).stem
    pipeline_output_name = output_name or requested_document_id
    prepared_output_name, prepared_output_dir = resolve_output_dir(
        resolved_image_path,
        result_folder,
        output_name=pipeline_output_name,
    )
    active_client = client or get_client(api_key)
    crop_mode = crop_agent_lib.normalize_crop_preprocessing_mode(enable_crop_preprocessing)
    sheet_info = drawing_preprocessor_lib.detect_pdf_sheet_info(resolved_image_path, pdf_page=pdf_page)
    active_prompt_template = None if use_multi_pass_extraction else (
        prompt_template if prompt_template is not None else load_prompt_template(prompt_file)
    )

    if progress_callback:
        progress_callback(
            "Layer 1 Stage 01/08 - PDF Clarity Agent: preparing a clean working image from the source PDF/image for OCR and extraction ({})".format(
                requested_document_id
            )
        )

    input_preparation = prepare_pdf_clarity_image(
        resolved_image_path,
        prepared_output_dir,
        prepared_output_name,
        pdf_page=pdf_page,
        pdf_dpi=pdf_dpi,
    )
    prepared_image_path = input_preparation["prepared_image_path"]
    render_metadata = build_render_metadata(
        resolved_image_path,
        prepared_image_path,
        pdf_page=pdf_page,
        pdf_dpi=pdf_dpi,
    )

    if progress_callback:
        progress_callback(
            "Layer 1 Stage 02/08 - OpenAI Extraction Agent: reading drawing structure, dimensions, GD&T, and notes from the prepared image ({})".format(
                requested_document_id
            )
        )

    result = run_layer1_2d_extraction_stage(
        image_path=prepared_image_path,
        document_id=requested_document_id,
        model=model,
        client=active_client,
        prompt_template=active_prompt_template,
        pdf_page=1,
        pdf_dpi=pdf_dpi,
        preprocess=True,
        crop_preprocess=crop_mode,
        render_metadata=render_metadata,
    )
    result["crop_preprocessing"] = {
        "status": "enabled" if crop_mode != "skip" else "skipped",
        "engine": (
            "red_box_sheet_region_crops"
            if crop_mode == "red_boxes"
            else "dynamic_sheet_region_crops"
            if crop_mode == "dynamic"
            else "none"
        ),
        "requested_mode": crop_mode,
        "sheet_info": sheet_info,
    }
    extracted_document_id = sanitize_output_name(result.get("drawing_number"), fallback="") if result.get("drawing_number") else ""
    final_document_id = extracted_document_id or requested_document_id
    result["document_id"] = final_document_id
    result["part_number"] = result.get("drawing_number") or final_document_id
    result["document_number"] = result.get("drawing_number") or final_document_id
    result["drawing_role"] = drawing_role
    result["drawing_name"] = result.get("drawing_name") or resolved_drawing_name
    result["source_file_name"] = Path(resolved_image_path).name
    result["prepared_image_name"] = Path(prepared_image_path).name
    result["prepared_image_path"] = str(Path(prepared_image_path).resolve())
    result["input_preparation"] = input_preparation
    result["sheet_info"] = sheet_info
    result = apply_result_coordinate_space(result, render_metadata)
    result = repair_result_tolerance_fields(result)

    if enable_bbox_refinement:
        if progress_callback:
            progress_callback(
                "Layer 1 Stage 03/08 - Local BBox Refinement Agent: refining extracted dimension boxes with local OCR so positions are tighter ({})".format(
                    final_document_id
                )
            )
        result, bbox_refinement = refine_result_bboxes(
            result,
            prepared_image_path,
            pdf_page=1,
            pdf_dpi=pdf_dpi,
            glm_model_name=bbox_refiner_glm_model,
            max_new_tokens=bbox_refiner_max_new_tokens,
            preprocess=True,
        )
        result = apply_result_coordinate_space(result, render_metadata)
    else:
        if progress_callback:
            progress_callback(
                "Layer 1 Stage 03/08 - VLM Box Assignment Agent: using model-detected boxes directly and preparing fallback annotations ({})".format(
                    final_document_id
                )
            )
        fallback_counts = enable_vlm_annotation_fallback(result)
        bbox_refinement = {
            "status": "vlm_only",
            "engine": "openai_vlm",
            "hybrid_mode": False,
            "fallback_dimensions": fallback_counts["dimensions"],
            "fallback_feature_control_frames": fallback_counts["feature_control_frames"],
            "fallback_datums": fallback_counts["datums"],
            "ready_annotation_count": count_ready_target_annotations(result),
        }
        result["bbox_refinement"] = bbox_refinement
        result = apply_result_coordinate_space(result, render_metadata)

    if Path(resolved_image_path).suffix.lower() == ".pdf":
        if progress_callback:
            progress_callback(
                "Layer 1 Stage 03b/08 - PDF Text BBox Grounding Agent: aligning extracted dimensions to native PDF text boxes for cleaner locations ({})".format(
                    final_document_id
                )
            )
        result, pdf_text_bbox_refinement = refine_result_bboxes_from_pdf_text(
            result,
            resolved_image_path,
            pdf_page=pdf_page,
        )
        result = apply_result_coordinate_space(result, render_metadata)
    else:
        pdf_text_bbox_refinement = {
            "status": "skipped",
            "reason": "source is not a PDF",
            "dimensions_refined": 0,
        }
        result["pdf_text_bbox_refinement"] = pdf_text_bbox_refinement

    if progress_callback:
        progress_callback(
            "Layer 1 Stage 04/08 - Dimension Verification Agent: checking extracted dimensions against drawing coverage and required fields ({})".format(
                final_document_id
            )
        )

    result["dimension_verification"] = build_dimension_verification(result)
    result["dimension_count"] = result["dimension_verification"].get("green_box_dimension_count", 0)
    result["extracted_dimension_count"] = result["dimension_verification"].get("extracted_dimension_count", 0)
    result["dimension_field_counts"] = build_dimension_field_counts(result)

    if enable_value_verification:
        if progress_callback:
            progress_callback(
                "Layer 1 Stage 05/08 - Value Verification Agent: cross-checking extracted values and locations against local OCR text ({})".format(
                    final_document_id
                )
            )

        result, value_verification = verify_dimension_values(
            result,
            prepared_image_path,
            pdf_page=1,
            pdf_dpi=pdf_dpi,
            glm_model_name=bbox_refiner_glm_model,
            max_new_tokens=bbox_refiner_max_new_tokens,
            preprocess=True,
        )
        result = apply_result_coordinate_space(result, render_metadata)
    else:
        if progress_callback:
            progress_callback(
                "Layer 1 Stage 05/08 - Value Verification Agent skipped: local OCR value cross-check is disabled ({})".format(
                    final_document_id
                )
            )
        value_verification = {
            "status": "skipped",
            "engine": "none",
            "reason": "local CRAFT/GLM-OCR value verification disabled",
            "total_dimensions": result.get("extracted_dimension_count", 0),
            "verified_value_count": 0,
            "verified_location_count": 0,
            "bbox_mismatch_count": 0,
            "not_found_in_drawing_count": 0,
            "no_text_count": 0,
            "needs_manual_review": False,
        }
        result["value_verification"] = value_verification
        result = apply_result_coordinate_space(result, render_metadata)
    result["verified_value_count"] = value_verification.get("verified_value_count", 0)
    result["verified_location_count"] = value_verification.get("verified_location_count", 0)
    result["bbox_mismatch_count"] = value_verification.get("bbox_mismatch_count", 0)
    result["dimension_value_review_required"] = value_verification.get("needs_manual_review", False)

    return {
        "document_id": final_document_id,
        "result": result,
        "bbox_refinement": bbox_refinement,
        "pdf_text_bbox_refinement": pdf_text_bbox_refinement,
        "resolved_image_path": Path(resolved_image_path).resolve(),
        "prepared_image_path": Path(prepared_image_path).resolve(),
        "result_folder": result_folder,
        "db_path_override": db_path,
        "pdf_dpi": pdf_dpi,
        "crop_mode": crop_mode,
        "pipeline_output_name": pipeline_output_name,
        "model_used": model,
    }


def finalize_pipeline_output(core_output, progress_callback=None):
    result = core_output["result"]
    final_document_id = core_output["document_id"]

    if progress_callback:
        progress_callback(
            "Layer 1 Stage 06/08 - Output Writer Agent: saving JSON, summary text, and annotated image outputs ({})".format(
                final_document_id
            )
        )

    json_path, summary_path, annotated_path, output_dir = save_pipeline_outputs(
        str(core_output["prepared_image_path"]),
        result,
        core_output["result_folder"],
        output_name=core_output["pipeline_output_name"],
        pdf_page=1,
        pdf_dpi=core_output["pdf_dpi"],
        save_crop_preprocessing=core_output["crop_mode"],
    )
    resolved_db_path = resolve_output_db_path(output_dir, db_path=core_output["db_path_override"])

    if progress_callback:
        progress_callback(
            "Layer 1 Stage 07/08 - SQLite Import Agent: importing drawing data, dimensions, and metadata into SQLite ({})".format(
                final_document_id
            )
        )

    import_extraction_json_to_sqlite(str(json_path), str(resolved_db_path))

    if progress_callback:
        progress_callback(
            "Layer 1 Completed 07/08 - Core drawing pipeline saved to SQLite and output files are ready ({})".format(
                final_document_id
            )
        )

    finalized = dict(core_output)
    finalized.update(
        {
            "json_path": json_path,
            "summary_path": summary_path,
            "annotated_path": annotated_path,
            "db_path": resolved_db_path.resolve(),
        }
    )
    return finalized


def run_catia_parameter_sync(
    db_path,
    root_document_path=None,
    dimension_document_id=None,
    dimension_limit=None,
    target_identity=None,
    summary_json=None,
    tolerance_mode="ask",
    approved_only=False,
    progress_callback=None,
):
    if progress_callback:
        progress_callback("Post-run CATIA Parameter Agent - Syncing dimensions into CATIA")

    try:
        from ..catia_agents.create_length_parameters_agent import sync_parameters_from_dimension_db
    except ImportError as exc:
        try:
            from openai_pipeline.pipeline_stack.catia_agents.create_length_parameters_agent import sync_parameters_from_dimension_db
        except ImportError:
            raise RuntimeError("Could not import the CATIA length-parameter agent.") from exc

    return sync_parameters_from_dimension_db(
        dimension_db=db_path,
        root_document_path=root_document_path,
        dimension_document_id=dimension_document_id,
        dimension_limit=dimension_limit,
        target_identity=target_identity,
        summary_json=summary_json,
        tolerance_mode=tolerance_mode,
        approved_only=approved_only,
    )


def run_layer1_2d_pipeline(
    image_path,
    document_id=None,
    drawing_role="part",
    drawing_name=None,
    model=DEFAULT_HYBRID_FALLBACK_MODEL,
    model_strategy="direct",
    primary_model=DEFAULT_HYBRID_PRIMARY_MODEL,
    result_folder="Results/drawing_pipeline",
    db_path=None,
    api_key=None,
    prompt_file=None,
    prompt_template=None,
    use_multi_pass_extraction=False,
    pdf_page=1,
    pdf_dpi=450,
    enable_crop_preprocessing=False,
    enable_value_verification=True,
    enable_bbox_refinement=False,
    bbox_refiner_glm_model="zai-org/GLM-OCR",
    bbox_refiner_max_new_tokens=512,
    client=None,
    output_name=None,
    progress_callback=None,
    fallback_min_extracted_dimensions=1,
    fallback_min_green_box_dimensions=1,
    fallback_min_green_box_ratio=0.55,
    fallback_max_bbox_mismatches=3,
    sync_catia_length_parameters=False,
    catia_root_document_path=None,
    catia_dimension_limit=None,
    catia_target_identity=None,
    catia_summary_json=None,
    catia_tolerance_mode="ask",
    catia_approved_only=False,
):
    active_client = client or get_client(api_key)
    normalized_strategy = str(model_strategy or "direct").strip().lower()
    fallback_model = model

    def run_single_model_pass(active_model_name):
        return _run_layer1_2d_pipeline_core(
            image_path=image_path,
            document_id=document_id,
            drawing_role=drawing_role,
            drawing_name=drawing_name,
            model=active_model_name,
            result_folder=result_folder,
            db_path=db_path,
            api_key=api_key,
            prompt_file=prompt_file,
            prompt_template=prompt_template,
            use_multi_pass_extraction=use_multi_pass_extraction,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
            enable_crop_preprocessing=enable_crop_preprocessing,
            enable_value_verification=enable_value_verification,
            enable_bbox_refinement=enable_bbox_refinement,
            bbox_refiner_glm_model=bbox_refiner_glm_model,
            bbox_refiner_max_new_tokens=bbox_refiner_max_new_tokens,
            client=active_client,
            output_name=output_name,
            progress_callback=progress_callback,
        )

    if normalized_strategy != "hybrid":
        selected_output = run_single_model_pass(fallback_model)
        direct_assessment = {"should_fallback": False, "reasons": [], "metrics": {}}
        selected_output["model_strategy"] = "direct"
        selected_output["fallback_triggered"] = False
        selected_output["fallback_assessment"] = direct_assessment
        selected_output["model_attempts"] = [
            build_model_attempt_summary(
                fallback_model,
                selected_output["result"],
                fallback_assessment=direct_assessment,
                accepted=True,
            )
        ]
        selected_output["result"]["model_selection"] = {
            "strategy": "direct",
            "selected_model": fallback_model,
            "fallback_model": fallback_model,
            "primary_model": fallback_model,
            "fallback_triggered": False,
            "fallback_assessment": direct_assessment,
            "attempts": selected_output["model_attempts"],
        }
        finalized_output = finalize_pipeline_output(selected_output, progress_callback=progress_callback)
        if sync_catia_length_parameters:
            finalized_output["catia_parameter_sync"] = run_catia_parameter_sync(
                db_path=str(finalized_output["db_path"]),
                root_document_path=catia_root_document_path,
                dimension_document_id=finalized_output.get("document_id"),
                dimension_limit=catia_dimension_limit,
                target_identity=catia_target_identity,
                summary_json=catia_summary_json,
                tolerance_mode=catia_tolerance_mode,
                approved_only=catia_approved_only,
                progress_callback=progress_callback,
            )
        return finalized_output

    if progress_callback:
        progress_callback("Hybrid Model Agent - Primary pass: {}".format(primary_model))
    primary_output = run_single_model_pass(primary_model)
    fallback_assessment = assess_fallback_need(
        primary_output["result"],
        min_extracted_dimensions=fallback_min_extracted_dimensions,
        min_green_box_dimensions=fallback_min_green_box_dimensions,
        min_green_box_ratio=fallback_min_green_box_ratio,
        max_bbox_mismatches=fallback_max_bbox_mismatches,
    )
    primary_accepted = not fallback_assessment["should_fallback"] or fallback_model == primary_model
    attempts = [
        build_model_attempt_summary(
            primary_model,
            primary_output["result"],
            fallback_assessment=fallback_assessment,
            accepted=primary_accepted,
        )
    ]

    if primary_accepted:
        selected_output = primary_output
        fallback_triggered = False
        selected_model = primary_model
    else:
        fallback_triggered = True
        if progress_callback:
            progress_callback(
                "Hybrid Model Agent - Retrying with {} because {}".format(
                    fallback_model,
                    "; ".join(fallback_assessment["reasons"]),
                )
            )
        selected_output = run_single_model_pass(fallback_model)
        selected_model = fallback_model
        attempts.append(
            build_model_attempt_summary(
                fallback_model,
                selected_output["result"],
                fallback_assessment={"should_fallback": False, "reasons": ["selected as fallback model"], "metrics": {}},
                accepted=True,
            )
        )

    selected_output["model_strategy"] = "hybrid"
    selected_output["fallback_triggered"] = fallback_triggered
    selected_output["fallback_assessment"] = fallback_assessment
    selected_output["model_attempts"] = attempts
    selected_output["result"]["model_selection"] = {
        "strategy": "hybrid",
        "selected_model": selected_model,
        "fallback_model": fallback_model,
        "primary_model": primary_model,
        "fallback_triggered": fallback_triggered,
        "fallback_assessment": fallback_assessment,
        "attempts": attempts,
    }
    finalized_output = finalize_pipeline_output(selected_output, progress_callback=progress_callback)
    if sync_catia_length_parameters:
        finalized_output["catia_parameter_sync"] = run_catia_parameter_sync(
            db_path=str(finalized_output["db_path"]),
            root_document_path=catia_root_document_path,
            dimension_document_id=finalized_output.get("document_id"),
            dimension_limit=catia_dimension_limit,
            target_identity=catia_target_identity,
            summary_json=catia_summary_json,
            tolerance_mode=catia_tolerance_mode,
            approved_only=catia_approved_only,
            progress_callback=progress_callback,
        )
    return finalized_output


# Backward-compatible aliases for existing integrations.
run_openai_extraction_stage = run_layer1_2d_extraction_stage
_run_drawing_pipeline_core = _run_layer1_2d_pipeline_core
run_drawing_pipeline = run_layer1_2d_pipeline


def main():
    args = parse_args()
    output = run_layer1_2d_pipeline(
        image_path=args.image,
        document_id=args.document_id,
        drawing_role=args.drawing_role,
        drawing_name=args.drawing_name,
        model=args.model,
        model_strategy=choose_model_strategy(args),
        primary_model=args.primary_model,
        result_folder=args.result_folder,
        db_path=args.db,
        api_key=args.openai_api_key,
        prompt_file=args.prompt_file,
        use_multi_pass_extraction=choose_multi_pass_extraction(args),
        pdf_page=args.pdf_page,
        pdf_dpi=args.pdf_dpi,
        enable_crop_preprocessing=choose_crop_preprocessing(args),
        enable_value_verification=choose_value_verification(args),
        enable_bbox_refinement=args.enable_bbox_refinement and not args.disable_bbox_refinement,
        bbox_refiner_glm_model=args.bbox_refiner_glm_model,
        bbox_refiner_max_new_tokens=args.bbox_refiner_max_new_tokens,
        fallback_min_extracted_dimensions=args.fallback_min_extracted_dimensions,
        fallback_min_green_box_dimensions=args.fallback_min_green_box_dimensions,
        fallback_min_green_box_ratio=args.fallback_min_green_box_ratio,
        fallback_max_bbox_mismatches=args.fallback_max_bbox_mismatches,
    )
    json_path = output["json_path"]
    summary_path = output["summary_path"]
    annotated_path = output["annotated_path"]
    print("Stage 02/07 - OpenAI Extraction Agent completed")
    print("Stage 03/07 - Box Assignment Agent completed")
    print("Stage 04/07 - Dimension Verification Agent completed")
    print("Stage 05/07 - Value Verification Agent completed")
    print("Stage 06/07 - Output Writer Agent completed")
    print("Saved extraction JSON to: {}".format(json_path))
    print("Saved summary to: {}".format(summary_path))
    print("Saved annotated image to: {}".format(annotated_path))
    print("Saved prepared image to: {}".format(output["prepared_image_path"]))
    print("Selected model: {} (strategy: {})".format(output["model_used"], output.get("model_strategy", "direct")))
    if output.get("fallback_triggered"):
        print("Hybrid fallback reasons: {}".format("; ".join(output.get("fallback_assessment", {}).get("reasons", []))))
    print(
        "Verified green-box dimensions: {}".format(
            output["result"].get("dimension_verification", {}).get("green_box_dimension_count", 0)
        )
    )
    print(
        "Verified dimension values: {} | Correctly located: {} | BBox mismatch: {}".format(
            output["result"].get("verified_value_count", 0),
            output["result"].get("verified_location_count", 0),
            output["result"].get("bbox_mismatch_count", 0),
        )
    )
    print("Stage 07/07 - SQLite Import Agent completed")
    print("Imported result into database: {}".format(output["db_path"]))


if __name__ == "__main__":
    main()
