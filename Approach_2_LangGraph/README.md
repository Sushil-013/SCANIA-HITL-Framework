# OpenAI Pipeline & CRAFT Framework
*Drawing-to-CATIA Geometric Validation Pipeline*

This folder is the shareable home for the active OpenAI drawing-to-CATIA pipeline. It includes the unified Master UI and a bundled, localized **CRAFT runtime** used for Layer 1 bounding-box refinement and value verification.

## 🎯 Shareable Package Goal
This pipeline has been refactored to be a standalone handoff package. It no longer relies on a repository-root `CRAFT Model` folder to run its local refinement stages. All required CRAFT runtime files and weights now live directly inside `openai_pipeline/craft_runtime/`. 

*(Note: To avoid breaking legacy experiments in the wider repository, original root-level model files were left in place, but this active pipeline strictly loads from its internal bundled copy).*

---

## 🏗️ Layer Architecture Summary

The pipeline is divided into three primary operational layers:

### Layer 1: Perception & Refinement
* **Document Prep:** Drawing loading, PDF clarity preparation, and PDF text grounding.
* **Extraction:** OpenAI-driven geometric feature extraction.
* **Refinement:** Local bounding box refinement using the bundled CRAFT runtime.
* **Verification:** Dimension and value verification.
* **Outputs & HITL:** JSON/Image/SQLite output writing and Human-in-the-Loop review.

### Layer 2: CAD Extraction & Mapping
* **CAD Fetch:** CATIA published-parameter extraction via COM automation.
* **Logging:** CATIA extraction summary outputs.
* **Mapping:** Dimension-to-parameter linking UI for the engineer.

### Layer 3: Actuation & DMU
* **Actuation:** Tolerance actuation and Nominal/Max/Min DMU workflow.
* **Validation:** Result logging and automated preview UI.

---

## 📁 Repository Structure

* `5_pipeline.py` — Main CLI entrypoint for the full pipeline.
* `master_pipeline_ui.py` — Entry script for the unified Master UI.
* `pipeline_stack/` — Canonical runtime modules for Layer 1, Layer 2, and Layer 3. *(See `pipeline_stack/README.md` for runtime modules grouped by responsibility).*
* `craft_runtime/` — Bundled local CRAFT + GLM-OCR support files used by bbox refinement and value verification.
* `craft_runtime/weights/craft_mlt_25k.pth` — Bundled CRAFT detection weight used by the pipeline.
* `prompts/` — Prompt assets for extraction tuning.
* `requirements.txt` — Pip install list specifically for this pipeline package.
* `PIPELINE_ORDER.md` — Stage-by-stage execution order.
* `THIRD_PARTY_NOTICES.md` — Third-party attribution and citation guidance for bundled components.

---

## 🚀 Setup & Installation

**Recommended Environment:**
* Python `3.10.x`
* Windows PowerShell

### 1. Fresh Setup from Zero
Run these commands from the folder that contains `openai_pipeline`:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r .\openai_pipeline\requirements.txt
```

### 2. Environment Variables
Set your OpenAI API key before running Layer 1:
```powershell
$env:OPENAI_API_KEY = "your_openai_api_key_here"
```

### 3. Installation Notes
* **Tkinter:** Used by the review UI and the Master UI. It normally comes pre-packaged with standard Windows Python and is not installed from `requirements.txt`.
* **CATIA:** Layer 2 and Layer 3 physically require CATIA installed on a Windows machine.
* **Transformers Version:** If `transformers==5.3.0` is not available on another machine, install the exact same build you used locally, or relax only that line to a compatible `transformers` version used with GLM-OCR.

<details>
<summary><b>📦 View Installed Dependency Snapshot</b></summary>
The current working environment used for this package relies on these direct versions:

* `openai==2.29.0`
* `Pillow==9.4.0`
* `PyMuPDF==1.27.2.2`
* `matplotlib==3.10.8`
* `numpy==1.26.4`
* `opencv-python==4.8.1.78`
* `scikit-image==0.21.0`
* `torch==2.11.0`
* `torchvision==0.26.0`
* `transformers==5.3.0`
* `accelerate==1.13.0`
* `safetensors==0.7.0`
* `python-pptx==1.0.2`
* `Pygments==2.20.0`
* `pywin32==311`
* `pycatia==0.9.6`
</details>

---

## ⚙️ How to Run

### Option A: Master UI (Recommended)
Launches one clean end-to-end run. It sends Layer 1 through extraction and review, lets the review popup decide whether Layer 2 and Layer 3 continue, and keeps the high-level operator flow inside one window.

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\master_pipeline_ui.py
```

### Option B: CLI Pipeline (Headless)
Useful for direct execution:

```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\5_pipeline.py --image "C:\path\to\drawing.pdf" --pdf_dpi 450
```
*Useful example with an image instead of a PDF:*
```powershell
.\.venv\Scripts\python.exe .\openai_pipeline\5_pipeline.py --image "C:\path\to\drawing.png"
```

---

## 📂 Default Outputs

If you keep the parent folder structure intact, output paths will automatically be created next to `openai_pipeline`.

* **Layer 1 Output Root:** `Results/drawing_pipeline/`
  *(Typical outputs: extraction JSON, summary text, annotated image, prepared image, SQLite database, coverage review outputs, and crop preprocessing images when enabled).*
* **Layer 3 Output Root:** `Results/layer3_dmu/`

---

## ⚠️ Important Runtime Notes

* **Layer Independence:** Layer 1 can run entirely standalone without CATIA.
* **Windows Requirement:** Layer 2 and Layer 3 are Windows-only in practice because they depend strictly on CATIA automation.
* **CRAFT Dependencies:** The bundled CRAFT weight is required when bbox refinement or value verification is enabled.
* **UI Defaults:** The Master UI defaults to hybrid model mode, multi-pass extraction, dynamic crop preprocessing, local value verification, local bbox refinement, and human review.

---

## 📦 Package Handoff & Sharing

**Files to send for handoff:**
At minimum, if you are sending this to a friend or colleague, provide:
1. The full `openai_pipeline/` folder.
2. A folder containing the drawings they want to process.
3. Any CATIA source files (`.CATProduct` / `.CATPart`) needed for Layers 2 and 3.

**Licensing & Citation:**
* You do not need to buy a separate CRAFT license to share this. The bundled CRAFT code is under the **MIT license**.
* The package includes `openai_pipeline/craft_runtime/LICENSE` and `openai_pipeline/THIRD_PARTY_NOTICES.md`. You **must** keep the bundled CRAFT license file and the third-party notice file with the package when sharing.
* *Do you need to cite this?* For normal sharing with a friend, usually no. However, for a thesis, paper, report, or presentation, **yes, it is strongly recommended.**

**Recommended Citation for Academic Use:**
> Baek, Youngmin, Bado Lee, Dongyoon Han, Sangdoo Yun, and Hwalsuk Lee. "Character Region Awareness for Text Detection." *Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)*, 2019.
