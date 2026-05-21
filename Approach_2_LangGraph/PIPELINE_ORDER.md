# Pipeline Order

The live runtime is `openai_pipeline/pipeline_stack/`.

## Main launch paths

CLI:

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\5_pipeline.py --image "C:\path\to\drawing.pdf" --pdf_dpi 450
```

Master UI:

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\master_pipeline_ui.py
```

Default output roots:

- `Results/drawing_pipeline/`
- `Results/layer3_dmu/`

## High-level order

1. Input drawing is selected.
2. Layer 1 extracts and verifies drawing information.
3. Human review approves, rejects, or adds dimensions.
4. Review decides whether Layer 2 continues.
5. Layer 2 extracts CATIA published parameters and opens the link UI.
6. Review/Layer 2 decisions decide whether Layer 3 continues.
7. Layer 3 runs tolerance and DMU analysis.

## Master UI behavior

The master UI is a controlled wrapper around the full pipeline:

1. User selects a PDF or image.
2. UI launches `5_pipeline.py` with fixed safe defaults.
3. Layer 1 runs automatically.
4. Human review popup opens.
5. The review popup controls whether Layer 2 and Layer 3 continue.
6. The master UI tracks status in five blocks:
   - input / extraction
   - review
   - Layer 2 parameter extract
   - Layer 2 dimension link
   - Layer 3 DMU

## Layer 1 detailed order

`Layer 1 Stage 01/08`
- Input loading and PDF clarity preparation.
- For PDFs, the page is rendered at the selected DPI.
- Preprocessed images are prepared for OpenAI and local OCR checks.

`Layer 1 Stage 02/08`
- OpenAI extraction.
- The pipeline runs either direct or hybrid model selection.
- Multi-pass mode separates structure, dimensions, recovery, and GD&T passes.

`Layer 1 Stage 03/08`
- Local bbox refinement.
- Uses the bundled `openai_pipeline/craft_runtime/` package.
- CRAFT detects text regions.
- GLM-OCR reads local crops.
- Extracted dimensions, datums, and feature control frames get tighter boxes.

`Layer 1 Stage 03b/08`
- PDF text bbox grounding.
- For PDF inputs, boxes are aligned against the native PDF text layer with PyMuPDF.

`Layer 1 Stage 04/08`
- Dimension verification.
- Coverage and extracted-dimension counts are computed.

`Layer 1 Stage 05/08`
- Value verification.
- Local OCR checks whether extracted values and locations match visible text.

`Layer 1 Stage 06/08`
- Output writer.
- JSON, summary text, annotated image, prepared image, and crop outputs are written.

`Layer 1 Stage 07/08`
- SQLite import.
- Extraction results are imported into the drawing database.

`Layer 1 Stage 08/08`
- Human review.
- User can approve/reject dimensions, add missing ones, and request reruns.

## Layer 2 detailed order

`Layer 2 Stage 01/03`
- CATIA published-parameter extraction.
- Reads the CATIA product/part and writes published parameters to SQLite.

`Layer 2 Stage 02/03`
- CATIA extraction summary output.
- Summary JSON and DB updates are saved.

`Layer 2 Stage 03/03`
- CATIA dimension link UI.
- User links approved 2D dimensions to CATIA parameters.

## Layer 3 detailed order

`Layer 3 Stage 01/05`
- CATIA tolerance actuation setup.

`Layer 3 Stage 02/05`
- DMU mode selection.

`Layer 3 Stage 03/05`
- Root CATIA document activation / target resolution.

`Layer 3 Stage 04/05`
- Nominal plus per-dimension max/min DMU clash analysis.

`Layer 3 Stage 05/05`
- DMU preview UI and result review.

## Main runtime modules

- `pipeline_stack/main_pipelines/interactive_pipeline.py`
  Top-level orchestration across Layer 1 to Layer 3.

- `pipeline_stack/main_pipelines/drawing_pipeline.py`
  Layer 1 extraction, refinement, verification, and output writing.

- `pipeline_stack/main_pipelines/human_dimension_review.py`
  Human review workflow.

- `pipeline_stack/main_pipelines/master_pipeline_ui.py`
  Master UI controller and run monitor.

- `pipeline_stack/refinement/bbox_refiner.py`
  Local CRAFT + GLM-OCR bbox refinement loader.

- `pipeline_stack/catia_agents/`
  CATIA extraction, linking, tolerance, DMU, and review tools.

## Bundled local model/runtime files used by this order

- `craft_runtime/craft_glmocr_pipeline.py`
- `craft_runtime/craft.py`
- `craft_runtime/refinenet.py`
- `craft_runtime/imgproc.py`
- `craft_runtime/basenet/vgg16_bn.py`
- `craft_runtime/weights/craft_mlt_25k.pth`
