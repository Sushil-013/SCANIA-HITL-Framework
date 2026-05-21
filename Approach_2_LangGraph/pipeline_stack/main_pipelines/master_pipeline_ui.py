import os
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from ..helpers.tk_ui_sizing import apply_standard_window
except ImportError:
    import sys
    from pathlib import Path
    _workspace_root = str(Path(__file__).resolve().parents[2])
    if _workspace_root not in sys.path:
        sys.path.insert(0, _workspace_root)
    from pipeline_stack.helpers.tk_ui_sizing import apply_standard_window


DEFAULT_OUTPUT_ROOT = "Results/drawing_pipeline"
DEFAULT_LAYER3_OUTPUT_ROOT = "Results/layer3_dmu"
DEFAULT_PDF_DPI = "450"
DEFAULT_PDF_PAGE = "1"

MAIN_BG = "#EDF2F4"
SURFACE_BG = "#FFFFFF"
SURFACE_ALT_BG = "#F6FAFC"
HEADER_BG = "#1F3A4A"
HEADER_SUBTEXT = "#CFE0E8"
TEXT_COLOR = "#1F2933"
MUTED_TEXT = "#5B6770"
BORDER_COLOR = "#D6DEE5"
ACCENT_BLUE = "#2C6E7F"
ACCENT_BLUE_HOVER = "#255B69"
ACCENT_GOLD = "#C08A3E"
SUCCESS_GREEN = "#3E7C59"
DANGER_RED = "#B55246"
LOG_BG = SURFACE_ALT_BG
LOG_FG = TEXT_COLOR
LOG_ERROR = DANGER_RED
RUNNING_BLUE = "#2C6E7F"
PENDING_GREY = "#B8C3CB"
SKIPPED_GREY = "#8E9AA3"
COMPLETED_GREEN = "#3E7C59"
FAILED_RED = "#B55246"


def _open_path(path_text):
    if not path_text:
        return
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        return
    try:
        os.startfile(str(path))
    except Exception:
        pass


class PipelineProcessRunner:
    def __init__(self, on_output, on_finish):
        self.on_output = on_output
        self.on_finish = on_finish
        self.process = None
        self._queue = queue.Queue()
        self._threads = []

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, command, workdir):
        if self.is_running():
            raise RuntimeError("A pipeline run is already active.")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            command,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._threads = [
            threading.Thread(target=self._pump_stream, args=(self.process.stdout, False), daemon=True),
            threading.Thread(target=self._pump_stream, args=(self.process.stderr, True), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        threading.Thread(target=self._wait_for_exit, daemon=True).start()

    def stop(self):
        if self.is_running():
            self.process.terminate()

    def drain_queue(self):
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self.on_output(payload["text"], is_error=payload["is_error"])
            elif kind == "finish":
                self.on_finish(payload["returncode"])
                self.process = None
                self._threads = []

    def _pump_stream(self, stream, is_error):
        if stream is None:
            return
        try:
            for line in stream:
                self._queue.put(
                    (
                        "line",
                        {
                            "text": line.rstrip(),
                            "is_error": is_error,
                        },
                    )
                )
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _wait_for_exit(self):
        returncode = self.process.wait()
        self._queue.put(("finish", {"returncode": returncode}))


class MasterPipelineApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Drawing Pipeline Master UI")
        apply_standard_window(root, width=1550, height=980, min_width=1280, min_height=800)

        self.project_root = Path(__file__).resolve().parents[3]
        self.pipeline_script = Path(__file__).resolve().parents[2] / "5_pipeline.py"
        self.runner = PipelineProcessRunner(self._append_log, self._handle_process_finish)

        self.file_path_var = tk.StringVar()
        self.output_folder_var = tk.StringVar(value=str((self.project_root / DEFAULT_OUTPUT_ROOT).resolve()))
        self.layer3_output_root_var = tk.StringVar(value=str((self.project_root / DEFAULT_LAYER3_OUTPUT_ROOT).resolve()))
        self.pdf_dpi_var = tk.StringVar(value=DEFAULT_PDF_DPI)
        self.pdf_page_var = tk.StringVar(value=DEFAULT_PDF_PAGE)
        self.status_var = tk.StringVar(value="Ready. Select a PDF drawing or image to begin.")
        self.stage_summary_var = tk.StringVar(
            value="The master UI now starts one full pipeline path. Layer 2 and Layer 3 choices appear inside the review popup."
        )
        self.run_state_var = tk.StringVar(value="Ready")
        self.current_stage_var = tk.StringVar(value="Waiting to start")
        self.runtime_var = tk.StringVar(value="00:00")
        self.last_result_var = tk.StringVar(value="No run yet")
        self.live_summary_var = tk.StringVar(value="No run started.")
        self.plan_summary_var = tk.StringVar(
            value=(
                "1. Prepare the drawing and run extraction.\n"
                "2. Open the human review popup.\n"
                "3. Continue to Layer 2 and Layer 3 based on review choices.\n"
                "4. Save outputs and show the detailed log below."
            )
        )
        self.stage_rows = {}
        self.stage_status = {}
        self.summary_events = []
        self.run_started_at = None
        self.current_running_stage_key = None
        self.stage_sequence = [
            ("input", "1. Input / Extraction"),
            ("review", "2. Human Review"),
            ("layer2_extract", "3. Layer 2 Parameter Extract"),
            ("layer2_link", "4. Layer 2 Dimension Link"),
            ("layer3", "5. Layer 3 DMU"),
        ]

        self.file_path_var.trace_add("write", lambda *_args: self._sync_input_state())

        self._configure_styles()
        self._build_ui()
        self._sync_input_state()
        self._reset_stage_statuses()
        self._poll_runner()
        self._tick_runtime()

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.root.configure(bg=MAIN_BG)

        style.configure("App.TFrame", background=MAIN_BG)
        style.configure("Surface.TFrame", background=SURFACE_ALT_BG)
        style.configure("Card.TFrame", background=SURFACE_BG)

        style.configure(
            "Card.TLabelframe",
            background=SURFACE_BG,
            bordercolor=BORDER_COLOR,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            font=("Segoe UI", 10, "bold"),
        )

        style.configure(
            "App.TNotebook",
            background=MAIN_BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "App.TNotebook.Tab",
            background="#D9E4EA",
            foreground=TEXT_COLOR,
            padding=(16, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "App.TNotebook.Tab",
            background=[("selected", ACCENT_BLUE), ("active", "#C8D8DF")],
            foreground=[("selected", "#FFFFFF"), ("active", TEXT_COLOR)],
        )

        style.configure(
            "TEntry",
            fieldbackground=SURFACE_BG,
            bordercolor=BORDER_COLOR,
            lightcolor=BORDER_COLOR,
            darkcolor=BORDER_COLOR,
            foreground=TEXT_COLOR,
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE_BG,
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            bordercolor=BORDER_COLOR,
            lightcolor=BORDER_COLOR,
            darkcolor=BORDER_COLOR,
            arrowsize=16,
            padding=4,
        )
        style.configure(
            "TSpinbox",
            fieldbackground=SURFACE_BG,
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            bordercolor=BORDER_COLOR,
            lightcolor=BORDER_COLOR,
            darkcolor=BORDER_COLOR,
            arrowsize=14,
            padding=4,
        )

        style.configure(
            "Primary.TButton",
            background=ACCENT_BLUE,
            foreground="#FFFFFF",
            bordercolor=ACCENT_BLUE,
            focusthickness=0,
            focuscolor=ACCENT_BLUE,
            padding=(14, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", ACCENT_BLUE_HOVER), ("disabled", "#AABBC4")],
            foreground=[("disabled", "#F4F7F9")],
        )

        style.configure(
            "Secondary.TButton",
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            bordercolor=BORDER_COLOR,
            focusthickness=0,
            focuscolor=BORDER_COLOR,
            padding=(12, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#E6EEF2"), ("disabled", "#F3F6F8")],
            foreground=[("disabled", "#97A6AF")],
        )

        style.configure(
            "Danger.TButton",
            background=DANGER_RED,
            foreground="#FFFFFF",
            bordercolor=DANGER_RED,
            focusthickness=0,
            focuscolor=DANGER_RED,
            padding=(12, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#964239"), ("disabled", "#D8B4AE")],
            foreground=[("disabled", "#FFF6F4")],
        )

        style.configure(
            "Success.TButton",
            background=SUCCESS_GREEN,
            foreground="#FFFFFF",
            bordercolor=SUCCESS_GREEN,
            focusthickness=0,
            focuscolor=SUCCESS_GREEN,
            padding=(12, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Success.TButton",
            background=[("active", "#356A4C"), ("disabled", "#B9D0C2")],
            foreground=[("disabled", "#F5FBF7")],
        )

    def _build_ui(self):
        header = tk.Frame(self.root, bg=HEADER_BG, height=82)
        header.pack(fill="x")
        header.pack_propagate(False)

        # Header left side with title
        header_left = tk.Frame(header, bg=HEADER_BG)
        header_left.pack(side="left", fill="both", expand=True)

        title = tk.Label(
            header_left,
            text="Drawing Pipeline Master UI",
            bg=HEADER_BG,
            fg="#FFFFFF",
            font=("Segoe UI", 18, "bold"),
            anchor="w",
            padx=18,
        )
        title.pack(fill="x", pady=(14, 0))

        subtitle = tk.Label(
            header_left,
            text="Safe single-run wrapper around the current pipeline. Review keeps the downstream choices inside the small popup.",
            bg=HEADER_BG,
            fg=HEADER_SUBTEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=18,
        )
        subtitle.pack(fill="x", pady=(2, 12))

        # Prerequisites button in header right side
        header_right = tk.Frame(header, bg=HEADER_BG)
        header_right.pack(side="right", padx=18, pady=20)

        prereq_btn = tk.Button(
            header_right,
            text="View Prerequisites",
            bg=ACCENT_GOLD,
            fg="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._show_prerequisites,
        )
        prereq_btn.pack()

        main = ttk.Frame(self.root, padding=12, style="App.TFrame")
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        self._build_sidebar(main)
        self._build_workspace(main)

        footer = tk.Frame(self.root, bg="#DDE6EC", height=34)
        footer.pack(fill="x")
        footer.pack_propagate(False)
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg="#DDE6EC",
            fg=TEXT_COLOR,
            font=("Segoe UI", 10),
            anchor="w",
            padx=12,
        ).pack(fill="x", pady=6)

    def _build_sidebar(self, parent):
        sidebar = ttk.Frame(parent, padding=(0, 0, 12, 0), style="App.TFrame")
        sidebar.grid(row=0, column=0, rowspan=2, sticky="ns")

        pipeline_box = ttk.LabelFrame(sidebar, text="Pipeline Stages", padding=12, style="Card.TLabelframe")
        pipeline_box.pack(fill="x")

        for key, label_text in self.stage_sequence:
            row = tk.Frame(pipeline_box, bg=SURFACE_BG)
            row.pack(fill="x", pady=4)
            light = tk.Label(row, width=2, bg=PENDING_GREY, relief="flat")
            light.pack(side="left", padx=(0, 8), ipadx=2, ipady=4)
            text_box = tk.Frame(row, bg=SURFACE_BG)
            text_box.pack(side="left", fill="x", expand=True)
            title = tk.Label(text_box, text=label_text, bg=SURFACE_BG, fg=TEXT_COLOR, font=("Segoe UI", 10))
            title.pack(anchor="w")
            status = tk.Label(text_box, text="Pending", bg=SURFACE_BG, fg=MUTED_TEXT, font=("Segoe UI", 8))
            status.pack(anchor="w")
            self.stage_rows[key] = {"light": light, "status": status, "title": title}

        action_box = ttk.LabelFrame(sidebar, text="Actions", padding=12, style="Card.TLabelframe")
        action_box.pack(fill="x", pady=(12, 0))

        ttk.Label(action_box, text="Run State", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(action_box, textvariable=self.run_state_var).pack(anchor="w", pady=(0, 8))
        ttk.Label(action_box, text="Runtime", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(action_box, textvariable=self.runtime_var).pack(anchor="w", pady=(0, 10))

        self.run_button = ttk.Button(action_box, text="Run Pipeline", command=self._run_pipeline, style="Primary.TButton")
        self.run_button.pack(fill="x", pady=(0, 8))

        self.stop_button = ttk.Button(action_box, text="Stop Current Run", command=self._stop_pipeline, style="Danger.TButton")
        self.stop_button.pack(fill="x", pady=8)
        self.stop_button.state(["disabled"])

        ttk.Button(action_box, text="Open Output Folder", command=self._open_output_folder, style="Secondary.TButton").pack(fill="x", pady=8)
        ttk.Button(action_box, text="Open Layer 3 Folder", command=self._open_layer3_folder, style="Secondary.TButton").pack(fill="x", pady=8)

        note_box = ttk.LabelFrame(sidebar, text="Review Note", padding=12, style="Card.TLabelframe")
        note_box.pack(fill="x", pady=(12, 0))
        ttk.Label(
            note_box,
            text=(
                "The master UI now launches one clean pipeline path. "
                "The review popup decides whether Layer 2 and Layer 3 continue."
            ),
            wraplength=250,
            justify="left",
        ).pack(fill="x")

    def _build_workspace(self, parent):
        config_card = ttk.LabelFrame(parent, text="Run Setup", padding=12, style="Card.TLabelframe")
        config_card.grid(row=0, column=1, sticky="ew")
        for col in range(5):
            config_card.columnconfigure(col, weight=1 if col in (1, 3) else 0)

        ttk.Label(config_card, text="Drawing File").grid(row=0, column=0, sticky="w", pady=6)
        self.file_entry = ttk.Entry(config_card, textvariable=self.file_path_var)
        self.file_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 8), pady=6)
        ttk.Button(config_card, text="Browse", command=self._browse_file, style="Secondary.TButton").grid(row=0, column=4, sticky="ew", pady=6)

        ttk.Label(config_card, text="Output Folder").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(config_card, textvariable=self.output_folder_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(8, 8), pady=6
        )
        ttk.Button(config_card, text="Browse", command=self._browse_output_folder, style="Secondary.TButton").grid(row=1, column=4, sticky="ew", pady=6)

        self.pdf_page_label = ttk.Label(config_card, text="PDF Page")
        self.pdf_page_label.grid(row=2, column=0, sticky="w", pady=6)
        self.pdf_page_spin = ttk.Spinbox(config_card, from_=1, to=999, textvariable=self.pdf_page_var, width=8)
        self.pdf_page_spin.grid(row=2, column=1, sticky="w", padx=(8, 8), pady=6)

        self.pdf_dpi_label = ttk.Label(config_card, text="PDF DPI")
        self.pdf_dpi_label.grid(row=2, column=2, sticky="w", pady=6)
        self.pdf_dpi_combo = ttk.Combobox(
            config_card,
            textvariable=self.pdf_dpi_var,
            values=("300", "450", "600"),
            state="readonly",
            width=10,
        )
        self.pdf_dpi_combo.grid(row=2, column=3, sticky="w", padx=(8, 8), pady=6)

        stage_card = ttk.LabelFrame(parent, text="Pipeline Workspace", padding=12, style="Card.TLabelframe")
        stage_card.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        stage_card.columnconfigure(0, weight=1)
        stage_card.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(stage_card, style="App.TNotebook")
        notebook.grid(row=0, column=0, sticky="nsew")

        overview_tab = ttk.Frame(notebook, padding=12, style="Surface.TFrame")
        log_tab = ttk.Frame(notebook, padding=12, style="Surface.TFrame")
        notebook.add(overview_tab, text="Overview")
        notebook.add(log_tab, text="Run Log")

        self._build_overview_tab(overview_tab)
        self._build_log_tab(log_tab)

    def _build_overview_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        status_box = ttk.LabelFrame(parent, text="Live Run Status", padding=12, style="Card.TLabelframe")
        status_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        status_box.columnconfigure(0, weight=1)
        status_box.columnconfigure(1, weight=1)

        ttk.Label(status_box, text="Run State", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.run_state_var).grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(status_box, text="Current Stage", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(status_box, textvariable=self.current_stage_var, wraplength=260, justify="left").grid(
            row=1, column=1, sticky="w", pady=(0, 8)
        )
        ttk.Label(status_box, text="Runtime", font=("Segoe UI", 9, "bold")).grid(row=2, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.runtime_var).grid(row=3, column=0, sticky="w", pady=(0, 8))
        ttk.Label(status_box, text="Last Result", font=("Segoe UI", 9, "bold")).grid(row=2, column=1, sticky="w")
        ttk.Label(status_box, textvariable=self.last_result_var, wraplength=260, justify="left").grid(
            row=3, column=1, sticky="w", pady=(0, 8)
        )
        ttk.Separator(status_box).grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(status_box, text="Latest Update", font=("Segoe UI", 9, "bold")).grid(row=5, column=0, sticky="w")
        ttk.Label(status_box, textvariable=self.live_summary_var, wraplength=560, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w"
        )

        plan_box = ttk.LabelFrame(parent, text="What Will Happen In This Run", padding=12, style="Card.TLabelframe")
        plan_box.grid(row=0, column=1, sticky="nsew")
        ttk.Label(plan_box, textvariable=self.plan_summary_var, justify="left", wraplength=520).pack(fill="x")

        summary_box = ttk.LabelFrame(parent, text="Simplified Run Summary", padding=12, style="Card.TLabelframe")
        summary_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        summary_box.columnconfigure(0, weight=1)
        summary_box.rowconfigure(0, weight=1)

        self.summary_listbox = tk.Listbox(
            summary_box,
            height=10,
            font=("Segoe UI", 10),
            background=SURFACE_BG,
            foreground=TEXT_COLOR,
            activestyle="none",
            highlightthickness=0,
            borderwidth=0,
        )
        self.summary_listbox.grid(row=0, column=0, sticky="nsew")
        summary_scroll = ttk.Scrollbar(summary_box, orient="vertical", command=self.summary_listbox.yview)
        summary_scroll.grid(row=0, column=1, sticky="ns")
        self.summary_listbox.configure(yscrollcommand=summary_scroll.set)
        self._refresh_summary_listbox()

    def _build_log_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            parent,
            wrap="word",
            background=LOG_BG,
            foreground=LOG_FG,
            insertbackground=LOG_FG,
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("stderr", foreground=LOG_ERROR)
        self.log_text.tag_configure("stdout", foreground=LOG_FG)

        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def _reset_stage_statuses(self):
        self.stage_status = {}
        self.current_running_stage_key = None
        for key, _label_text in self.stage_sequence:
            self.stage_status[key] = "pending"
            self._render_stage_status(key, "pending", "Pending")

    def _render_stage_status(self, key, status, detail_text):
        row = self.stage_rows.get(key)
        if not row:
            return
        color_map = {
            "pending": PENDING_GREY,
            "running": RUNNING_BLUE,
            "completed": COMPLETED_GREEN,
            "skipped": SKIPPED_GREY,
            "failed": FAILED_RED,
        }
        text_map = {
            "pending": "Pending",
            "running": "Running",
            "completed": "Completed",
            "skipped": "Skipped",
            "failed": "Failed",
        }
        row["light"].configure(bg=color_map.get(status, PENDING_GREY))
        row["status"].configure(text=detail_text or text_map.get(status, "Pending"))

    def _set_stage_status(self, key, status, detail_text=None):
        if key not in self.stage_status:
            return
        self.stage_status[key] = status
        if status == "running":
            self.current_running_stage_key = key
        elif self.current_running_stage_key == key and status in ("completed", "skipped", "failed"):
            self.current_running_stage_key = None
        self._render_stage_status(key, status, detail_text)

    def _push_summary_event(self, text):
        if not text:
            return
        cleaned = str(text).strip()
        if not cleaned:
            return
        self.live_summary_var.set(cleaned)
        self.summary_events.append(cleaned)
        self.summary_events = self.summary_events[-12:]
        self._refresh_summary_listbox()

    def _refresh_summary_listbox(self):
        if not hasattr(self, "summary_listbox"):
            return
        self.summary_listbox.delete(0, "end")
        for line in self.summary_events[-12:]:
            self.summary_listbox.insert("end", line)

    def _mark_current_stage_failed(self):
        if self.current_running_stage_key:
            self._set_stage_status(self.current_running_stage_key, "failed", "Failed")

    def _interpret_log_line(self, text, is_error=False):
        cleaned = str(text or "").strip()
        if not cleaned:
            return

        if "Layer 1 Stage 01/08" in cleaned:
            self._set_stage_status("input", "running", "Preparing PDF/image")
            self.current_stage_var.set("Layer 1 - Preparing PDF/image")
            self._push_summary_event("Layer 1 started: preparing the drawing image.")
        elif "Layer 1 Stage 02/08" in cleaned:
            self._set_stage_status("input", "running", "Running extraction")
            self.current_stage_var.set("Layer 1 - OpenAI extraction")
            self._push_summary_event("Extraction started.")
        elif "Layer 1 Stage 03/08" in cleaned or "Layer 1 Stage 04/08" in cleaned or "Layer 1 Stage 05/08" in cleaned or "Layer 1 Stage 06/08" in cleaned or "Layer 1 Stage 07/08" in cleaned:
            self._set_stage_status("input", "running", "Refining and saving")
            self.current_stage_var.set("Layer 1 - Refinement and output")
            self._push_summary_event(cleaned)
        elif "Layer 1 Stage 08/08" in cleaned:
            self._set_stage_status("input", "completed", "Completed")
            self._set_stage_status("review", "running", "Waiting for review")
            self.current_stage_var.set("Layer 1 review")
            self._push_summary_event("Review popup opened.")
        elif "Layer 1 Completed 08/08" in cleaned:
            self._set_stage_status("review", "completed", "Completed")
            self.current_stage_var.set("Layer 1 complete")
            self._push_summary_event("Layer 1 finished.")
        elif "Layer 2 Stage 01/03" in cleaned or "Layer 2 Result - Published Parameters" in cleaned:
            if "skipped" in cleaned.lower():
                self._set_stage_status("layer2_extract", "skipped", "Skipped")
                self._push_summary_event("Layer 2 published-parameter extraction skipped.")
            else:
                status_text = "Completed" if "Result -" in cleaned else "Running"
                self._set_stage_status("layer2_extract", "completed" if "Result -" in cleaned else "running", status_text)
                self.current_stage_var.set("Layer 2 published-parameter extraction")
                self._push_summary_event("Layer 2 published-parameter extraction active.")
        elif "Layer 2 Stage 03/03" in cleaned or "Layer 2 Result - Dimension Linking" in cleaned:
            if "skipped" in cleaned.lower():
                self._set_stage_status("layer2_link", "skipped", "Skipped")
                self._push_summary_event("Layer 2 dimension link UI skipped.")
            else:
                status_text = "Completed" if "Result -" in cleaned else "Running"
                self._set_stage_status("layer2_link", "completed" if "Result -" in cleaned else "running", status_text)
                self.current_stage_var.set("Layer 2 dimension link")
                self._push_summary_event("Layer 2 dimension link UI active.")
        elif "Layer 2 Completed 03/03" in cleaned:
            if self.stage_status.get("layer2_extract") == "running":
                self._set_stage_status("layer2_extract", "completed", "Completed")
            if self.stage_status.get("layer2_link") == "running":
                self._set_stage_status("layer2_link", "completed", "Completed")
            self._push_summary_event("Layer 2 finished.")
        elif "Layer 3 Result - Sweep Summary" in cleaned:
            self._set_stage_status("layer3", "completed", "Completed")
            self.current_stage_var.set("Layer 3 complete")
            self._push_summary_event("Layer 3 DMU completed.")
        elif "CATIA Layer 3 was skipped" in cleaned or ("Layer 3" in cleaned and "skipped" in cleaned.lower()):
            self._set_stage_status("layer3", "skipped", "Skipped")
            self._push_summary_event("Layer 3 skipped.")
        elif "CATIA Layer 3:" in cleaned and "error" not in cleaned.lower():
            self._set_stage_status("layer3", "running", "Running")
            self.current_stage_var.set("Layer 3 DMU")
        elif cleaned.startswith("Pipeline completed."):
            self._push_summary_event("Pipeline completed.")
        elif "Pipeline ended with exit code" in cleaned or is_error:
            self._mark_current_stage_failed()
            self._push_summary_event(cleaned)

    def _show_prerequisites(self):
        popup = tk.Toplevel(self.root)
        popup.title("Prerequisites — Check Before Running")
        popup.geometry("720x300")
        popup.resizable(False, False)
        popup.configure(bg=SURFACE_BG)
        popup.transient(self.root)
        popup.grab_set()

        # Center the popup
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (720 // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (300 // 2)
        popup.geometry(f"+{x}+{y}")

        header = tk.Label(
            popup,
            text="Check these items before running the pipeline:",
            bg=SURFACE_BG,
            fg=TEXT_COLOR,
            font=("Segoe UI", 11, "bold"),
        )
        header.pack(pady=(16, 12), padx=20, anchor="w")

        prereq_items = [
            ("1. Drawing File Ready", "Have your engineering drawing (PDF or image) ready"),
            ("2. OpenAI API Key", "Ensure OPENAI_API_KEY environment variable is set"),
            ("3. CATIA Running", "If using Layer 2/3, CATIA V5 must be open with target assembly"),
            ("4. Python Environment", "Virtual environment activated with dependencies installed"),
            ("5. Output Folder", "Ensure output folder is accessible with write permissions"),
        ]

        for title, desc in prereq_items:
            row = tk.Frame(popup, bg=SURFACE_BG)
            row.pack(fill="x", padx=20, pady=4)
            tk.Label(row, text=title, bg=SURFACE_BG, fg=ACCENT_BLUE, font=("Segoe UI", 10, "bold"), width=22, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg=SURFACE_BG, fg=MUTED_TEXT, font=("Segoe UI", 10), anchor="w").pack(side="left")

        btn_frame = tk.Frame(popup, bg=SURFACE_BG)
        btn_frame.pack(pady=20)
        tk.Button(
            btn_frame,
            text="Got it!",
            bg=ACCENT_BLUE,
            fg="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=20,
            pady=8,
            command=popup.destroy,
        ).pack()

    def _browse_file(self):
        selected = filedialog.askopenfilename(
            title="Select drawing PDF or image",
            filetypes=(
                ("Drawing files", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                ("PDF", "*.pdf"),
                ("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                ("All files", "*.*"),
            ),
            initialdir=str(self.project_root),
        )
        if selected:
            self.file_path_var.set(selected)

    def _browse_output_folder(self):
        selected = filedialog.askdirectory(title="Select output folder", initialdir=str(self.project_root))
        if selected:
            self.output_folder_var.set(selected)

    def _open_output_folder(self):
        _open_path(self.output_folder_var.get())

    def _open_layer3_folder(self):
        _open_path(self.layer3_output_root_var.get())

    def _sync_input_state(self):
        file_text = self.file_path_var.get().strip().lower()
        is_pdf = file_text.endswith(".pdf")
        widgets = (self.pdf_page_label, self.pdf_page_spin, self.pdf_dpi_label, self.pdf_dpi_combo)
        for widget in widgets:
            if is_pdf:
                widget.grid()
            else:
                widget.grid_remove()

    def _validate(self):
        file_path = Path(self.file_path_var.get().strip().strip('"')).expanduser()
        if not file_path.exists():
            messagebox.showerror("Missing Drawing", "Select a valid PDF drawing or image file.")
            return None

        output_folder = Path(self.output_folder_var.get().strip().strip('"')).expanduser()
        if not output_folder:
            messagebox.showerror("Missing Output Folder", "Select a valid output folder.")
            return None

        try:
            pdf_page = max(1, int(self.pdf_page_var.get().strip() or "1"))
        except ValueError:
            messagebox.showerror("Invalid PDF Page", "PDF page must be a whole number.")
            return None

        try:
            pdf_dpi = max(72, int(self.pdf_dpi_var.get().strip() or DEFAULT_PDF_DPI))
        except ValueError:
            messagebox.showerror("Invalid DPI", "PDF DPI must be a whole number.")
            return None

        return {
            "file_path": file_path.resolve(),
            "output_folder": output_folder.resolve(),
            "pdf_page": pdf_page,
            "pdf_dpi": pdf_dpi,
        }

    def _build_command(self, validated):
        return [
            sys.executable,
            str(self.pipeline_script),
            "--image",
            str(validated["file_path"]),
            "--result_folder",
            str(validated["output_folder"]),
            "--pdf_page",
            str(validated["pdf_page"]),
            "--pdf_dpi",
            str(validated["pdf_dpi"]),
            "--model_strategy",
            "hybrid",
            "--extraction_mode",
            "multi",
            "--crop_preprocessing_mode",
            "dynamic",
            "--value_verification_mode",
            "local",
            "--bbox_refinement_mode",
            "local",
            "--human_review_mode",
            "coverage",
            "--catia_layer3_mode",
            "skip",
            "--catia_layer3_output_root",
            str(Path(self.layer3_output_root_var.get()).expanduser().resolve()),
        ]

    def _run_pipeline(self):
        validated = self._validate()
        if validated is None:
            return

        self._reset_stage_statuses()
        self.summary_events = []
        self._refresh_summary_listbox()
        self.run_started_at = time.time()
        self.run_state_var.set("Running")
        self.current_stage_var.set("Starting pipeline")
        self.last_result_var.set("Run in progress")
        self.live_summary_var.set("Launching pipeline...")

        command = self._build_command(validated)
        self._append_log("Launching pipeline with explicit stage choices...", is_error=False)
        self._append_log(
            "Command: " + " ".join('"{}"'.format(part) if " " in part else part for part in command),
            is_error=False,
        )

        try:
            Path(validated["output_folder"]).mkdir(parents=True, exist_ok=True)
            Path(self.layer3_output_root_var.get()).expanduser().resolve().mkdir(parents=True, exist_ok=True)
            self.runner.start(command, self.project_root)
        except Exception as exc:
            messagebox.showerror("Launch Failed", str(exc))
            return

        self.run_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status_var.set("Pipeline is running. The review popup will handle downstream Layer 2 and Layer 3 choices.")

    def _stop_pipeline(self):
        if not self.runner.is_running():
            return
        if not messagebox.askyesno("Stop Run", "Stop the current pipeline run?"):
            return
        self.runner.stop()
        self.status_var.set("Stopping pipeline...")
        self.run_state_var.set("Stopping")
        self._append_log("Stop requested by user.", is_error=True)

    def _append_log(self, text, is_error=False):
        if not text:
            return
        self._interpret_log_line(text, is_error=is_error)
        self.log_text.insert("end", text + "\n", "stderr" if is_error else "stdout")
        self.log_text.see("end")
        self.log_text.update_idletasks()

    def _handle_process_finish(self, returncode):
        self.run_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        if returncode == 0:
            self.status_var.set("Pipeline completed successfully.")
            self.run_state_var.set("Completed")
            self.last_result_var.set("Success")
            self.current_stage_var.set("Run finished")
            for key, _label in self.stage_sequence:
                if self.stage_status.get(key) == "running":
                    self._set_stage_status(key, "completed", "Completed")
                elif self.stage_status.get(key) == "pending":
                    self._set_stage_status(key, "skipped", "Skipped")
            self._append_log("Pipeline finished successfully.", is_error=False)
        else:
            self.status_var.set("Pipeline ended with exit code {}.".format(returncode))
            self.run_state_var.set("Failed")
            self.last_result_var.set("Exit code {}".format(returncode))
            self.current_stage_var.set("Run failed")
            self._mark_current_stage_failed()
            self._append_log("Pipeline ended with exit code {}.".format(returncode), is_error=True)
        self.run_started_at = None

    def _poll_runner(self):
        self.runner.drain_queue()
        self.root.after(150, self._poll_runner)

    def _tick_runtime(self):
        if self.run_started_at is None:
            if self.run_state_var.get() in ("Ready", "Completed", "Failed"):
                pass
        else:
            elapsed = max(0, int(time.time() - self.run_started_at))
            minutes = elapsed // 60
            seconds = elapsed % 60
            self.runtime_var.set("{:02d}:{:02d}".format(minutes, seconds))
        self.root.after(1000, self._tick_runtime)


def main():
    root = tk.Tk()
    MasterPipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
