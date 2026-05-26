# 🚛 SCANIA HITL Framework: Geometric Validation

An automated **Human-in-the-Loop (HITL)** pipeline bridging 2D legacy engineering drawings to 3D **CATIA V5 ENOVIA VPM** models for early-stage clash detection. 

## 🏗️ System Architecture & Multi-Agent Pipeline

The framework is orchestrated by a unified `CustomTkinter` Master Application (`master_app.py`) which acts as the Control Center. The pipeline is divided into four sequential layers. To ensure safety and determinism, probabilistic AI outputs are strictly air-gapped from the CAD environment via a central SQLite database (`eats_validation.db`) and rigorous HITL validation gates.

> **Pipeline Flow:**
> ```text
>   Drawing (PDF/Image)
>         │
>         ▼
>   LAYER 1 — 2D Perception (OpenAI GPT-4o)
>         │   Tiled multi-modal extraction → Structured JSON tolerances
>         ▼
>   LAYER 2 — CAD Structural Fetch (COM API)
>         │   Harvests published parameters directly from live CATIA assembly
>         ▼
>   LAYER 3 — Human Mapping (HITL UI)
>         │   Engineer explicitly links 2D features ↔ 3D CAD parameters
>         ▼
>   LAYER 4 — CAD Actuation & DMU Clash Validation
>         │   Drives CATIA to MMC/LMC, executes DMU Space Analysis, 
>         │   captures annotated clash evidence, records HITL decision
>         ▼
>   Report Generator
>         Stand-alone HTML report generated for engineering review
> ```

---

## ⚙️ Core Modules

### 👁️ Layer 1: Perception Module (2D Extraction)
**File:** `agents/A1_2D_Extraction.py`  
Extracts geometric dimensions and tolerances (GD&T) from 2D PDF drawings using a multi-modal Vision-Language Model (**OpenAI GPT-4o**) utilizing a tiled-vision approach.
* **Agent 1A (Vision):** Splits the drawing into 4 overlapping quadrants. Scans for raw text, enforcing strict spatial rules for Feature Control Frames (FCF) and datum attachments.
* **Agent 1B (JSON Structuring):** A text-only agent that parses the raw text into strict JSON, applying custom merging rules to resolve overlap artifacts.
* **Agent 1C (QA Audit):** Cross-checks the JSON against the raw vision text to ensure zero dropped dimensions.
* **HITL Gatekeeper & Nx Expansion:** Extracted tolerances are displayed in a custom Tkinter UI, requiring the engineer to **approve, edit, or reject** the data. If approved, grouped features (e.g., "4X") are automatically expanded into distinct database rows for 1-to-1 CAD mapping.

### 🌐 Layer 2: Logical Module (CAD Extraction)
**File:** `agents/A2_Cad_Extraction.py`  
Scans the active 3D CAD environment to discover controllable parameters.
* **COM API Integration:** Connects directly to the live CATIA V5 / ENOVIA VPM session via `win32com.client`.
* **Tree Walking & Dual-Path Resolution:** Recursively walks the `.CATProduct` assembly tree to locate all officially "Published" parameters, using a robust dual-path fallback strategy to prevent API crashes. 
* **Database Registration:** Extracts the internal parameter name, formula, current value, and unit, saving them to the database for future actuation.

### 🤝 Layer 3: Logical Module (Semantic Mapping)
**File:** `agents/A3_UI_Mapping.py`  
Because 2D drawing annotations rarely match 3D CAD parameter names perfectly, this layer provides a safe, manual bridge.
* **Contextual UI:** Displays unlinked 2D tolerances side-by-side with unlinked 3D CAD parameters.
* **Human Authority:** The engineer explicitly maps the features based on design intent. This mapped relationship (`vlm_id` ↔ `cad_id`) is saved to the `human_mapping` table and acts as the instruction manual for the Actuator.

### 🚀 Layer 4: Actuation & DMU Validation
**File:** `agents/A4_The_Actuator.py`  
The deterministic execution engine that physically drives the CAD model and detects clashes.
* **Boundary Actuation:** Automatically computes the Maximum Material Condition (**MMC**) and Least Material Condition (**LMC**) boundaries for each mapped parameter and drives the live CATIA model to those extremes.
* **Safe Reversion & Tree Hygiene:** Wraps all CATIA manipulation in strict `try/finally` blocks, guaranteeing the CAD model **reverts to nominal** after evaluation. It also actively deletes temporary DMU objects to prevent master model corruption.
* **DMU Space Analysis:** Programmatically runs CATIA Clash Detection specifically targeting the actuated parts, filtering out irrelevant background noise.
* **Resilient Capture & Clash HUD:** Re-frames the CATIA viewport and safely captures the clash using a multi-format fallback loop (PNG -> JPG -> BMP). The `Pillow` library is then used to overlay a red bounding frame and an informational "Clash HUD" (detailing penetration depth and part IDs) directly onto the image.
* **HITL Decision Review:** Pauses CAD execution and presents the annotated screenshot to the engineer, who must classify the clash as an *Approve*, *Reject (False Positive)*, or *Override*. 

### 📊 Automated Reporting
**File:** `generate_report.py`  
A dedicated module that transforms the final database results into a professional, interactive HTML report for engineering review.
* **Zero-Dependency Portability (Base64):** Dynamically encodes all captured clash screenshots into `base64` data-URIs. This embeds the images directly into the HTML markup, creating a **single, standalone file** that can be easily emailed or uploaded to Scania’s PLM system without broken image links.
* **Interactive Dashboard:** Features a top-level KPI summary, a pass/fail matrix for all evaluated parameters, and a detailed, color-coded conflict ledger.
* **Photographic Evidence:** Engineers can click on any recorded clash within the report ledger to instantly expand and view the high-resolution, HUD-annotated spatial capture.

---

## 🗄️ Database Architecture (The Digital Thread)

The framework relies on a local SQLite database (`eats_validation.db`) to maintain full deterministic traceability from the original drawing to the final DMU clash result.

| Table Name | Purpose |
| :--- | :--- |
| `drawings` & `semantic_tolerances` | Stores the AI extraction results and drawing metadata. |
| `catia_assemblies` & `catia_parameters` | Stores the 3D parametric data pulled directly from ENOVIA VPM. |
| `human_mapping` | The relational bridge joining the 2D ID (`vlm_id`) to the 3D ID (`cad_id`). |
| `dmu_mmc_results` & `dmu_lmc_results` | The final output tables storing the evaluated boundaries, parts involved, clash penetration depths, image paths, and human review decisions. |

---

## 🚀 Usage Guide for Scania Engineers (Standalone App)

For ease of use within the Scania enterprise environment, this entire pipeline has been compiled into a single, standalone Windows executable. **No Python installation, virtual environments, or terminal commands are required.**

### Prerequisites
* Windows 10 or 11.
* CATIA V5 or ENOVIA VPM open locally with an active SPA license.
* An active OpenAI API key.

### How to Run
1. Download the latest `SCANIA_HITL_Framework.exe` from the **[Releases](../../releases)** page on this GitHub repository.
2. Place the `.exe` inside an empty folder on your machine.
3. Double-click the `.exe` to launch the application.
4. **API Key Authentication:** * On first launch, the app will display a secure prompt asking for your OpenAI API Key. 
   * It will safely encrypt and store this key in your **Windows Credential Manager**.
   * On subsequent launches, the app will detect the saved key and simply ask for your permission to use it, preventing the need to re-paste it or manage hidden `.env` files.
5. The Master Control Center will open. Run your validation pipeline, and all databases and HTML reports will auto-generate right next to the executable.

---

## 💻 Developer Guide (Source Code Setup)

If you are an academic examiner or developer wishing to inspect, run, or build the raw source code, please follow these steps:

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Sushil-013/SCANIA-HITL-Framework.git
   cd SCANIA-HITL-Framework
   ```

2. **Set up the virtual environment** inside the `Approach_1_SQLite_Tkinter` directory:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
