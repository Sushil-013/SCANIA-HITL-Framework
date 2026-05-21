from pathlib import Path

try:
    from ..helpers.drawing_preprocessor import load_input_drawing
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.drawing_preprocessor import load_input_drawing


def prepare_pdf_clarity_image(image_path, output_dir, output_stem, pdf_page=1, pdf_dpi=450):
    source_path = Path(image_path).resolve()
    resolved_output_dir = Path(output_dir).resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    if source_path.suffix.lower() != ".pdf":
        return {
            "status": "skipped",
            "source_type": "image",
            "source_path": str(source_path),
            "prepared_image_path": str(source_path),
            "raw_render_path": None,
            "enhanced_render_path": None,
            "pdf_page": None,
            "pdf_dpi": None,
        }

    raw_render = load_input_drawing(str(source_path), pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=False)
    enhanced_render = load_input_drawing(str(source_path), pdf_page=pdf_page, pdf_dpi=pdf_dpi, preprocess=True)
    raw_render_path = resolved_output_dir / "{}_pdf_raw.png".format(output_stem)
    enhanced_render_path = resolved_output_dir / "{}_pdf_high_clarity.png".format(output_stem)

    try:
        raw_render.save(raw_render_path, format="PNG", optimize=True)
        enhanced_render.save(enhanced_render_path, format="PNG", optimize=True)
    finally:
        raw_render.close()
        enhanced_render.close()

    return {
        "status": "saved",
        "source_type": "pdf",
        "source_path": str(source_path),
        "prepared_image_path": str(enhanced_render_path.resolve()),
        "raw_render_path": str(raw_render_path.resolve()),
        "enhanced_render_path": str(enhanced_render_path.resolve()),
        "pdf_page": pdf_page,
        "pdf_dpi": pdf_dpi,
    }
