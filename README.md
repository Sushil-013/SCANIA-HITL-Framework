# 🚛 SCANIA HITL Framework: Geometric Validation

An exploratory **Human-in-the-Loop (HITL)** framework bridging 2D legacy engineering drawings to 3D **CATIA V5 ENOVIA VPM** models for early-stage clash detection. 

This software was developed as part of a Master's Thesis at **Linköping University (LiU)** in collaboration with **Scania CV AB**. It serves as an automated pipeline to solve the "Semantic Gap" between disconnected 2D visual manufacturing data and deterministic parametric 3D CAD environments.

---

## 🚀 Quick Start for Scania Engineers: SCANVA 3.0 (App)

For ease of use within the Scania enterprise environment, the primary architecture of this framework has been compiled into a standalone Windows executable Application named **SCANVA 3.0**. 

**No Python installation, virtual environments, or terminal commands are required.** 👉 **[Download SCANVA 3.0 from GitHub Releases](../../releases)**

*(Note: The executable securely integrates with the Windows Credential Manager to handle API keys. For full instructions on running the app, please read the [Primary Approach README](./Approach_1_SQLite_Tkinter/README.md)).*

---

## 🔬 Research Methodology: Two Exploratory Approaches

To thoroughly evaluate the best method for bridging this gap, this repository is structured as a **Monorepo** containing two distinct system architectures. Both approaches were developed, tested, and evaluated during our research.

### 📂 [APPROACH 1: SQLite & Tkinter Pipeline (Primary)](./Approach_1_SQLite_Tkinter)
*Developed by Sushil Krishna*

This is the primary pipeline selected for the final thesis case study (and the architecture behind the SCANVA 3.0 app). It prioritizes deterministic safety, strict Human-in-the-Loop (HITL) UI gates, multi-view coordinate camera targeting, and a central **SQLite Database** to air-gap probabilistic AI from the live CAD environment.

### 📂 [APPROACH 2: OpenAI Pipeline & LangGraph (Alternate)](./Approach_2_LangGraph)
*Developed by Sajath Salim*

This alternative approach explores high AI autonomy. It utilizes **LangGraph** for node-based state orchestration and features a bundled, localized **CRAFT runtime** for precise bounding-box refinement and value verification without relying on external databases.

---

## 💻 Developer Guide (Source Code Setup)

If you are an academic examiner or developer wishing to inspect, modify, or run the raw Python source code, please note the global prerequisites:
* **Operating System:** Windows 10 or 11 (Strictly required for the PyWin32 COM API interaction with CATIA).
* **Python:** Version `3.10.x` or higher.
* **CAD Software:** CATIA V5 or ENOVIA VPM installed locally, with an active **SPA (Space Analysis)** license.
* **API Access:** An active OpenAI API key.

Because each pipeline relies on different architectural paradigms, they operate completely independently. Navigate to the specific folder of the approach you wish to run for exact setup instructions:

* **For the primary SQLite/Tkinter code:** Read [`Approach_1_SQLite_Tkinter/README.md`](./Approach_1_SQLite_Tkinter/README.md)
* **For the alternate LangGraph/CRAFT code:** Read [`Approach_2_LangGraph/README.md`](./Approach_2_LangGraph/README.md)

---

## 📖 Comprehensive Documentation

For a comprehensive, step-by-step User Manual on how to operate the HITL UIs, map parameters, and interpret the final HTML engineering reports, please visit our overarching project Wiki:

👉 **[SCANIA HITL Framework - Official GitHub Wiki](https://github.com/Sushil-013/SCANIA-HITL-Framework/wiki)**

*(The Wiki also includes deep dives into the system architecture, mathematical boundary calculations for MMC/LMC, database schemas, and CATIA API integration logic).*
