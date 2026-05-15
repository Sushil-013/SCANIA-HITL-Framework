# SCANIA HITL Framework: Geometric Validation

An automated Human-in-the-Loop (HITL) pipeline bridging 2D legacy engineering drawings to 3D CATIA V5 ENOVIA VPM models for early-stage clash detection. This project was developed as a Master's Thesis at Linköping University in collaboration with Scania CV AB.

## 🏗 System Architecture
The framework is divided into four distinct layers, orchestrated by a central SQLite database to ensure deterministic execution and data integrity:

* **Layer 1 (Perception):** Uses a tiled multi-agent Vision-Language Model (OpenAI GPT-4o) to extract GD&T and dimensional tolerances from 2D PDFs.
* **Layer 2 (CAD Extraction):** Interfaces with the CATIA V5 COM API to recursively walk the active 3D assembly and register published parameters.
* **Layer 3 (UI Mapping):** A graphical interface for the engineer to explicitly link 2D drawing semantics to 3D CAD parameters.
* **Layer 4 (Actuator & DMU):** Automatically drives the mapped CATIA parameters to their worst-case boundaries (MMC and LMC) and executes collision detection, pausing for human review upon detecting a clash.

## 🚀 Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/Sushil-013/SCANIA-HITL-Framework.git](https://github.com/Sushil-013/SCANIA-HITL-Framework.git)
   cd SCANIA-HITL-Framework