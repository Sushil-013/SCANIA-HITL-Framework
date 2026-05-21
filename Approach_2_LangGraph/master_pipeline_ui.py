try:
    from .pipeline_stack.main_pipelines.master_pipeline_ui import main
except ImportError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from openai_pipeline.pipeline_stack.main_pipelines.master_pipeline_ui import main


if __name__ == "__main__":
    main()
