# SCANIA HITL Framework: Geometric Validation

An automated **Human-in-the-Loop (HITL)** pipeline bridging 2D legacy engineering drawings to 3D CATIA V5 ENOVIA VPM models for early-stage clash detection. 

This software was developed as part of a Master's Thesis at **Linköping University (LiU)** in collaboration with **Scania CV AB**. It serves as an exploratory system to solve the "Semantic Gap" between disconnected 2D visual data and parametric 3D CAD environments.

---

## 🏗 System Architecture & Multi-Agent Pipeline

The framework is orchestrated by a unified `CustomTkinter` Master Application (`master_app.py`) which acts as the Control Center. The pipeline is divided into four sequential layers. To ensure safety and determinism, probabilistic AI outputs are strictly air-gapped from the CAD environment via a central SQLite database (`eats_validation.db`) and rigorous HITL validation gates.

### Layer 1: Perception Module (2D Extraction)
**File:** `agents/A1_2D_Extraction.py`
This module extracts geometric dimensions and tolerances (GD&T) from 2D PDF drawings using a multi-modal Vision-Language Model (OpenAI GPT-4o).
* **Tiled-Vision Approach:** Large engineering drawings are automatically split into 4 overlapping quadrants to maintain high resolution.
* **Agent Consensus & Filtering:** The pipeline uses multiple prompts to extract, structure into strict JSON, and verify data. A programmatic filter automatically drops zero-tolerance dimensions (e.g., +0.0 / -0.0) that do not affect spatial clearance.
* **HITL Gatekeeper:** Extracted tolerances are displayed in a custom UI, requiring the engineer to approve, edit, or reject the data before it is committed to the database.

### Layer 2: Logical Module (CAD Extraction)
**File:** `agents/A2_Cad_Extraction.py`
This module scans the active 3D CAD environment to discover controllable parameters.
* **COM API Integration:** Connects directly to the live CATIA V5 / ENOVIA VPM session via `win32com.client`.
* **Tree Walking:** Recursively walks the `.CATProduct` assembly tree to locate all officially "Published" parameters. 
* **Database Registration:** Extracts the internal parameter name, formula, current value, and unit, saving them to the database for future actuation.

### Layer 3: Logical Module (Semantic Mapping)
**File:** `agents/A3_UI_Mapping.py`
Because 2D drawing annotations rarely match 3D CAD parameter names perfectly, this layer provides a safe, manual bridge.
* **Contextual UI:** Displays unlinked 2D tolerances side-by-side with unlinked 3D CAD parameters.
* **Human Authority:** The engineer explicitly maps the features based on design intent. This mapped relationship (`vlm_id` ↔ `cad_id`) is saved to the `human_mapping` table and acts as the instruction manual for the Actuator.

### Layer 4: Actuation & DMU Validation
**File:** `agents/A4_The_Actuator.py`
The execution engine that physically drives the CAD model and detects clashes.
* **MMC & LMC Actuation:** Automatically computes the Maximum Material Condition (MMC) and Least Material Condition (LMC) boundaries for each mapped parameter and drives the live CATIA model to those extremes.
* **DMU Space Analysis:** Programmatically runs CATIA Clash Detection specifically targeting the actuated parts, filtering out irrelevant background clashes.
* **Dual-Camera Montage:** Automatically generates a stitched side-by-side screenshot (Context View + Zoomed Detail View) using the `Pillow` library, overlaying a custom HUD with the exact penetration depth.
* **HITL Decision Review:** Pauses CAD execution and presents the screenshot to the engineer, who must classify the clash as an *Approve*, *Reject (False Positive)*, or *Override*. 

---

## 🗄️ Database Architecture (The Digital Thread)

The framework relies on a local SQLite database (`eats_validation.db`) to maintain full traceability from the original drawing to the final DMU clash result.

Key Tables:
* `drawings` & `semantic_tolerances`: Stores the AI extraction results.
* `catia_assemblies` & `catia_parameters`: Stores the 3D data pulled from ENOVIA VPM.
* `human_mapping`: The relational bridge joining the 2D ID (`vlm_id`) to the 3D ID (`cad_id`).
* `actuation_results`: The final output table storing the evaluated boundaries, parts involved, clash penetration depths, and human review decisions.

---

## 🚀 Setup & Installation

### Prerequisites
* **Python:** 3.10 or higher.
* **CAD Software:** CATIA V5 / ENOVIA VPM installed and licensed with SPA (Space Analysis) capabilities.
* **OS:** Windows (required for `win32com` CATIA interaction).

### Installation
1. **Clone the repository:**
   ```bash
   git clone [https://github.com/Sushil-013/SCANIA-HITL-Framework.git](https://github.com/Sushil-013/SCANIA-HITL-Framework.git)
   cd SCANIA-HITL-Framework