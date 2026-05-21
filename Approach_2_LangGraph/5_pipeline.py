try:
    from .pipeline_stack.main_pipelines.interactive_pipeline import main
except ImportError:
    import sys
    from pathlib import Path

    _root = str(Path(__file__).resolve().parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from pipeline_stack.main_pipelines.interactive_pipeline import main


if __name__ == "__main__":
    main()
