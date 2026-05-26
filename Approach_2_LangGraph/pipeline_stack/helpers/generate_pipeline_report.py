from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont


REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = REPO_ROOT / "Results" / "pipeline_docs"
MARKDOWN_PATH = OUTPUT_DIR / "current_pipeline_architecture_report.md"
FLOWCHART_PATH = OUTPUT_DIR / "current_pipeline_flowchart.png"
PDF_PATH = OUTPUT_DIR / "current_pipeline_architecture_report.pdf"


def build_report_markdown() -> str:
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M")
    command_text = r'.\.venv\Scripts\python.exe ".\openai_pipeline\5_pipeline.py" --image "...pdf" --pdf_dpi 450'
    return textwrap.dedent(
        f"""
        # Current Pipeline Architecture Report

        Generated on: {generated_on}

        ## Scope

        This report describes the live runtime under `openai_pipeline/pipeline_stack/`.
        It covers the end-to-end drawing-to-CATIA workflow that currently runs from:

        `{command_text}`

        Default output root:

        - `Results/drawing_pipeline/`

        ## Executive Summary

        The current solution is a 3-layer pipeline:

        1. Layer 1: 2D drawing extraction, local refinement, verification, output writing, SQLite import, and human review.
        2. Layer 2: CATIA published-parameter extraction and manual 2D-to-CATIA mapping.
        3. Layer 3: per-parameter CATIA tolerance actuation and DMU clash/contact/clearance analysis.

        The runtime uses:

        - 3 main orchestration modules:
          - `main_pipelines/interactive_pipeline.py`
          - `main_pipelines/drawing_pipeline.py`
          - `main_pipelines/human_dimension_review.py`
        - 4 refinement modules:
          - `refinement/bbox_refiner.py`
          - `refinement/pdf_text_bbox_refiner.py`
          - `refinement/dimension_verifier.py`
          - `refinement/value_verifier.py`
        - 5 CATIA-oriented modules:
          - `catia_agents/extract_published_parameters_agent.py`
          - `catia_agents/dimension_link_ui_agent.py`
          - `catia_agents/max_tolerance_dmu_agent.py`
          - `catia_agents/DMU Agent.py`
          - `catia_agents/dmu_ui.py`
        - supporting helper modules for preprocessing, coordinate handling, overlay rendering, and SQLite persistence.

        ## Runtime Layers and Numbered Stages

        ### Layer 1

        Layer 1 has 8 numbered stages plus one conditional substage:

        1. Stage 01/08 - PDF Clarity Agent
        2. Stage 02/08 - OpenAI Extraction Agent
        3. Stage 03/08 - Local BBox Refinement Agent, or VLM Box Assignment Agent when local refinement is disabled
        4. Stage 03b/08 - PDF Text BBox Grounding Agent for PDF inputs only
        5. Stage 04/08 - Dimension Verification Agent
        6. Stage 05/08 - Value Verification Agent
        7. Stage 06/08 - Output Writer Agent
        8. Stage 07/08 - SQLite Import Agent
        9. Stage 08/08 - Human Dimension Review

        ### Layer 2

        Layer 2 has 3 numbered stages:

        1. Stage 01/03 - CATIA Published Parameter Extraction
        2. Stage 02/03 - CATIA Published Parameter Output Writer
        3. Stage 03/03 - CATIA Dimension Link UI

        ### Layer 3

        Layer 3 currently operates as an 8-slot parameter sweep workflow:

        1. Stage 01/08 - Maximum tolerance actuation sweep, one linked parameter at a time
        2. Stage 02/08 - DMU mode selection
        3. Stage 03/08 - implicit root CATIA document activation before each DMU run
        4. Stage 04/08 - DMU clash/contact/clearance execution for each maximum case
        5. Stage 05/08 - DMU preview UI for flagged results
        6. Stage 06/08 - minimum tolerance follow-up decision
        7. Stage 07/08 - minimum tolerance actuation sweep, one linked parameter at a time
        8. Stage 08/08 - completion and summary

        Note:
        The current Layer 3 implementation logs the root-document activation and DMU execution as runtime steps inside the sweep loop rather than as separate persisted stage records.

        ## Top-Level Orchestration

        Primary entry points:

        - `openai_pipeline/5_pipeline.py`
          - package entry wrapper used by the command line
        - `openai_pipeline/pipeline_stack/main_pipelines/interactive_pipeline.py`
          - top-level orchestrator across Layers 1, 2, and 3
        - `openai_pipeline/pipeline_stack/main_pipelines/drawing_pipeline.py`
          - full Layer 1 extraction pipeline

        Key orchestration functions:

        - `interactive_pipeline.main()`
          - reads CLI arguments, resolves runtime options, runs the pipeline loop, handles human-review reruns, and conditionally invokes Layers 2 and 3
        - `interactive_pipeline.run_pipeline_attempt(...)`
          - runs a single Layer 1 attempt
        - `interactive_pipeline.run_catia_published_parameter_capture(...)`
          - invokes the Layer 2 extraction module
        - `interactive_pipeline.run_catia_dimension_link_ui(...)`
          - invokes the Layer 2 mapping UI
        - `interactive_pipeline.run_catia_layer3_pipeline(...)`
          - invokes the Layer 3 tolerance + DMU module
        - `interactive_pipeline.print_run_summary(...)`
          - prints the final run summary to the terminal

        Why this orchestrator exists:

        - it keeps Layer 1 isolated from CATIA-specific work
        - it supports reruns after human review
        - it allows the same single command to drive the complete workflow

        Limitation:

        - because later layers depend on SQLite output from Layer 1, the workflow is deliberately sequential

        ## Layer 1 Detailed Breakdown

        ### Stage 01/08 - PDF Clarity Agent

        Main function:

        - `sub_agents/pdf_clarity_agent.py -> prepare_pdf_clarity_image(...)`

        What it does:

        - if the input is a PDF, renders the chosen page to a high-resolution PNG
        - creates a high-clarity prepared image used by the rest of the pipeline
        - stores both raw and enhanced PNG outputs in the run folder

        Why it is used:

        - the downstream OpenAI and OCR stages perform better on a stable rasterized image than on a raw PDF stream

        Limitation:

        - if the source PDF itself is low quality, this stage can improve clarity only up to the readable information already present

        ### Stage 02/08 - OpenAI Extraction Agent

        Main functions:

        - `drawing_pipeline.run_layer1_2d_extraction_stage(...)`
        - `drawing_pipeline.run_json_schema_request(...)`
        - `drawing_pipeline.merge_pass_results(...)`
        - `drawing_pipeline.merge_dimension_pass_results(...)`

        What it does:

        - sends the prepared drawing image to the OpenAI model
        - can run in multi-pass mode:
          - structure pass
          - dimension pass
          - dimension recovery pass
          - GD&T pass
        - repairs tolerance fields after extraction
        - assigns drawing identity fields such as `document_id`, `part_number`, and `document_number`

        Why it is used:

        - this is the core semantic extraction stage that turns drawing pixels into structured engineering data

        Limitations:

        - extraction can still miss dimensions in dense or low-contrast areas
        - it is conservative by design, so ambiguous values may be dropped rather than guessed

        ### Stage 03/08 - Local BBox Refinement Agent or VLM Box Assignment Agent

        Main functions:

        - `refinement/bbox_refiner.py -> refine_result_bboxes(...)`
        - `refinement/bbox_refiner.py -> run_craft_text_localizer(...)`
        - `refinement/bbox_refiner.py -> enable_vlm_annotation_fallback(...)`
        - `refinement/bbox_refiner.py -> count_ready_target_annotations(...)`

        What it does:

        - tries to improve or recover bounding boxes for dimensions, datums, feature control frames, and other text items
        - when local OCR refinement is enabled:
          - runs CRAFT text localization
          - uses OCR text entries to choose better boxes
          - marks refined items as `annotation_ready`
        - when local OCR refinement is disabled:
          - uses the original OpenAI/VLM boxes as the fallback annotation source
          - still marks items as usable where possible

        Why it is used:

        - later stages need a reliable bounding box to count coverage, create overlays, and verify values

        Limitations:

        - OCR text grouping can still fail in crowded drawings
        - if no reliable text localization is found, the stage falls back to VLM boxes and loses some geometric precision

        ### Stage 03b/08 - PDF Text BBox Grounding Agent

        Main function:

        - `refinement/pdf_text_bbox_refiner.py -> refine_result_bboxes_from_pdf_text(...)`

        What it does:

        - runs only when the source is a PDF
        - extracts words and text blocks from the native PDF text layer through PyMuPDF
        - builds candidate word clusters
        - tries to match each extracted dimension `raw_text` to a matching text cluster in the PDF
        - refines the dimension bbox and marks `annotation_source = "pdf_text_layer"` when successful

        Why it is used:

        - native PDF text is often more accurate than image OCR, especially for repeated dimensions and dense annotations

        Limitations:

        - if the PDF text layer is missing, broken, or outlined as geometry rather than text, this stage cannot help

        ### Stage 04/08 - Dimension Verification Agent

        Main function:

        - `refinement/dimension_verifier.py -> build_dimension_verification(...)`

        What it does:

        - counts extracted dimensions
        - counts how many extracted dimensions are `annotation_ready`
        - reports missing annotation coverage per view and for unassigned dimensions

        Why it is used:

        - provides a fast coverage signal used both for reporting and for hybrid-model fallback decisions

        Very important limitation:

        - this stage does not know the true number of dimensions on the drawing
        - it only counts dimensions that were already extracted
        - if extraction missed 5 real dimensions, Stage 04 still counts only the extracted set

        ### Stage 05/08 - Value Verification Agent

        Main function:

        - `refinement/value_verifier.py -> verify_dimension_values(...)`

        Supporting functions:

        - `_build_text_matches(...)`
        - `_select_matches_for_view(...)`
        - `_choose_location_match(...)`

        What it does:

        - reruns local OCR text localization on the prepared drawing image
        - for each extracted dimension:
          - takes the extracted `raw_text`
          - searches OCR entries for similar text
          - checks whether the current dimension bbox overlaps the matched OCR text location
        - writes per-dimension verification metadata:
          - `value_verified`
          - `location_verified`
          - `overall_status`
          - `matched_text`
          - `matched_bbox`
          - `match_score`
          - `location_iou`

        Why it is used:

        - this stage is the main automatic sanity check that the extracted value actually exists on the drawing and that the box is on the correct text

        Limitations:

        - it verifies against OCR text, not engineering truth
        - a perfect OCR text match does not prove the dimension was semantically complete
        - OCR errors can create false negatives, especially for small tolerances or unusual symbols

        ### Stage 06/08 - Output Writer Agent

        Main functions:

        - `drawing_pipeline.finalize_pipeline_output(...)`
        - `drawing_pipeline.save_pipeline_outputs(...)`
        - `helpers/overlay_matplotlib.py -> save_result_overlay(...)`

        What it does:

        - writes the extraction JSON
        - writes a text summary
        - writes the annotated image
        - writes the prepared image
        - writes a render manifest
        - writes crop-preprocessing artifacts when enabled

        Why it is used:

        - preserves the full Layer 1 state for inspection, debugging, and downstream consumption

        ### Stage 07/08 - SQLite Import Agent

        Main functions:

        - `helpers/sqlite_importer.py -> create_tables(...)`
        - `helpers/sqlite_importer.py -> migrate_tables(...)`
        - `helpers/sqlite_importer.py -> import_extraction_json_to_sqlite(...)`

        What it does:

        - creates or migrates SQLite tables
        - imports the drawing JSON into structured tables such as documents, dimensions, tolerances, and GD&T-related tables
        - preserves manual dimensions across reimports

        Why it is used:

        - the database becomes the shared state used by human review, CATIA extraction, linking, and Layer 3 DMU runs

        Limitation:

        - SQLite holds the authoritative pipeline state, so manual edits or external corruption of the DB affect downstream layers directly

        ### Stage 08/08 - Human Dimension Review

        Main function:

        - `main_pipelines/human_dimension_review.py -> run_layer1_human_review_stage(...)`

        Important supporting functions:

        - `build_coverage_review_image(...)`
        - `_confirm_coverage_tk(...)`
        - `save_manual_dimension(...)`
        - `update_dimension_record(...)`
        - `delete_dimension_record(...)`
        - `save_dimension_review_decisions_bulk(...)`

        What it does:

        - opens the review UI or a terminal fallback
        - shows the coverage image and extracted dimensions
        - lets the operator:
          - approve dimensions
          - add manual dimensions
          - modify dimensions
          - delete dimensions
          - request a full rerun
        - updates SQLite immediately

        Why it is used:

        - this is the final quality gate before CATIA automation starts
        - it compensates for the known limitations of automatic extraction and OCR verification

        Limitations:

        - human review still depends on the reviewer’s judgment
        - it is a manual bottleneck by design

        ## Hybrid Model Selection and Fallback to GPT-5.5

        Main functions:

        - `drawing_pipeline.run_layer1_2d_pipeline(...)`
        - `drawing_pipeline.assess_fallback_need(...)`

        Current model defaults:

        - primary model in hybrid mode: `gpt-5.4-mini`
        - fallback model in hybrid mode: `gpt-5.5`

        Why the fallback exists:

        - the pipeline first tries the cheaper primary model
        - it then evaluates the result using local post-processing metrics
        - if quality signals are weak, it reruns the full extraction with the stronger model

        Current fallback triggers:

        - no views extracted
        - no dimensions extracted
        - extracted dimensions below the minimum floor
        - green-box dimensions below the minimum floor
        - green-box ratio below the configured threshold
        - bbox mismatches above the configured threshold
        - value verification marked `needs_manual_review = True`

        Why this design is useful:

        - it reduces cost for easy drawings
        - it escalates automatically on hard drawings

        Limitation:

        - fallback is still reactive; it only happens after the first attempt has already produced a weak result

        ## Layer 2 Detailed Breakdown

        ### Stage 01/03 - CATIA Published Parameter Extraction

        Main function:

        - `catia_agents/extract_published_parameters_agent.py -> run_layer2_parameter_extraction_stage(...)`

        Supporting functions:

        - `resolve_target_matches(...)`
        - `extract_publication_rows(...)`
        - `build_parameter_snapshot_map(...)`
        - `resolve_parameter_snapshot(...)`

        What it does:

        - connects to the active CATIA session
        - finds the requested part number inside the open CATProduct
        - reads CATIA publications and resolved parameter values

        Why it is used:

        - Layer 3 cannot actuate CATIA values until the drawing dimensions are mapped to real CATIA published parameters

        Limitations:

        - depends on a live CATIA session and a resolvable CATProduct context
        - if the target part has no accessible publications, mapping cannot proceed

        ### Stage 02/03 - CATIA Published Parameter Output Writer

        Main function:

        - `extract_published_parameters_agent.py -> save_published_parameters_to_sqlite(...)`

        What it does:

        - stores CATIA publication rows into `catia_published_parameters`
        - optionally writes a JSON summary

        Why it is used:

        - this creates the CATIA-side dataset used by the mapping UI

        ### Stage 03/03 - CATIA Dimension Link UI

        Main function:

        - `catia_agents/dimension_link_ui_agent.py -> run_layer2_dimension_linking_stage(...)`

        Supporting functions:

        - `load_dimensions(...)`
        - `load_published_parameters(...)`
        - `upsert_dimension_link(...)`
        - `delete_dimension_link(...)`
        - `build_summary(...)`

        What it does:

        - opens a Tkinter UI with:
          - extracted 2D dimensions
          - CATIA published parameters
        - lets the operator manually link one drawing dimension to one CATIA publication
        - stores mappings in `catia_dimension_links`

        Why it is used:

        - the current system intentionally uses human confirmation for 2D-to-CATIA correspondence
        - this avoids automatically linking the wrong engineering parameter

        Limitations:

        - manual linking takes time
        - later automation is only as good as the mapping decisions stored here

        ## Layer 3 Detailed Breakdown

        ### Layer 3 Design Intent

        Layer 3 is not a global worst-case stack-up engine. It currently performs a one-parameter-at-a-time sweep:

        - one linked CATIA parameter is actuated to maximum or minimum tolerance
        - all other linked parameters are restored to nominal during that run
        - DMU is executed for that one-parameter scenario

        This design answers:

        - which single linked parameter causes a clash, contact, or clearance issue?

        It does not answer:

        - what happens when several parameters are at extreme values simultaneously?

        ### Stage 01/08 and Stage 07/08 - Parameter Sweep Actuation

        Main functions:

        - `catia_agents/max_tolerance_dmu_agent.py -> run_parameter_sweep(...)`
        - `catia_agents/max_tolerance_dmu_agent.py -> apply_parameter_scenario(...)`
        - `catia_agents/max_tolerance_dmu_agent.py -> restore_nominal_parameter_state(...)`

        Supporting functions:

        - `compute_nominal_value(...)`
        - `compute_maximum_value(...)`
        - `compute_minimum_value(...)`
        - `compute_target_value(...)`
        - `_resolve_linked_parameter(...)`

        What it does:

        - loads manually linked rows from `catia_dimension_links`
        - for each linked row:
          - sets the active parameter to max or min
          - sets all other linked parameters to nominal
          - updates and saves the CATIA part
        - after each sweep finishes, restores all linked parameters to nominal

        Why it is used:

        - it isolates the effect of a single dimension-to-parameter relationship

        Limitations:

        - it will not capture multi-parameter interaction effects
        - it assumes the linked parameter can be resolved and written safely

        ### Stage 02/08 - DMU Mode Selection

        Main functions:

        - `choose_dmu_mode(...)`
        - `resolve_dmu_inputs(...)`

        What it does:

        - selects the DMU scope
        - defaults to `selection_against_all`
        - resolves part number inputs for the chosen mode

        Why it is used:

        - DMU needs a valid analysis scope before clash or clearance calculations can run

        Limitation:

        - incorrect part selection produces misleading but structurally valid DMU results

        ### Stage 03/08 - Root CATIA Document Activation

        Main function:

        - `activate_root_document(...)`

        What it does:

        - re-activates the CATIA assembly/product context before DMU

        Why it is used:

        - parameter actuation may temporarily focus a part document
        - DMU must run against the correct root product context

        ### Stage 04/08 - DMU Clash/Contact/Clearance Execution

        Main functions:

        - `run_dmu_cycle(...)`
        - subprocess call into `catia_agents/DMU Agent.py`

        Important DMU Agent functions:

        - `run_contact_plus_clash(...)`
        - `run_clearance(...)`
        - `create_output_paths(...)`

        What it does:

        - launches the standalone DMU agent as a subprocess
        - runs CATIA clash/contact analysis
        - runs CATIA clearance analysis
        - writes per-run files:
          - `results.json`
          - `results.csv`
          - `results.txt`
          - `images/`
        - imports structured results into SQLite:
          - `catia_dmu_runs_max`
          - `catia_dmu_results_max`
          - `catia_dmu_runs_min`
          - `catia_dmu_results_min`

        Why it is used:

        - it keeps the heavy CATIA DMU execution isolated from the Python orchestration layer

        Limitations:

        - requires a valid CATIA DMU-capable environment
        - subprocess failure or CATIA state issues will stop the current run

        ### Stage 05/08 - DMU Preview UI

        Main module:

        - `catia_agents/dmu_ui.py`

        What it does:

        - opens only when the current run contains a clash or clearance
        - displays the result set for human inspection
        - shows which actuated parameter and target value produced the result

        Why it is used:

        - makes the flagged run immediately inspectable without opening raw JSON

        Limitation:

        - it is intentionally selective; non-flagged runs are stored in SQLite but do not automatically open the UI

        ### Stage 06/08 - Minimum Tolerance Decision

        Main function:

        - `choose_minimum_tolerance_followup(...)`

        What it does:

        - asks whether the pipeline should run the minimum-tolerance sweep after maximum runs finish

        Why it is used:

        - lets the operator control whether the additional sweep is needed

        ## Output and Data Persistence by Layer

        ### Layer 1 outputs

        Run root:

        - `Results/drawing_pipeline/<output_name>/`

        Main files:

        - `*_pdf_raw.png`
        - `*_pdf_high_clarity.png`
        - `*_drawing_features.json`
        - `*_summary.txt`
        - `*_annotated.png`
        - `*_render_manifest.json`
        - optional crop-preprocessing images
        - `drawing_data.db`

        ### Layer 2 outputs

        Mainly stored inside the same SQLite database:

        - `catia_published_parameters`
        - `catia_dimension_links`

        Optional file outputs:

        - summary JSON files when explicit CLI paths are provided

        ### Layer 3 outputs

        Stored in the same Layer 1 run folder as timestamped DMU run subfolders:

        - `results.json`
        - `results.csv`
        - `results.txt`
        - `images/`

        Structured database tables:

        - `catia_dmu_runs_max`
        - `catia_dmu_results_max`
        - `catia_dmu_runs_min`
        - `catia_dmu_results_min`

        ## Main Limitations Across the Whole Pipeline

        1. Layer 1 Stage 04 counts only extracted dimensions, not all true drawing dimensions.
        2. Layer 1 Stage 05 verifies OCR text presence and location, not full engineering correctness.
        3. Hybrid fallback improves weak extractions, but it is still reactive.
        4. Layer 2 depends on a live CATIA product structure and usable published parameters.
        5. Manual dimension linking is required for trustworthy 2D-to-CATIA mapping.
        6. Layer 3 currently tests one parameter at a time, so interaction effects between several tolerance extremes are not modeled.
        7. DMU execution depends on CATIA workbench availability, active document state, and correct part scoping.

        ## Why the Current Architecture Makes Sense

        The architecture is intentionally layered:

        - Layer 1 transforms a drawing into a reviewed structured database
        - Layer 2 converts that reviewed database into CATIA parameter mappings
        - Layer 3 uses those mappings for controlled CATIA actuation and DMU analysis

        This separation is useful because each layer has a different reliability problem:

        - Layer 1 solves extraction and review
        - Layer 2 solves semantic mapping into CATIA
        - Layer 3 solves what-if physical interference testing

        ## Complete Flow Narrative

        Full runtime narrative:

        1. The user runs the single pipeline command.
        2. Layer 1 prepares the input image or PDF.
        3. OpenAI extraction creates the initial structured drawing result.
        4. Local bbox refinement and optional PDF text grounding improve locations.
        5. Dimension verification measures annotation coverage.
        6. Value verification checks whether extracted values and boxes align with OCR evidence.
        7. If the hybrid quality checks are weak, the pipeline reruns extraction with `gpt-5.5`.
        8. Layer 1 writes output files and imports the result into SQLite.
        9. Human review confirms, adds, modifies, deletes, or reruns.
        10. Layer 2 extracts CATIA published parameters for the inferred or provided part number.
        11. Layer 2 opens the mapping UI so 2D dimensions can be linked to CATIA publications.
        12. Layer 3 performs a maximum tolerance sweep one linked parameter at a time.
        13. For each actuation case, Layer 3 restores the CATIA root document context, runs DMU, stores the result, and opens the preview UI only for flagged runs.
        14. The operator can then choose whether to run the minimum tolerance sweep.
        15. The pipeline restores all linked parameters back to nominal and prints the final summary.

        ## File Map for the Most Important Runtime Functions

        - `main_pipelines/interactive_pipeline.py`
          - cross-layer orchestration
        - `main_pipelines/drawing_pipeline.py`
          - Layer 1 execution and hybrid model selection
        - `main_pipelines/human_dimension_review.py`
          - human review UI and SQL updates
        - `refinement/bbox_refiner.py`
          - local OCR-driven bbox refinement
        - `refinement/pdf_text_bbox_refiner.py`
          - native PDF text-layer grounding
        - `refinement/dimension_verifier.py`
          - extracted-versus-annotation-ready counting
        - `refinement/value_verifier.py`
          - OCR value and location verification
        - `catia_agents/extract_published_parameters_agent.py`
          - CATIA publication extraction
        - `catia_agents/dimension_link_ui_agent.py`
          - manual 2D-to-CATIA linking UI
        - `catia_agents/max_tolerance_dmu_agent.py`
          - Layer 3 parameter sweep orchestration
        - `catia_agents/DMU Agent.py`
          - CATIA DMU subprocess runner
        - `catia_agents/dmu_ui.py`
          - result preview UI
        - `helpers/sqlite_importer.py`
          - all persistent structured storage

        ## Deliverables Created by the Report Generator

        This generator writes:

        - this Markdown source report
        - a flowchart PNG
        - a PDF version of the report
        """
    ).strip() + "\n"


def load_font(size: int, bold: bool = False):
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_centered_text(draw: ImageDraw.ImageDraw, box, text: str, font, fill):
    x1, y1, x2, y2 = box
    wrapped = textwrap.fill(text, width=max(12, int((x2 - x1) / 14)))
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6, align="center")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    tx = x1 + ((x2 - x1) - text_w) / 2
    ty = y1 + ((y2 - y1) - text_h) / 2
    draw.multiline_text((tx, ty), wrapped, font=font, fill=fill, spacing=6, align="center")


def draw_box(draw, box, text, fill, outline, font):
    draw.rounded_rectangle(box, radius=18, fill=fill, outline=outline, width=3)
    draw_centered_text(draw, box, text, font, fill=(20, 20, 20))


def draw_diamond(draw, center_x, top_y, width, height, text, fill, outline, font):
    half_w = width // 2
    half_h = height // 2
    center_y = top_y + half_h
    points = [
        (center_x, top_y),
        (center_x + half_w, center_y),
        (center_x, top_y + height),
        (center_x - half_w, center_y),
    ]
    draw.polygon(points, fill=fill, outline=outline)
    draw.line(points + [points[0]], fill=outline, width=3)
    draw_centered_text(
        draw,
        (center_x - half_w + 10, top_y + 10, center_x + half_w - 10, top_y + height - 10),
        text,
        font,
        fill=(20, 20, 20),
    )


def arrow(draw, start, end, fill=(70, 70, 70), width=4):
    draw.line([start, end], fill=fill, width=width)
    ex, ey = end
    sx, sy = start
    if abs(ex - sx) < 4:
        direction = 1 if ey >= sy else -1
        left = (ex - 8, ey - 14 * direction)
        right = (ex + 8, ey - 14 * direction)
    else:
        direction = 1 if ex >= sx else -1
        left = (ex - 14 * direction, ey - 8)
        right = (ex - 14 * direction, ey + 8)
    draw.polygon([end, left, right], fill=fill)


def label_text(draw, xy, text, font, fill=(60, 60, 60)):
    draw.text(xy, text, font=font, fill=fill)


def create_flowchart_png(path: Path):
    width, height = 2200, 3600
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34, bold=True)
    box_font = load_font(22, bold=True)
    small_font = load_font(18, bold=False)

    draw.text((60, 35), "Current Pipeline Flowchart", font=title_font, fill=(20, 20, 20))
    draw.text((60, 82), "Live runtime: openai_pipeline/pipeline_stack", font=small_font, fill=(80, 80, 80))

    layer_colors = {
        "l1": ((229, 243, 255), (88, 130, 193)),
        "l2": ((233, 250, 238), (69, 153, 97)),
        "l3": ((255, 243, 224), (199, 126, 49)),
        "decision": ((255, 245, 199), (188, 152, 40)),
    }

    x = 610
    w = 980
    h = 90
    gap = 38
    y = 150

    def add_box(text, layer_key, current_y):
        fill, outline = layer_colors[layer_key]
        box = (x, current_y, x + w, current_y + h)
        draw_box(draw, box, text, fill, outline, box_font)
        return box

    start_box = add_box("Start: run 5_pipeline.py", "l1", y)
    y += h + gap
    box_pdf = add_box("Layer 1 Stage 01/08: PDF Clarity Agent", "l1", y)
    arrow(draw, ((start_box[0] + start_box[2]) // 2, start_box[3]), ((box_pdf[0] + box_pdf[2]) // 2, box_pdf[1]))

    y += h + gap
    box_extract = add_box("Layer 1 Stage 02/08: OpenAI Extraction Agent", "l1", y)
    arrow(draw, ((box_pdf[0] + box_pdf[2]) // 2, box_pdf[3]), ((box_extract[0] + box_extract[2]) // 2, box_extract[1]))

    y += h + gap
    box_bbox = add_box("Layer 1 Stage 03/08: Local BBox Refinement or VLM Box Assignment", "l1", y)
    arrow(draw, ((box_extract[0] + box_extract[2]) // 2, box_extract[3]), ((box_bbox[0] + box_bbox[2]) // 2, box_bbox[1]))

    y += h + gap
    box_pdf_text = add_box("Layer 1 Stage 03b/08: PDF Text BBox Grounding for PDF inputs", "l1", y)
    arrow(draw, ((box_bbox[0] + box_bbox[2]) // 2, box_bbox[3]), ((box_pdf_text[0] + box_pdf_text[2]) // 2, box_pdf_text[1]))

    y += h + gap
    box_dim_verify = add_box("Layer 1 Stage 04/08: Dimension Verification", "l1", y)
    arrow(draw, ((box_pdf_text[0] + box_pdf_text[2]) // 2, box_pdf_text[3]), ((box_dim_verify[0] + box_dim_verify[2]) // 2, box_dim_verify[1]))

    y += h + gap
    box_val_verify = add_box("Layer 1 Stage 05/08: Value Verification", "l1", y)
    arrow(draw, ((box_dim_verify[0] + box_dim_verify[2]) // 2, box_dim_verify[3]), ((box_val_verify[0] + box_val_verify[2]) // 2, box_val_verify[1]))

    y += h + gap + 10
    decision_y = y
    draw_diamond(draw, x + w // 2, decision_y, 540, 140, "Hybrid fallback needed?", *layer_colors["decision"], font=box_font)
    arrow(draw, ((box_val_verify[0] + box_val_verify[2]) // 2, box_val_verify[3]), (x + w // 2, decision_y))

    loop_box = (120, decision_y + 25, 470, decision_y + 105)
    draw_box(draw, loop_box, "Retry Layer 1 extraction with GPT-5.5", (255, 236, 236), (193, 82, 82), small_font)
    arrow(draw, (x + w // 2 - 270, decision_y + 70), (loop_box[2], decision_y + 70))
    label_text(draw, (x + w // 2 - 330, decision_y + 30), "Yes", small_font)
    arrow(draw, (loop_box[0] + 170, loop_box[0] * 0 + loop_box[3]), (loop_box[0] + 170, box_extract[1] + 45))

    y = decision_y + 180
    box_out = add_box("Layer 1 Stage 06/08: Output Writer", "l1", y)
    arrow(draw, (x + w // 2, decision_y + 140), ((box_out[0] + box_out[2]) // 2, box_out[1]))
    label_text(draw, (x + w // 2 + 110, decision_y + 30), "No", small_font)

    y += h + gap
    box_sql = add_box("Layer 1 Stage 07/08: SQLite Import", "l1", y)
    arrow(draw, ((box_out[0] + box_out[2]) // 2, box_out[3]), ((box_sql[0] + box_sql[2]) // 2, box_sql[1]))

    y += h + gap
    box_review = add_box("Layer 1 Stage 08/08: Human Dimension Review", "l1", y)
    arrow(draw, ((box_sql[0] + box_sql[2]) // 2, box_sql[3]), ((box_review[0] + box_review[2]) // 2, box_review[1]))

    decision_review_y = y + h + gap
    draw_diamond(draw, x + w // 2, decision_review_y, 540, 140, "Human review requests rerun?", *layer_colors["decision"], font=box_font)
    arrow(draw, ((box_review[0] + box_review[2]) // 2, box_review[3]), (x + w // 2, decision_review_y))
    label_text(draw, (x + w // 2 - 330, decision_review_y + 30), "Yes", small_font)
    arrow(draw, (x + w // 2 - 270, decision_review_y + 70), (loop_box[2], decision_review_y + 70))

    y = decision_review_y + 180
    box_l2a = add_box("Layer 2 Stage 01/03: CATIA Published Parameter Extraction", "l2", y)
    arrow(draw, (x + w // 2, decision_review_y + 140), ((box_l2a[0] + box_l2a[2]) // 2, box_l2a[1]))
    label_text(draw, (x + w // 2 + 110, decision_review_y + 30), "No", small_font)

    y += h + gap
    box_l2b = add_box("Layer 2 Stage 02/03: Save CATIA publications to SQLite and optional JSON", "l2", y)
    arrow(draw, ((box_l2a[0] + box_l2a[2]) // 2, box_l2a[3]), ((box_l2b[0] + box_l2b[2]) // 2, box_l2b[1]))

    y += h + gap
    box_l2c = add_box("Layer 2 Stage 03/03: Manual Dimension Link UI", "l2", y)
    arrow(draw, ((box_l2b[0] + box_l2b[2]) // 2, box_l2b[3]), ((box_l2c[0] + box_l2c[2]) // 2, box_l2c[1]))

    y += h + gap
    box_l3mode = add_box("Layer 3 Stage 02/08: DMU Mode Selection", "l3", y)
    arrow(draw, ((box_l2c[0] + box_l2c[2]) // 2, box_l2c[3]), ((box_l3mode[0] + box_l3mode[2]) // 2, box_l3mode[1]))

    y += h + gap
    box_l3max = add_box("Layer 3 Stage 01/08: Maximum sweep, one linked parameter at a time", "l3", y)
    arrow(draw, ((box_l3mode[0] + box_l3mode[2]) // 2, box_l3mode[3]), ((box_l3max[0] + box_l3max[2]) // 2, box_l3max[1]))

    y += h + gap
    box_l3run = add_box("For each max case: activate root document, run DMU, save DB rows", "l3", y)
    arrow(draw, ((box_l3max[0] + box_l3max[2]) // 2, box_l3max[3]), ((box_l3run[0] + box_l3run[2]) // 2, box_l3run[1]))

    y += h + gap
    box_l3ui = add_box("Open DMU Preview UI only for clash or clearance", "l3", y)
    arrow(draw, ((box_l3run[0] + box_l3run[2]) // 2, box_l3run[3]), ((box_l3ui[0] + box_l3ui[2]) // 2, box_l3ui[1]))

    decision_min_y = y + h + gap
    draw_diamond(draw, x + w // 2, decision_min_y, 540, 140, "Run minimum sweep?", *layer_colors["decision"], font=box_font)
    arrow(draw, ((box_l3ui[0] + box_l3ui[2]) // 2, box_l3ui[3]), (x + w // 2, decision_min_y))

    y = decision_min_y + 180
    box_l3min = add_box("Layer 3 Stage 07/08: Minimum sweep, one linked parameter at a time", "l3", y)
    arrow(draw, (x + w // 2, decision_min_y + 140), ((box_l3min[0] + box_l3min[2]) // 2, box_l3min[1]))
    label_text(draw, (x + w // 2 - 330, decision_min_y + 30), "Yes", small_font)

    y += h + gap
    box_l3minrun = add_box("For each min case: activate root document, run DMU, save DB rows", "l3", y)
    arrow(draw, ((box_l3min[0] + box_l3min[2]) // 2, box_l3min[3]), ((box_l3minrun[0] + box_l3minrun[2]) // 2, box_l3minrun[1]))

    end_box = (610, y + h + gap + 30, 1590, y + h + gap + 120)
    draw_box(draw, end_box, "End: restore nominal values, keep outputs in Results/drawing_pipeline/<run>/ and print summary", (244, 244, 244), (90, 90, 90), box_font)
    arrow(draw, ((box_l3minrun[0] + box_l3minrun[2]) // 2, box_l3minrun[3]), ((end_box[0] + end_box[2]) // 2, end_box[1]))

    no_end = (1720, decision_min_y + 25, 2050, decision_min_y + 105)
    draw_box(draw, no_end, "Skip minimum sweep", (250, 250, 250), (120, 120, 120), small_font)
    arrow(draw, (x + w // 2 + 270, decision_min_y + 70), (no_end[0], decision_min_y + 70))
    label_text(draw, (x + w // 2 + 110, decision_min_y + 30), "No", small_font)
    arrow(draw, (no_end[2] - 120, no_end[3]), (no_end[2] - 120, end_box[1] + 40))

    legend_y = height - 170
    draw.text((60, legend_y), "Legend", font=box_font, fill=(30, 30, 30))
    legends = [
        ("Layer 1 drawing pipeline", layer_colors["l1"][0], layer_colors["l1"][1]),
        ("Layer 2 CATIA publication and mapping", layer_colors["l2"][0], layer_colors["l2"][1]),
        ("Layer 3 CATIA tolerance actuation and DMU", layer_colors["l3"][0], layer_colors["l3"][1]),
        ("Decision point", layer_colors["decision"][0], layer_colors["decision"][1]),
    ]
    lx = 60
    for label, fill, outline in legends:
        draw.rounded_rectangle((lx, legend_y + 48, lx + 80, legend_y + 88), radius=8, fill=fill, outline=outline, width=2)
        draw.text((lx + 100, legend_y + 53), label, font=small_font, fill=(50, 50, 50))
        lx += 480

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def normalize_markdown_for_pdf(markdown_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            lines.append(line[2:].upper())
            lines.append("")
        elif line.startswith("## "):
            lines.append(line[3:].upper())
            lines.append("")
        elif line.startswith("### "):
            lines.append(line[4:])
        elif line.startswith("- "):
            lines.append("• " + line[2:])
        elif line.startswith("  - "):
            lines.append("  • " + line[4:])
        elif line.startswith("1. "):
            lines.append(line)
        else:
            lines.append(line)
    return lines


def wrap_lines(lines: list[str], width: int = 100) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if not line.strip():
            wrapped.append("")
            continue
        if line.isupper() and len(line) < width:
            wrapped.append(line)
            continue
        initial = ""
        subsequent = ""
        content = line
        if line.startswith("• "):
            initial = "• "
            subsequent = "  "
            content = line[2:]
        elif line.startswith("  • "):
            initial = "  • "
            subsequent = "    "
            content = line[4:]
        elif len(line) > 3 and line[0].isdigit() and line[1:3] == ". ":
            marker, content = line.split(" ", 1)
            initial = marker + " "
            subsequent = " " * len(initial)
        wrapped.extend(
            textwrap.wrap(
                content,
                width=width,
                initial_indent=initial,
                subsequent_indent=subsequent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [line]
        )
    return wrapped


def add_text_pages(doc: fitz.Document, markdown_text: str):
    page_width = 595
    page_height = 842
    left = 46
    top = 52
    right = 46
    bottom = 46
    font_size = 10.5
    line_height = 15

    normalized = normalize_markdown_for_pdf(markdown_text)
    wrapped = wrap_lines(normalized, width=98)
    max_lines = int((page_height - top - bottom) / line_height)

    index = 0
    page_number = 1
    while index < len(wrapped):
        page = doc.new_page(width=page_width, height=page_height)
        rect = fitz.Rect(left, top, page_width - right, page_height - bottom)
        batch = wrapped[index : index + max_lines]
        text = "\n".join(batch)
        page.insert_textbox(rect, text, fontsize=font_size, fontname="cour", color=(0.1, 0.1, 0.1), lineheight=1.15)
        page.insert_text((left, page_height - 24), f"Pipeline Report | Page {page_number}", fontsize=8, fontname="helv", color=(0.4, 0.4, 0.4))
        index += max_lines
        page_number += 1


def add_cover_page(doc: fitz.Document):
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(0, 0, 595, 842), color=(1, 1, 1), fill=(0.97, 0.98, 1.0))
    page.draw_rect(fitz.Rect(42, 42, 553, 800), color=(0.25, 0.42, 0.68), width=2)
    page.insert_text((60, 120), "Current Pipeline Architecture Report", fontsize=26, fontname="helv", fill=(0.12, 0.23, 0.38))
    page.insert_text((60, 165), "OpenAI Drawing Extraction + CATIA Mapping + DMU Sweep", fontsize=14, fontname="helv", fill=(0.28, 0.28, 0.28))
    summary = "\n".join(
        [
            "Scope:",
            "  - Live runtime under openai_pipeline/pipeline_stack",
            "  - Layer 1 drawing extraction and review",
            "  - Layer 2 CATIA publication capture and mapping",
            "  - Layer 3 parameter sweep actuation and DMU analysis",
            "",
            "Deliverables in this PDF:",
            "  - architecture summary",
            "  - stage-by-stage explanation",
            "  - function map",
            "  - limitations",
            "  - complete flowchart",
        ]
    )
    page.insert_textbox(fitz.Rect(60, 225, 520, 440), summary, fontsize=12, fontname="cour", color=(0.15, 0.15, 0.15), lineheight=1.3)
    page.insert_text((60, 760), f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", fontsize=10, fontname="helv", fill=(0.35, 0.35, 0.35))
    page.insert_text((60, 785), f"Source root: {REPO_ROOT}", fontsize=8.5, fontname="cour", fill=(0.35, 0.35, 0.35))


def add_flowchart_page(doc: fitz.Document, flowchart_path: Path):
    page = doc.new_page(width=842, height=595)
    page.insert_text((30, 28), "Complete Flowchart", fontsize=18, fontname="helv", fill=(0.12, 0.23, 0.38))
    rect = fitz.Rect(20, 42, 822, 575)
    page.insert_image(rect, filename=str(flowchart_path))
    page.insert_text((22, 585), "Flowchart page", fontsize=8, fontname="helv", fill=(0.4, 0.4, 0.4))


def generate_pdf(markdown_text: str, flowchart_path: Path, pdf_path: Path):
    doc = fitz.open()
    add_cover_page(doc)
    add_text_pages(doc, markdown_text)
    add_flowchart_page(doc, flowchart_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(pdf_path))
    doc.close()


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown_text = build_report_markdown()
    MARKDOWN_PATH.write_text(markdown_text, encoding="utf-8")
    create_flowchart_png(FLOWCHART_PATH)
    generate_pdf(markdown_text, FLOWCHART_PATH, PDF_PATH)
    print("Markdown:", MARKDOWN_PATH)
    print("Flowchart:", FLOWCHART_PATH)
    print("PDF:", PDF_PATH)


if __name__ == "__main__":
    main()
