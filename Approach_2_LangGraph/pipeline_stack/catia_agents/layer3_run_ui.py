import argparse
import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise RuntimeError("Layer 3 run UI requires Pillow. Install it in the active Python environment.") from exc

try:
    from ..helpers.tk_ui_sizing import apply_standard_window
except ImportError:
    try:
        from openai_pipeline.pipeline_stack.helpers.tk_ui_sizing import apply_standard_window
    except ImportError:
        PIPELINE_STACK_ROOT = Path(__file__).resolve().parents[1]
        if str(PIPELINE_STACK_ROOT) not in sys.path:
            sys.path.insert(0, str(PIPELINE_STACK_ROOT))
        from helpers.tk_ui_sizing import apply_standard_window


def parse_args():
    parser = argparse.ArgumentParser(description="Open a Layer 3 DMU run preview window.")
    parser.add_argument("--results_json", required=True, type=str, help="Path to the run results.json file.")
    parser.add_argument("--run_summary_json", default=None, type=str, help="Optional path to run_summary.json.")
    return parser.parse_args()


def safe_open_path(path_text):
    if not path_text:
        return
    path = Path(path_text)
    if not path.exists():
        return
    os.startfile(str(path))


VIEW_ORDER = ("iso", "front", "top", "right")
VIEW_DISPLAY_NAMES = {
    "iso": "View 1",
    "front": "View 2",
    "top": "View 3",
    "right": "View 4",
}


class ImageZoomViewer:
    def __init__(self, parent, image_path, window_title):
        self.parent = parent
        self.image_path = Path(image_path).resolve()
        self.window = tk.Toplevel(parent)
        self.window.title(window_title)
        apply_standard_window(self.window, width=1320, height=920)

        self.source_image = Image.open(self.image_path).convert("RGB")
        self.photo = None
        self.scale = 1.0

        container = ttk.Frame(self.window, padding=10)
        container.grid(row=0, column=0, sticky="nsew")
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        header_var = tk.StringVar(value=str(self.image_path))
        ttk.Label(container, textvariable=header_var, anchor="w", justify="left", wraplength=1240).grid(
            row=0, column=0, sticky="ew", pady=(0, 8)
        )

        controls = ttk.Frame(container)
        controls.grid(row=1, column=0, sticky="new")
        for index in range(5):
            controls.columnconfigure(index, weight=0)

        ttk.Button(controls, text="Zoom In", command=lambda: self._change_zoom(1.25)).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(controls, text="Zoom Out", command=lambda: self._change_zoom(0.8)).grid(row=0, column=1, padx=6)
        ttk.Button(controls, text="100%", command=lambda: self._set_zoom(1.0)).grid(row=0, column=2, padx=6)
        ttk.Button(controls, text="Fit", command=self._fit_to_window).grid(row=0, column=3, padx=6)
        ttk.Button(controls, text="Open File", command=lambda: safe_open_path(self.image_path)).grid(row=0, column=4, padx=(6, 0))

        viewer_frame = ttk.Frame(container)
        viewer_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        viewer_frame.columnconfigure(0, weight=1)
        viewer_frame.rowconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        self.canvas = tk.Canvas(viewer_frame, background="#1d2030", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(viewer_frame, orient="vertical", command=self.canvas.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(viewer_frame, orient="horizontal", command=self.canvas.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_linux_wheel_up)
        self.canvas.bind("<Button-5>", self._on_linux_wheel_down)
        self.canvas.bind("<Double-Button-1>", lambda _event: self._fit_to_window())

        self.window.update_idletasks()
        self._fit_to_window()

    def _render(self):
        width = max(1, int(self.source_image.width * self.scale))
        height = max(1, int(self.source_image.height * self.scale))
        resized = self.source_image.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def _set_zoom(self, scale):
        self.scale = max(0.1, min(8.0, float(scale)))
        self._render()

    def _change_zoom(self, factor):
        self._set_zoom(self.scale * factor)

    def _fit_to_window(self):
        self.window.update_idletasks()
        canvas_width = max(1, self.canvas.winfo_width() or 1)
        canvas_height = max(1, self.canvas.winfo_height() or 1)
        fit_scale = min(canvas_width / max(1, self.source_image.width), canvas_height / max(1, self.source_image.height))
        self._set_zoom(min(1.0, fit_scale))

    def _on_mousewheel(self, event):
        factor = 1.1 if event.delta > 0 else 0.9
        self._change_zoom(factor)

    def _on_linux_wheel_up(self, _event):
        self._change_zoom(1.1)

    def _on_linux_wheel_down(self, _event):
        self._change_zoom(0.9)


class Layer3RunPreviewApp:
    def __init__(self, root, results_json_path, run_summary_json_path=None):
        self.root = root
        self.root.title("Layer 3 Run Results")
        apply_standard_window(self.root, width=1520, height=960)

        self.results_json_path = Path(results_json_path).resolve()
        self.run_summary_json_path = Path(run_summary_json_path).resolve() if run_summary_json_path else None
        self.results_payload = json.loads(self.results_json_path.read_text(encoding="utf-8"))
        if self.run_summary_json_path and self.run_summary_json_path.exists():
            self.run_summary_payload = json.loads(self.run_summary_json_path.read_text(encoding="utf-8"))
        else:
            self.run_summary_payload = {}

        self.result_rows = list(self.results_payload.get("results", []))
        self.row_index = {}
        self.image_refs = []
        self.current_view_paths = {}

        self.summary_text_var = tk.StringVar(value=self._build_summary_text())
        self.detail_text_var = tk.StringVar(value="Select a result row to load the 4 annotated views.")

        self._build_ui()
        self._populate_rows()
        if self.result_rows:
            first_item = self.tree.get_children()[0]
            self.tree.selection_set(first_item)
            self._on_row_selected()

    def _build_summary_text(self):
        payload = self.run_summary_payload or {}
        pieces = [
            "Run: {}".format(payload.get("run_label") or payload.get("run_id") or self.results_json_path.parent.name),
            "Dimension: {}".format(payload.get("dimension_id") or "nominal"),
            "Parameter: {}".format(payload.get("parameter_name") or payload.get("publication_name") or "n/a"),
            "Mode: {}".format(payload.get("tolerance_mode") or "nominal"),
            "Target: {}".format(payload.get("target_value_mm") if payload.get("target_value_mm") is not None else "nominal"),
            "Rows: {}".format(payload.get("result_row_count", len(self.result_rows))),
            "Clash: {}".format(payload.get("clash_count", 0)),
            "Contact: {}".format(payload.get("contact_count", 0)),
            "Clearance: {}".format(payload.get("clearance_count", 0)),
        ]
        return " | ".join(str(piece) for piece in pieces)

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=10)
        container.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=7)
        container.columnconfigure(1, weight=6)
        container.rowconfigure(1, weight=1)

        summary_label = ttk.Label(
            container,
            textvariable=self.summary_text_var,
            justify="left",
            anchor="w",
            wraplength=1400,
        )
        summary_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        left_frame = ttk.Frame(container)
        left_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(1, weight=1)

        action_frame = ttk.Frame(left_frame)
        action_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for index in range(3):
            action_frame.columnconfigure(index, weight=1)
        ttk.Button(action_frame, text="Open Folder", command=self._open_run_folder).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(action_frame, text="Open JSON", command=self._open_results_json).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(action_frame, text="Open TXT", command=self._open_results_txt).grid(row=0, column=2, sticky="ew", padx=(6, 0))

        columns = ("No", "Product1", "Product2", "Type", "Value", "Status", "Info", "Keep", "Comment", "Location")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings", height=24)
        headings = {
            "No": 50,
            "Product1": 160,
            "Product2": 160,
            "Type": 90,
            "Value": 75,
            "Status": 110,
            "Info": 90,
            "Keep": 70,
            "Comment": 130,
            "Location": 240,
        }
        for column in columns:
            self.tree.heading(column, text=column)
            self.tree.column(column, width=headings[column], anchor="w")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_row_selected())

        yscroll = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        right_frame = ttk.Frame(container)
        right_frame.grid(row=1, column=1, sticky="nsew")
        right_frame.columnconfigure(0, weight=1)
        right_frame.columnconfigure(1, weight=1)
        right_frame.rowconfigure(1, weight=1)
        right_frame.rowconfigure(2, weight=1)

        detail_label = ttk.Label(
            right_frame,
            textvariable=self.detail_text_var,
            justify="left",
            anchor="w",
            wraplength=680,
        )
        detail_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.image_labels = {}
        self.image_buttons = {}
        for index, view_name in enumerate(VIEW_ORDER):
            row = 1 + (index // 2)
            column = index % 2
            frame = ttk.LabelFrame(right_frame, text=VIEW_DISPLAY_NAMES.get(view_name, view_name.upper()), padding=6)
            frame.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            label = ttk.Label(frame, text="No image")
            label.grid(row=0, column=0, sticky="nsew")
            label.bind("<Button-1>", lambda _event, current_view=view_name: self._open_zoom_view(current_view))
            button = ttk.Button(frame, text="Open / Zoom", command=lambda current_view=view_name: self._open_zoom_view(current_view))
            button.grid(row=1, column=0, sticky="ew", pady=(6, 0))
            button.state(["disabled"])
            self.image_labels[view_name] = label
            self.image_buttons[view_name] = button

    def _populate_rows(self):
        for row in self.result_rows:
            item_id = "row_{}".format(row.get("No"))
            self.row_index[item_id] = row
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row.get("No", ""),
                    row.get("Product1", ""),
                    row.get("Product2", ""),
                    row.get("Type", ""),
                    row.get("Value", ""),
                    row.get("Status", ""),
                    row.get("Info", ""),
                    row.get("Keep", ""),
                    row.get("Comment", ""),
                    row.get("Location", ""),
                ),
            )

    def _open_run_folder(self):
        safe_open_path(self.results_json_path.parent)

    def _open_results_json(self):
        safe_open_path(self.results_json_path)

    def _open_results_txt(self):
        txt_path = self.results_json_path.parent / "results.txt"
        safe_open_path(txt_path)

    def _on_row_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        row = self.row_index.get(selected[0])
        if row is None:
            return
        self.detail_text_var.set(
            "Row {} | {} | {} | {} | Value {} | {}".format(
                row.get("No", ""),
                row.get("Type", ""),
                row.get("Product1", ""),
                row.get("Product2", ""),
                row.get("Value", ""),
                row.get("Location", "") or "No explicit location text",
            )
        )
        self._load_row_images(row)

    def _load_row_images(self, row):
        self.image_refs = []
        self.current_view_paths = {}
        views = row.get("Views") or {}
        for view_name, label in self.image_labels.items():
            button = self.image_buttons[view_name]
            image_path = views.get(view_name)
            if not image_path:
                label.configure(text="No image", image="")
                button.state(["disabled"])
                continue
            resolved_path = Path(image_path)
            if not resolved_path.exists():
                label.configure(text="Missing image", image="")
                button.state(["disabled"])
                continue
            self.current_view_paths[view_name] = resolved_path
            image = Image.open(resolved_path)
            target_width = max(460, int(label.winfo_width() or 0) - 12)
            target_height = max(320, int(label.winfo_height() or 0) - 12)
            image.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            image.close()
            label.configure(image=photo, text="")
            label.image = photo
            self.image_refs.append(photo)
            button.state(["!disabled"])

    def _open_zoom_view(self, view_name):
        image_path = self.current_view_paths.get(view_name)
        if not image_path:
            return
        selected = self.tree.selection()
        row_number = "?"
        if selected:
            row = self.row_index.get(selected[0]) or {}
            row_number = row.get("No", "?")
        ImageZoomViewer(
            self.root,
            image_path,
            "{} - Row {} - {}".format(
                VIEW_DISPLAY_NAMES.get(view_name, view_name.upper()),
                row_number,
                image_path.name,
            ),
        )


def run_layer3_run_preview_ui(results_json_path, run_summary_json_path=None):
    root = tk.Tk()
    app = Layer3RunPreviewApp(root, results_json_path, run_summary_json_path=run_summary_json_path)
    root.mainloop()
    return app


def main():
    args = parse_args()
    run_layer3_run_preview_ui(args.results_json, run_summary_json_path=args.run_summary_json)


if __name__ == "__main__":
    main()
