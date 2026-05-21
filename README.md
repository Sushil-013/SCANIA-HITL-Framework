# 🚛 SCANIA HITL Framework

This repository contains two pipeline implementations for geometric validation between 2D drawings and 3D CATIA models:

- `Approach_1_SQLite_Tkinter`  
  SQLite + Tkinter based HITL workflow.
- `Approach_2_LangGraph`  
  OpenAI/LangGraph-oriented pipeline with updated runtime structure.

## Repository Structure

```text
SCANIA-HITL-Framework/
├── Approach_1_SQLite_Tkinter/
│   └── README.md
└── Approach_2_LangGraph/
    └── README.md
```

## Which README to Use

- For the classic Tkinter + SQLite flow, read:  
  `Approach_1_SQLite_Tkinter/README.md`
- For the LangGraph/OpenAI flow, read:  
  `Approach_2_LangGraph/README.md`

## Common Requirements

- Windows environment for CATIA automation workflows.
- Python 3.10+ recommended.
- CATIA/ENOVIA access required for CAD extraction/actuation stages.

## Notes

This is the common top-level README for both pipelines.  
Each pipeline folder keeps its own detailed setup and run instructions.
