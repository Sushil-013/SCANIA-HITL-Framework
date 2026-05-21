# OpenAI Pipeline

This folder is now the shareable home for the active OpenAI drawing-to-CATIA pipeline, including the new master UI and the local CRAFT runtime used by Layer 1 refinement.

## What is inside this folder

- `5_pipeline.py`
  Main CLI entrypoint for the full pipeline.

- `master_pipeline_ui.py`
  Entry script for the master UI you added.

- `pipeline_stack/`
  Canonical runtime for Layer 1, Layer 2, and Layer 3.

- `craft_runtime/`
  Bundled local CRAFT + GLM-OCR support files used by bbox refinement and value verification.

- `craft_runtime/weights/craft_mlt_25k.pth`
  Bundled CRAFT detection weight used by the pipeline.

- `prompts/`
  Prompt assets for extraction tuning.

- `requirements.txt`
  Pip install list for this pipeline package.

- `PIPELINE_ORDER.md`
  Stage-by-stage execution order.

- `THIRD_PARTY_NOTICES.md`
  Third-party attribution and citation guidance for bundled components.

## Shareable package goal

The OpenAI pipeline no longer needs the repo-root `CRAFT Model` folder to run its local refinement stages.
The required CRAFT runtime files now live inside `openai_pipeline/craft_runtime/`, so this folder is much closer to a single handoff package for your friend.

To avoid breaking your other experiments in this repo, the original root-level model files were left in place. The active pipeline now loads the bundled copy from inside `openai_pipeline`.

## Recommended Python environment

Use the same Python family as your current virtual environment:

- Python `3.10.x`
- Windows PowerShell

## Fresh setup from zero

Run these commands from the folder that contains `openai_pipeline`:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r .\openai_pipeline\requirements.txt
```

Set the OpenAI key before running Layer 1:

```powershell
$env:OPENAI_API_KEY = "your_openai_api_key_here"
```

Notes:

- `tkinter` is used by the review UI and the master UI. It normally comes with standard Windows Python and is not installed from `requirements.txt`.
- Layer 2 and Layer 3 also require CATIA on Windows.
- If `transformers==5.3.0.dev0` is not available on another machine, install the same build you used locally or relax only that line to a compatible `transformers` version used with GLM-OCR.

## Installed dependency snapshot

The current working environment used for this package has these direct versions:

- `openai==2.29.0`
- `Pillow==9.4.0`
- `PyMuPDF==1.27.2.2`
- `matplotlib==3.10.8`
- `numpy==1.26.4`
- `opencv-python==4.8.1.78`
- `scikit-image==0.21.0`
- `torch==2.11.0`
- `torchvision==0.26.0`
- `transformers==5.3.0.dev0`
- `accelerate==1.13.0`
- `safetensors==0.7.0`
- `python-pptx==1.0.2`
- `Pygments==2.20.0`
- `pywin32==311`
- `pycatia==0.9.6`

## How to run

### Master UI

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\master_pipeline_ui.py
```

What it does:

- launches one clean end-to-end run
- sends Layer 1 through extraction and review
- lets the review popup decide whether Layer 2 and Layer 3 continue
- keeps the high-level operator flow inside one window

### CLI pipeline

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\5_pipeline.py --image "C:\path\to\drawing.pdf" --pdf_dpi 450
```

Useful example with an image instead of a PDF:

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\5_pipeline.py --image "C:\path\to\drawing.png"
```

## Default outputs

- Layer 1 output root: `Results/drawing_pipeline/`
- Layer 3 output root: `Results/layer3_dmu/`

Typical Layer 1 outputs per drawing:

- extraction JSON
- summary text
- annotated image
- prepared image
- SQLite database
- crop preprocessing images when enabled
- coverage review outputs

## Layer summary

### Layer 1

- drawing loading
- PDF clarity preparation
- OpenAI extraction
- local bbox refinement with bundled CRAFT runtime
- PDF text grounding for PDFs
- dimension verification
- value verification
- JSON/image/SQLite output writing
- human review

### Layer 2

- CATIA published-parameter extraction
- CATIA extraction summary output
- dimension-to-parameter linking UI

### Layer 3

- tolerance actuation
- nominal/max/min DMU workflow
- result logging
- preview UI

## Files your friend should receive

At minimum, send:

- the full `openai_pipeline/` folder
- a folder containing the drawings they want to process
- any CATIA source files needed for Layer 2 and Layer 3

If you want the exact same folder layout as your current machine, also keep the parent folder structure so output paths like `Results/` are created next to `openai_pipeline`.

## Sharing and citation

For sharing this folder with a friend or professor:

- you do not need to buy a separate CRAFT license
- the bundled CRAFT code is under the MIT license
- the package now includes `openai_pipeline/craft_runtime/LICENSE`
- the package also includes `openai_pipeline/THIRD_PARTY_NOTICES.md`

What you should do when sharing:

- keep the bundled CRAFT license file with the package
- keep the third-party notice file with the package

Do you need citation?

- for normal sharing with a friend or professor, usually `no` as a strict legal requirement
- for a thesis, paper, report, or presentation, `yes`, it is strongly recommended

Recommended citation for academic use:

```text
Baek, Youngmin, Bado Lee, Dongyoon Han, Sangdoo Yun, and Hwalsuk Lee.
"Character Region Awareness for Text Detection."
Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR), 2019.
```

## Important runtime notes

- Layer 1 can run without CATIA.
- Layer 2 and Layer 3 are Windows-only in practice because they depend on CATIA automation.
- The bundled CRAFT weight is required when bbox refinement or value verification is enabled.
- The master UI defaults to hybrid model mode, multi-pass extraction, dynamic crop preprocessing, local value verification, local bbox refinement, and human review.

## Main code location

See [pipeline_stack/README.md](</c:/Users/SSAAFL/Documents/Tolerance/ED3 - CRAFT/CRAFT-pytorch-master/openai_pipeline/pipeline_stack/README.md>) for the runtime modules grouped by responsibility.
