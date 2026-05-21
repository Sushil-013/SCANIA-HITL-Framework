import argparse
import sys
from pathlib import Path

try:
    from .drawing_pipeline import run_drawing_pipeline
    from .human_dimension_review import run_human_dimension_review
except ImportError:
    from openai_pipeline.pipeline_stack.main_pipelines.drawing_pipeline import run_drawing_pipeline
    from openai_pipeline.pipeline_stack.main_pipelines.human_dimension_review import run_human_dimension_review


DEFAULT_RESULT_FOLDER = "Results/drawing_pipeline"
DEFAULT_PRIMARY_MODEL = "gpt-5.4-mini"
DEFAULT_FALLBACK_MODEL = "gpt-5.5"
MAX_AUTO_RERUNS = 3


def print_progress(message):
    print(message)


def _mode_default_label(default_value, label_map):
    return label_map.get(default_value, default_value)


def prompt_for_image_path(current_value):
    if current_value:
        return current_value
    if not sys.stdin.isatty():
        raise RuntimeError("Missing --image. Pass a drawing image/PDF path when running non-interactively.")

    while True:
        print("")
        try:
            candidate = input("Drawing image or PDF path: ").strip().strip('"')
        except EOFError:
            raise RuntimeError("Missing --image. Pass a drawing image/PDF path when running non-interactively.")
        if not candidate:
            print("Please enter an image or PDF path.")
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return path
        print("Path not found: {}".format(path))


def choose_model_strategy(args, force_prompt=False, default_value="hybrid"):
    if not force_prompt and args.model_strategy == "direct":
        return "direct"
    if not force_prompt and args.model_strategy == "hybrid":
        return "hybrid"
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("Model strategy:")
    print("  1. Hybrid (recommended) - try {} first, then fall back to {}".format(args.primary_model, args.model))
    print("  2. Direct - use {} immediately".format(args.model))
    default_label = _mode_default_label(default_value, {"hybrid": "hybrid", "direct": "direct"})
    while True:
        try:
            choice = input(
                "Choose model strategy [1/hybrid or 2/direct, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "h", "hybrid", "auto"):
            return "hybrid"
        if choice in ("2", "d", "direct"):
            return "direct"
        print("Please enter 1/hybrid or 2/direct.")


def choose_multi_pass_extraction(args, force_prompt=False, default_value=True):
    if not force_prompt and args.extraction_mode == "single":
        return False
    if not force_prompt and args.extraction_mode == "multi":
        return True
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("OpenAI extraction mode:")
    print("  1. Single-pass (faster)")
    print("  2. Multi-pass (better structure, dimensions, and GD&T separation)")
    default_label = "multi" if default_value else "single"
    while True:
        try:
            choice = input(
                "Choose extraction mode [1/single or 2/multi, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "single", "single-pass", "single pass"):
            return False
        if choice in ("2", "m", "multi", "multi-pass", "multi pass"):
            return True
        print("Please enter 1/single or 2/multi.")


def choose_crop_preprocessing(args, force_prompt=False, default_value="dynamic"):
    if not force_prompt and args.crop_preprocessing_mode in ("skip", "dynamic", "red_boxes"):
        return args.crop_preprocessing_mode
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("Crop preprocessing:")
    print("  1. Skip")
    print("  2. Dynamic crops for notes, title/BOM, and drawing views")
    print("  3. Red markup box crops")
    default_label = _mode_default_label(
        default_value,
        {"skip": "skip", "dynamic": "dynamic", "red_boxes": "red_boxes"},
    )
    while True:
        try:
            choice = input(
                "Choose crop preprocessing [1/skip, 2/dynamic, 3/red_boxes, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return "skip"
        if choice in ("2", "d", "dynamic", "yes", "y"):
            return "dynamic"
        if choice in ("3", "r", "red", "red_boxes", "red-boxes", "boxes"):
            return "red_boxes"
        print("Please enter 1/skip, 2/dynamic, or 3/red_boxes.")


def choose_value_verification(args, force_prompt=False, default_value=True):
    if not force_prompt and args.value_verification_mode == "skip":
        return False
    if not force_prompt and args.value_verification_mode == "local":
        return True
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("Value verification:")
    print("  1. Skip local OCR verification")
    print("  2. Run local OCR verification")
    default_label = "local" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run local value verification? [1/skip or 2/local, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "l", "local", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/local.")


def choose_bbox_refinement(args, force_prompt=False, default_value=True):
    if not force_prompt and args.bbox_refinement_mode == "skip":
        return False
    if not force_prompt and args.bbox_refinement_mode == "local":
        return True
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("BBox refinement:")
    print("  1. Skip local OCR bbox refinement")
    print("  2. Run local OCR bbox refinement")
    default_label = "local" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run bbox refinement? [1/skip or 2/local, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "l", "local", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/local.")


def choose_human_review_mode(args, force_prompt=False, default_value=True):
    if not force_prompt and args.human_review_mode == "coverage":
        return True
    if not force_prompt and args.human_review_mode == "skip":
        return False
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("Human dimension review:")
    print("  1. Run coverage review before accepting dimensions (recommended)")
    print("  2. Skip")
    default_label = "coverage" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run human dimension review? [1/coverage or 2/skip, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "c", "coverage", "yes", "y"):
            return True
        if choice in ("2", "s", "skip", "no", "n"):
            return False
        print("Please enter 1/coverage or 2/skip.")


def choose_catia_published_parameter_mode(args, force_prompt=False, default_value=False):
    if not force_prompt and args.catia_published_parameters_mode == "extract":
        return True
    if not force_prompt and args.catia_published_parameters_mode == "skip":
        return False
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("CATIA published-parameter extraction:")
    print("  1. Skip")
    print("  2. Extract published parameters from the matching CATIA part after human review")
    default_label = "extract" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run CATIA published-parameter extraction? [1/skip or 2/extract, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "e", "extract", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/extract.")


def choose_catia_dimension_link_mode(args, force_prompt=False, default_value=True):
    if not force_prompt and args.catia_dimension_link_mode == "link":
        return True
    if not force_prompt and args.catia_dimension_link_mode == "skip":
        return False
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("CATIA dimension link UI:")
    print("  1. Skip")
    print("  2. Open the 2D-to-CATIA linking UI after published-parameter extraction")
    default_label = "link" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run CATIA dimension link UI? [1/skip or 2/link, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "l", "link", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/link.")


def choose_catia_layer3_mode(args, force_prompt=False, default_value=True):
    if not force_prompt and args.catia_layer3_mode == "run":
        return True
    if not force_prompt and args.catia_layer3_mode == "skip":
        return False
    if not sys.stdin.isatty():
        return default_value

    print("")
    print("CATIA Layer 3 - Tolerance DMU Sweep:")
    print("  1. Skip")
    print("  2. Run nominal + per-dimension max/min DMU after Layer 2")
    default_label = "run" if default_value else "skip"
    while True:
        try:
            choice = input(
                "Run CATIA Layer 3? [1/skip or 2/run, default {}]: ".format(default_label)
            ).strip().lower()
        except EOFError:
            return default_value
        if choice == "":
            return default_value
        if choice in ("1", "s", "skip", "no", "n"):
            return False
        if choice in ("2", "r", "run", "yes", "y"):
            return True
        print("Please enter 1/skip or 2/run.")


def resolve_post_review_action(post_review_actions, key, fallback_value):
    if not isinstance(post_review_actions, dict):
        return fallback_value
    if key not in post_review_actions:
        return fallback_value
    return bool(post_review_actions.get(key))


def confirm_human_review_rerun(auto_rerun_count, max_auto_reruns):
    print("")
    print("Human review requested a full pipeline rerun.")
    print("The terminal will reopen the pipeline options so you can choose the rerun settings.")
    if not sys.stdin.isatty():
        return False
    while True:
        try:
            choice = input(
                "Rerun the full pipeline now? [y=yes / n=no, default yes]: "
            ).strip().lower()
        except EOFError:
            return False
        if choice in ("", "y", "yes"):
            if auto_rerun_count >= max_auto_reruns:
                print("Maximum rerun limit reached.")
                return False
            return True
        if choice in ("n", "no"):
            return False
        print("Please enter y/yes or n/no.")


def resolve_runtime_options(args, force_prompt=False, defaults=None):
    defaults = defaults or {}
    return {
        "model_strategy": choose_model_strategy(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("model_strategy", "hybrid"),
        ),
        "use_multi_pass_extraction": choose_multi_pass_extraction(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("use_multi_pass_extraction", True),
        ),
        "enable_crop_preprocessing": choose_crop_preprocessing(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("enable_crop_preprocessing", "dynamic"),
        ),
        "enable_value_verification": choose_value_verification(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("enable_value_verification", True),
        ),
        "enable_bbox_refinement": choose_bbox_refinement(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("enable_bbox_refinement", True),
        ),
        "run_human_review": choose_human_review_mode(
            args,
            force_prompt=force_prompt,
            default_value=defaults.get("run_human_review", True),
        ),
    }


def resolve_catia_target_part_number(args, output):
    explicit_value = str(args.catia_published_part_number or "").strip()
    if explicit_value:
        return explicit_value

    inferred_value = (
        str(output.get("result", {}).get("part_number") or "").strip()
        or str(output.get("result", {}).get("document_number") or "").strip()
        or str(output.get("document_id") or "").strip()
    )
    if inferred_value:
        return inferred_value

    if not sys.stdin.isatty():
        return None

    print("")
    try:
        candidate = input("CATIA target part number: ").strip()
    except EOFError:
        return None
    return candidate or None


def resolve_catia_layer3_part_number(args, output, fallback_part_number=None):
    explicit_value = str(args.catia_layer3_part_number or "").strip()
    if explicit_value:
        return explicit_value
    if fallback_part_number:
        return str(fallback_part_number).strip() or None
    return resolve_catia_target_part_number(args, output)


def run_catia_published_parameter_capture(args, output, target_part_number):
    if not target_part_number:
        return {
            "status": "skipped",
            "reason": "no CATIA target part number was provided or inferred",
            "table_name": "catia_published_parameters",
        }

    try:
        from ..catia_agents.extract_published_parameters_agent import extract_published_parameters_to_sqlite
    except ImportError as exc:
        try:
            from openai_pipeline.pipeline_stack.catia_agents.extract_published_parameters_agent import extract_published_parameters_to_sqlite
        except ImportError:
            return {
                "status": "failed",
                "reason": "could not import CATIA published-parameter agent",
                "error": str(exc),
                "table_name": "catia_published_parameters",
                "target_part_number": target_part_number,
            }

    def _run_once():
        return extract_published_parameters_to_sqlite(
            db_path=str(output["db_path"]),
            target_part_number=target_part_number,
            drawing_document_id=output.get("document_id"),
            drawing_part_number=output.get("result", {}).get("part_number"),
            root_document_path=args.catia_root_document,
            summary_json=args.catia_published_summary_json,
            progress_callback=print_progress,
        )

    def _looks_like_missing_root_document(error_text):
        text = str(error_text or "").lower()
        return (
            "no active catia catproduct/catpart document" in text
            or "active/open documents are not catproduct/catpart" in text
        )

    try:
        return _run_once()
    except Exception as exc:
        if not args.catia_root_document and _looks_like_missing_root_document(str(exc)):
            print_progress(
                "Layer 2 recovery - automatic CATIA root document detection did not succeed"
            )
        return {
            "status": "failed",
            "reason": "CATIA published-parameter extraction failed",
            "error": str(exc),
            "table_name": "catia_published_parameters",
            "target_part_number": target_part_number,
        }


def run_catia_dimension_link_ui(args, output, target_part_number):
    try:
        from ..catia_agents.dimension_link_ui_agent import run_dimension_link_ui_from_sqlite
    except ImportError as exc:
        try:
            from openai_pipeline.pipeline_stack.catia_agents.dimension_link_ui_agent import run_dimension_link_ui_from_sqlite
        except ImportError:
            return {
                "status": "failed",
                "reason": "could not import CATIA dimension link UI agent",
                "error": str(exc),
                "table_name": "catia_dimension_links",
                "target_part_number": target_part_number,
            }

    try:
        return run_dimension_link_ui_from_sqlite(
            db_path=str(output["db_path"]),
            document_id=output.get("document_id"),
            catia_part_number=target_part_number,
            summary_json=args.catia_dimension_link_summary_json,
            progress_callback=print_progress,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "CATIA dimension link UI failed",
            "error": str(exc),
            "table_name": "catia_dimension_links",
            "target_part_number": target_part_number,
        }


def run_catia_layer3_nominal(args, output, target_part_number):
    try:
        from ..catia_agents.layer3_tolerance_dmu_agent import run_layer3_tolerance_sweep_to_sqlite
    except ImportError as exc:
        try:
            from openai_pipeline.pipeline_stack.catia_agents.layer3_tolerance_dmu_agent import run_layer3_tolerance_sweep_to_sqlite
        except ImportError:
            return {
                "status": "failed",
                "reason": "could not import CATIA Layer 3 tolerance DMU agent",
                "error": str(exc),
                "table_name": "catia_dmu_results_nominal",
                "runs_table_name": "catia_dmu_runs_nominal",
                "target_part_number": target_part_number,
            }

    try:
        return run_layer3_tolerance_sweep_to_sqlite(
            db_path=str(output["db_path"]),
            document_id=output.get("document_id"),
            drawing_part_number=output.get("result", {}).get("part_number"),
            target_part_number=target_part_number,
            root_document_path=args.catia_root_document,
            clearance_mm=args.dmu_clearance_mm,
            output_root=args.catia_layer3_output_root,
            summary_json=args.catia_layer3_summary_json,
            progress_callback=print_progress,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "CATIA Layer 3 tolerance DMU failed",
            "error": str(exc),
            "table_name": "catia_dmu_results_nominal",
            "runs_table_name": "catia_dmu_runs_nominal",
            "target_part_number": target_part_number,
        }


def run_pipeline_attempt(args, image_path, runtime_options):
    return run_drawing_pipeline(
        image_path=str(image_path),
        document_id=args.document_id,
        drawing_role=args.drawing_role,
        drawing_name=args.drawing_name,
        model=args.model,
        model_strategy=runtime_options["model_strategy"],
        primary_model=args.primary_model,
        result_folder=args.result_folder,
        db_path=args.db,
        api_key=args.openai_api_key,
        prompt_file=args.prompt_file,
        use_multi_pass_extraction=runtime_options["use_multi_pass_extraction"],
        pdf_page=args.pdf_page,
        pdf_dpi=args.pdf_dpi,
        enable_crop_preprocessing=runtime_options["enable_crop_preprocessing"],
        enable_value_verification=runtime_options["enable_value_verification"],
        enable_bbox_refinement=runtime_options["enable_bbox_refinement"],
        bbox_refiner_glm_model=args.bbox_refiner_glm_model,
        bbox_refiner_max_new_tokens=args.bbox_refiner_max_new_tokens,
        output_name=args.output_name,
        progress_callback=print_progress,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Single interactive entrypoint for the OpenAI drawing pipeline.")
    parser.add_argument("--image", default=None, type=Path, help="Source drawing image or PDF. If omitted, the terminal prompts for it.")
    parser.add_argument("--document_id", default=None, type=str, help="Optional stable document id.")
    parser.add_argument("--drawing_role", default="part", type=str, help="Drawing role such as part or assembly.")
    parser.add_argument("--drawing_name", default=None, type=str, help="Optional human-readable drawing name.")
    parser.add_argument("--model", default=DEFAULT_FALLBACK_MODEL, type=str, help="Main OpenAI extraction model.")
    parser.add_argument(
        "--model_strategy",
        choices=("ask", "direct", "hybrid"),
        default="hybrid",
        help="Model strategy. Defaults to hybrid.",
    )
    parser.add_argument("--primary_model", default=DEFAULT_PRIMARY_MODEL, type=str, help="Primary model for hybrid mode.")
    parser.add_argument("--result_folder", default=DEFAULT_RESULT_FOLDER, type=str, help="Base folder for outputs.")
    parser.add_argument("--db", default=None, type=str, help="Optional SQLite DB path override.")
    parser.add_argument("--openai_api_key", default=None, type=str, help="Optional OpenAI API key override.")
    parser.add_argument("--prompt_file", default=None, type=str, help="Optional custom prompt template file.")
    parser.add_argument(
        "--extraction_mode",
        choices=("ask", "single", "multi"),
        default="multi",
        help="OpenAI extraction mode. Defaults to multi.",
    )
    parser.add_argument("--pdf_page", default=1, type=int, help="1-based PDF page number.")
    parser.add_argument("--pdf_dpi", default=450, type=int, help="PDF render DPI.")
    parser.add_argument(
        "--crop_preprocessing_mode",
        choices=("ask", "skip", "dynamic", "red_boxes"),
        default="dynamic",
        help="Crop preprocessing mode. Defaults to dynamic.",
    )
    parser.add_argument(
        "--value_verification_mode",
        choices=("ask", "skip", "local"),
        default="local",
        help="Value verification mode. Defaults to local.",
    )
    parser.add_argument(
        "--bbox_refinement_mode",
        choices=("ask", "skip", "local"),
        default="local",
        help="BBox refinement mode. Defaults to local.",
    )
    parser.add_argument(
        "--human_review_mode",
        choices=("ask", "skip", "coverage"),
        default="coverage",
        help="Human dimension review mode. Defaults to coverage review.",
    )
    parser.add_argument(
        "--expected_dimension_count",
        default=None,
        type=int,
        help="Optional expected total dimension count used during coverage review.",
    )
    parser.add_argument(
        "--catia_published_parameters_mode",
        choices=("ask", "skip", "extract"),
        default="extract",
        help="Run CATIA published-parameter extraction after human review. Defaults to extract.",
    )
    parser.add_argument(
        "--catia_published_part_number",
        default=None,
        type=str,
        help="Optional CATIA target part number override for published-parameter extraction.",
    )
    parser.add_argument(
        "--catia_root_document",
        default=None,
        type=str,
        help="Optional CATIA CATProduct/CATPart path override. Defaults to CATIA ActiveDocument.",
    )
    parser.add_argument(
        "--catia_published_summary_json",
        default=None,
        type=str,
        help="Optional JSON output path for the CATIA published-parameter extraction summary.",
    )
    parser.add_argument(
        "--catia_dimension_link_mode",
        choices=("ask", "skip", "link"),
        default="link",
        help="Run the CATIA dimension-to-published-parameter linking UI after extraction. Defaults to link.",
    )
    parser.add_argument(
        "--catia_dimension_link_summary_json",
        default=None,
        type=str,
        help="Optional JSON output path for the CATIA dimension-link UI summary.",
    )
    parser.add_argument(
        "--catia_layer3_mode",
        choices=("ask", "skip", "run"),
        default="ask",
        help="Run the CATIA nominal DMU layer after Layer 2. Defaults to ask.",
    )
    parser.add_argument(
        "--catia_layer3_part_number",
        default=None,
        type=str,
        help="Optional CATIA target part number override for Layer 3 selection-against-all.",
    )
    parser.add_argument(
        "--dmu_clearance_mm",
        default=5.0,
        type=float,
        help="CATIA DMU clearance threshold in millimeters for Layer 3.",
    )
    parser.add_argument(
        "--catia_layer3_summary_json",
        default=None,
        type=str,
        help="Optional JSON output path for the CATIA Layer 3 nominal DMU summary.",
    )
    parser.add_argument(
        "--catia_layer3_output_root",
        default="Results/layer3_dmu",
        type=str,
        help="Base output folder for Layer 3 nominal/max/min DMU results.",
    )
    parser.add_argument("--bbox_refiner_glm_model", default="zai-org/GLM-OCR", type=str, help="Local OCR model name.")
    parser.add_argument("--bbox_refiner_max_new_tokens", default=512, type=int, help="Max decode tokens.")
    parser.add_argument("--output_name", default=None, type=str, help="Optional output folder/file stem override.")
    return parser.parse_args()


def print_run_summary(output):
    result = output["result"]
    print("")
    print("Pipeline completed.")
    print("JSON output: {}".format(output["json_path"]))
    print("Annotated image: {}".format(output["annotated_path"]))
    print("Prepared image: {}".format(output["prepared_image_path"]))
    print("Database: {}".format(output["db_path"]))
    print("Selected model: {} (strategy: {})".format(output["model_used"], output.get("model_strategy", "direct")))
    if output.get("fallback_triggered"):
        print("Fallback reasons: {}".format("; ".join(output.get("fallback_assessment", {}).get("reasons", []))))
    print("Green-box dimensions: {}".format(result.get("dimension_count", 0)))
    print("Extracted dimensions: {}".format(result.get("extracted_dimension_count", 0)))
    print("Verified values: {}".format(result.get("verified_value_count", 0)))
    print("Verified locations: {}".format(result.get("verified_location_count", 0)))
    print("BBox mismatches: {}".format(result.get("bbox_mismatch_count", 0)))
    human_review = output.get("human_dimension_review")
    if human_review:
        print(
            "Human review: {} | approved={} | rejected={} | pending={}".format(
                human_review.get("status", "unknown"),
                human_review.get("approved_count", 0),
                human_review.get("rejected_count", 0),
                human_review.get("pending_count", 0),
            )
        )
        if human_review.get("manual_dimensions_added") is not None:
            print("Manual dimensions added: {}".format(human_review.get("manual_dimensions_added")))
        dimension_log_lines = human_review.get("dimension_log_lines") or []
        if dimension_log_lines:
            print("Reviewed dimensions:")
            for line in dimension_log_lines:
                print("  {}".format(line))
        if human_review.get("rerun_suggestion"):
            print("Suggested rerun: {}".format(human_review.get("rerun_suggestion")))
    if output.get("auto_rerun_count") is not None:
        print("Auto reruns: {}".format(output.get("auto_rerun_count", 0)))
    catia_published_parameters = output.get("catia_published_parameters")
    if catia_published_parameters:
        print(
            "CATIA published parameters: {} | target={} | extracted={}".format(
                catia_published_parameters.get("status", "unknown"),
                catia_published_parameters.get("target_part_number", "n/a"),
                catia_published_parameters.get("published_parameter_count", 0),
            )
        )
        if catia_published_parameters.get("error"):
            print("CATIA published-parameter error: {}".format(catia_published_parameters.get("error")))
    catia_dimension_links = output.get("catia_dimension_links")
    if catia_dimension_links:
        print(
            "CATIA dimension links: {} | linked={}/{}".format(
                catia_dimension_links.get("status", "unknown"),
                catia_dimension_links.get("linked_dimensions", 0),
                catia_dimension_links.get("total_dimensions", 0),
            )
        )
        if catia_dimension_links.get("paired_table_name"):
            print(
                "CATIA paired table: {} | paired_rows={}".format(
                    catia_dimension_links.get("paired_table_name"),
                    catia_dimension_links.get("paired_dimensions", 0),
                )
            )
        if catia_dimension_links.get("all_dimensions_linked"):
            print("CATIA dimension links are complete for all extracted 2D dimensions.")
        if catia_dimension_links.get("error"):
            print("CATIA dimension-link error: {}".format(catia_dimension_links.get("error")))
    catia_layer3 = output.get("catia_layer3")
    if catia_layer3:
        if catia_layer3.get("status") == "completed":
            print(
                "CATIA Layer 3: {} | nominal_rows={} | max_runs={} | max_dmu_rows={} | min_runs={} | min_dmu_rows={}".format(
                    catia_layer3.get("status", "unknown"),
                    catia_layer3.get("nominal_rows", 0),
                    catia_layer3.get("max_runs", 0),
                    catia_layer3.get("max_dmu_rows", 0),
                    catia_layer3.get("min_runs", 0),
                    catia_layer3.get("min_dmu_rows", 0),
                )
            )
            nominal_tables = catia_layer3.get("nominal_tables") or {}
            print(
                "CATIA Layer 3 nominal tables: {} + {}".format(
                    nominal_tables.get("runs", "catia_dmu_runs_nominal"),
                    nominal_tables.get("results", "catia_dmu_results_nominal"),
                )
            )
            if catia_layer3.get("nominal_results_txt_path"):
                print("CATIA Layer 3 nominal txt: {}".format(catia_layer3.get("nominal_results_txt_path")))
            if catia_layer3.get("layer3_output_root"):
                print("CATIA Layer 3 result root: {}".format(catia_layer3.get("layer3_output_root")))
        else:
            print("CATIA Layer 3: {}".format(catia_layer3.get("status", "unknown")))
        if catia_layer3.get("error"):
            print("CATIA Layer 3 error: {}".format(catia_layer3.get("error")))
def main():
    args = parse_args()
    image_path = prompt_for_image_path(args.image).resolve()
    runtime_options = resolve_runtime_options(args)
    auto_rerun_count = 0

    while True:
        output = run_pipeline_attempt(args, image_path, runtime_options)
        if runtime_options.get("run_human_review"):
            print_progress(
                "Layer 1 Stage 08/08 - Human Dimension Review: checking extraction coverage, approving/rejecting dimensions, and allowing manual additions"
            )
            output["human_dimension_review"] = run_human_dimension_review(
                result=output["result"],
                db_path=str(output["db_path"]),
                prepared_image_path=str(output["prepared_image_path"]),
                document_id=output["document_id"],
                review_scope="all",
                expected_dimension_count=args.expected_dimension_count,
                annotated_path=str(output["annotated_path"]),
                progress_callback=print_progress,
            )
            if output["human_dimension_review"].get("status") == "rerun_requested":
                output["human_dimension_review"]["rerun_suggestion"] = (
                    "Rerun can reopen the full pipeline options in the terminal so you can choose new settings."
                )
                if confirm_human_review_rerun(auto_rerun_count, MAX_AUTO_RERUNS):
                    auto_rerun_count += 1
                    print_progress(
                        "Rerun {}/{} - reopening pipeline options after human review requested a rerun".format(
                            auto_rerun_count,
                            MAX_AUTO_RERUNS,
                        )
                    )
                    runtime_options = resolve_runtime_options(
                        args,
                        force_prompt=True,
                        defaults=runtime_options,
                    )
                    continue
            print_progress(
                "Layer 1 Completed 08/08 - Human review finished with status {}".format(
                    output["human_dimension_review"].get("status", "unknown")
                )
            )
            post_review_actions = output["human_dimension_review"].get("post_review_actions") or {}
            run_catia_published_parameters = resolve_post_review_action(
                post_review_actions,
                "run_layer2_extract",
                choose_catia_published_parameter_mode(args),
            )
            run_catia_link_ui = resolve_post_review_action(
                post_review_actions,
                "run_layer2_link",
                choose_catia_dimension_link_mode(args),
            )
            run_catia_layer3 = resolve_post_review_action(
                post_review_actions,
                "run_layer3",
                choose_catia_layer3_mode(args),
            )

            if run_catia_published_parameters:
                target_part_number = resolve_catia_target_part_number(args, output)
                output["catia_published_parameters"] = run_catia_published_parameter_capture(
                    args,
                    output,
                    target_part_number,
                )
                if output["catia_published_parameters"].get("status") == "completed":
                    print_progress(
                        "Layer 2 Result - Published Parameters: target={} | extracted={} | table={}".format(
                            output["catia_published_parameters"].get("target_part_number", "n/a"),
                            output["catia_published_parameters"].get("published_parameter_count", 0),
                            output["catia_published_parameters"].get("table_name", "catia_published_parameters"),
                        )
                    )
                published_status = output["catia_published_parameters"].get("status")
                if published_status != "completed":
                    published_reason = (
                        output["catia_published_parameters"].get("error")
                        or output["catia_published_parameters"].get("reason")
                        or "unknown reason"
                    )
                    print_progress(
                        "Layer 2 Stage 01/03 - CATIA Published Parameter Extraction {}: {}".format(
                            published_status or "failed",
                            published_reason,
                        )
                    )
                    print_progress("Layer 2 Stage 02/03 - CATIA Published Parameter Output Writer skipped")
                if (
                    run_catia_link_ui
                    and output["catia_published_parameters"].get("status") == "completed"
                ):
                    output["catia_dimension_links"] = run_catia_dimension_link_ui(
                        args,
                        output,
                        output["catia_published_parameters"].get("target_part_number") or target_part_number,
                    )
                    if output["catia_dimension_links"].get("status") == "completed":
                        print_progress(
                            "Layer 2 Result - Dimension Linking: linked={}/{} | paired_rows={} | table={} | paired_table={}".format(
                                output["catia_dimension_links"].get("linked_dimensions", 0),
                                output["catia_dimension_links"].get("total_dimensions", 0),
                                output["catia_dimension_links"].get("paired_dimensions", 0),
                                output["catia_dimension_links"].get("table_name", "catia_dimension_links"),
                                output["catia_dimension_links"].get("paired_table_name", "catia_dimension_pairs"),
                            )
                        )
                elif run_catia_link_ui:
                    output["catia_dimension_links"] = {
                        "status": "skipped",
                        "reason": "CATIA dimension link UI did not run because published-parameter extraction did not complete",
                        "error": (
                            output["catia_published_parameters"].get("error")
                            or output["catia_published_parameters"].get("reason")
                        ),
                        "table_name": "catia_dimension_links",
                    }
                    print_progress(
                        "Layer 2 Stage 03/03 - CATIA Dimension Link UI skipped: {}".format(
                            output["catia_dimension_links"].get("error")
                            or output["catia_dimension_links"].get("reason")
                            or "published-parameter extraction did not complete"
                        )
                    )
                    print_progress("Layer 2 Completed 03/03 - CATIA post-run finished with skipped link UI")
                elif output["catia_published_parameters"].get("status") == "completed":
                    print_progress("Layer 2 Stage 03/03 - CATIA Dimension Link UI skipped")
                    print_progress("Layer 2 Completed 03/03 - CATIA post-run finished without manual linking")
        elif choose_catia_published_parameter_mode(args):
            output["catia_published_parameters"] = {
                "status": "skipped",
                "reason": "human review is disabled, so CATIA published-parameter extraction did not run",
                "table_name": "catia_published_parameters",
            }
            if choose_catia_dimension_link_mode(args):
                output["catia_dimension_links"] = {
                    "status": "skipped",
                    "reason": "CATIA dimension link UI did not run because published-parameter extraction was skipped",
                    "table_name": "catia_dimension_links",
                }
        if runtime_options.get("run_human_review"):
            run_catia_layer3_mode = run_catia_layer3
        else:
            run_catia_layer3_mode = choose_catia_layer3_mode(args)
        if run_catia_layer3_mode:
            fallback_part_number = None
            if output.get("catia_published_parameters"):
                fallback_part_number = output["catia_published_parameters"].get("target_part_number")
            target_part_number = resolve_catia_layer3_part_number(args, output, fallback_part_number=fallback_part_number)
            output["catia_layer3"] = run_catia_layer3_nominal(
                args,
                output,
                target_part_number,
            )
            if output["catia_layer3"].get("status") == "completed":
                print_progress(
                    "Layer 3 Result - Sweep Summary: nominal_rows={} | max_runs={} | max_rows={} | min_runs={} | min_rows={}".format(
                        output["catia_layer3"].get("nominal_rows", 0),
                        output["catia_layer3"].get("max_runs", 0),
                        output["catia_layer3"].get("max_dmu_rows", 0),
                        output["catia_layer3"].get("min_runs", 0),
                        output["catia_layer3"].get("min_dmu_rows", 0),
                    )
                )
        else:
            output["catia_layer3"] = {
                "status": "skipped",
                "reason": "CATIA Layer 3 was skipped",
                "runs_table_name": "catia_dmu_runs_nominal",
                "table_name": "catia_dmu_results_nominal",
            }
        output["auto_rerun_count"] = auto_rerun_count
        break
    print_run_summary(output)


if __name__ == "__main__":
    main()
