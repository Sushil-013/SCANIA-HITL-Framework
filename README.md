# 🚛 SCANIA HITL Framework: Geometric Validation

An exploratory **Human-in-the-Loop (HITL)** framework bridging 2D legacy engineering drawings to 3D **CATIA V5 ENOVIA VPM** models for early-stage clash detection. 

This software was developed as part of a Master's Thesis at **Linköping University (LiU)** in collaboration with **Scania CV AB**. It serves as an automated pipeline to solve the "Semantic Gap" between disconnected 2D visual manufacturing data and deterministic parametric 3D CAD environments.

---

## 🔬 Research Methodology: Two Exploratory Approaches

To thoroughly evaluate the best method for bridging this gap, this repository is structured as a **Monorepo** containing two distinct system architectures. Both approaches were developed, tested, and evaluated during our research.

### 📂 [APPROACH 1: SQLite & Tkinter Pipeline (Primary)](./Approach_1_SQLite_Tkinter)
*Developed by Sushil Krishna*

This is the primary pipeline selected for the final thesis case study. It prioritizes deterministic safety, strict Human-in-the-Loop (HITL) UI gates, multi-view coordinate camera targeting, and a central **SQLite Database** to air-gap probabilistic AI from the live CAD environment.

### 📂 [APPROACH 2: OpenAI Pipeline & LangGraph (Alternate)](./Approach_2_LangGraph)
*Developed by Sajath Salim*

This alternative approach explores high AI autonomy. It utilizes **LangGraph** for node-based state orchestration and features a bundled, localized **CRAFT runtime** for precise bounding-box refinement and value verification without relying on external databases.

---

## 🛠️ Global Prerequisites

Regardless of which approach you are running, the following system requirements apply:
* **Operating System:** Windows 10 or 11 (Strictly required for the PyWin32 COM API interaction with CATIA).
* **Python:** Version `3.10.x` or higher.
* **CAD Software:** CATIA V5 or ENOVIA VPM installed locally, with an active **SPA (Space Analysis)** license required for the DMU clash detection module.
* **API Access:** An active OpenAI API key.

---

## 🚀 Where to Start

Because each pipeline relies on different architectural paradigms, they operate completely independently and maintain their own dependencies, virtual environments, and execution scripts. 

Please navigate to the specific folder of the approach you wish to run and read its dedicated `README.md` for step-by-step setup instructions:

* **For the classic Tkinter + SQLite flow, read:** [`Approach_1_SQLite_Tkinter/README.md`](./Approach_1_SQLite_Tkinter/README.md)
* **For the LangGraph/OpenAI flow, read:** [`Approach_2_LangGraph/README.md`](./Approach_2_LangGraph/README.md)

---

## 📖 Comprehensive Documentation

For a deep dive into the overarching system architecture, mathematical boundary calculations (MMC/LMC), database schemas, and CATIA API integration logic, please visit our comprehensive **[GitHub Wiki](https://github.com/Sushil-013/SCANIA-HITL-Framework/wiki)**.
