# -*- coding: utf-8 -*-
"""
SCANIA HITL FRAMEWORK — Master Control Center
==============================================
A unified CustomTkinter desktop application that orchestrates all four
pipeline agents from a single professional UI.

Architecture:
  ┌──────────────────────────────────────────────────────────┐
  │  Sidebar  │         Main Content Panel                   │
  │  (nav)    │  Agent-specific controls + status cards      │
  │           │                                              │
  │           ├──────────────────────────────────────────────┤
  │           │  Global Console Log  (read-only, live tail)  │
  └──────────────────────────────────────────────────────────┘

IMPORTANT — Child-script Tkinter popup compatibility
────────────────────────────────────────────────────
Agent scripts that open their OWN Tkinter windows (A3_UI_Mapping,
A4_The Actuator) must NOT call `tk.Tk()` when running inside this app,
because only ONE Tk root can exist per process.

Required edits in child scripts:
  A3_UI_Mapping.py  — in run_ui():
      CHANGE:   root = tk.Tk()
      TO:       root = tk.Toplevel(_MASTER_ROOT)  # _MASTER_ROOT injected below

  A4_The Actuator.PY  — in _show_hitl_ui():
      CHANGE:   root = tk.Tk()
      TO:       root = tk.Toplevel(_MASTER_ROOT)

This file handles the injection of `_MASTER_ROOT` into each module's
namespace just before launching it on its thread.
"""

# ── Standard library ─────────────────────────────────────────────────────────
import sys
import os
import threading
import importlib.util
import tkinter as tk
from tkinter import filedialog

# ── Third-party ──────────────────────────────────────────────────────────────
try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit(
        "customtkinter is not installed.\n"
        "Run:  pip install customtkinter"
    )
try:
    import keyring
except ImportError:
    keyring = None   # graceful fallback — app still works via .env

# ── Project paths ─────────────────────────────────────────────────────────────
# ROOT_DIR  → persistent folder for DB / reports (next to .exe when frozen,
#             otherwise the Approach_1_SQLite_Tkinter directory).
# AGENTS_DIR → where agent .py files live (sys._MEIPASS/agents when frozen —
#              that is PyInstaller's temp extraction dir for bundled source).
if getattr(sys, 'frozen', False):
    ROOT_DIR   = os.path.dirname(sys.executable)
    AGENTS_DIR = os.path.join(getattr(sys, '_MEIPASS', ROOT_DIR), "agents")
else:
    ROOT_DIR   = os.path.dirname(os.path.abspath(__file__))
    AGENTS_DIR = os.path.join(ROOT_DIR, "agents")
if AGENTS_DIR not in sys.path:
    sys.path.insert(0, AGENTS_DIR)

# =============================================================================
# CONSOLE REDIRECTOR
# Hijacks sys.stdout so every print() from any imported module or thread
# appears in the bottom CTkTextbox, colour-tagged and auto-scrolled.
# =============================================================================

class ConsoleRedirector:
    """
    Drop-in replacement for sys.stdout / sys.stderr.

    Usage:
        redir = ConsoleRedirector(textbox_widget)
        sys.stdout = redir
        sys.stderr = redir
        # Restore with:  redir.restore()
    """

    # ANSI-style colour hints embedded in messages from our agents
    _TAG_RULES = [
        ("✅",  "green"),
        ("❌",  "red"),
        ("⚠",  "orange"),
        ("⚠️", "orange"),
        ("[DMU]",  "cyan"),
        ("[SVG]",  "cyan"),
        ("[HITL]", "magenta"),
        ("FAIL",   "red"),
        ("PASS",   "green"),
        ("ERROR",  "red"),
        ("Traceback", "red"),
    ]

    def __init__(self, textbox: ctk.CTkTextbox):
        self._textbox   = textbox
        self._orig_out  = sys.__stdout__
        self._orig_err  = sys.__stderr__
        self._lock      = threading.Lock()

        # Configure colour tags on the underlying Text widget
        inner = textbox._textbox          # CTkTextbox wraps a tk.Text
        inner.tag_config("green",   foreground="#166534")
        inner.tag_config("red",     foreground="#991b1b")
        inner.tag_config("orange",  foreground="#92400e")
        inner.tag_config("cyan",    foreground="#004852")
        inner.tag_config("magenta", foreground="#6b21a8")
        inner.tag_config("default", foreground="#1e293b")

    # ── io.IOBase compatibility ──────────────────────────────────────────
    def write(self, text: str):
        if not text:
            return
        tag = "default"
        for keyword, colour in self._TAG_RULES:
            if keyword in text:
                tag = colour
                break
        with self._lock:
            # Schedule on the main thread — safe to call from worker threads
            self._textbox.after(0, self._append, text, tag)
        # Mirror to real stdout so VS Code / terminal still shows output
        try:
            self._orig_out.write(text)
        except Exception:
            pass

    def _append(self, text: str, tag: str):
        self._textbox.configure(state="normal")
        self._textbox.insert("end", text, tag)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    def flush(self):
        try:
            self._orig_out.flush()
        except Exception:
            pass

    def restore(self):
        sys.stdout = self._orig_out
        sys.stderr = self._orig_err

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):
        """No-op shim — satisfies sys.stdout.reconfigure() calls in child modules."""
        pass


# =============================================================================
# AGENT LOADER
# Dynamically imports a .py file by filesystem path so the file name can
# contain spaces (e.g. "A2_Cad Extraction.py") without renaming it.
# =============================================================================

def _load_module(name: str, filepath: str):
    """Return the imported module object, or None on failure."""
    try:
        spec   = importlib.util.spec_from_file_location(name, filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        print(f"❌ Could not load '{filepath}': {exc}")
        return None


# Pre-resolve file paths for each agent
_A1_PATH = os.path.join(AGENTS_DIR, "A1_2D_Extraction.py")
_A2_PATH = os.path.join(AGENTS_DIR, "A2_Cad Extraction.py")
_A3_PATH = os.path.join(AGENTS_DIR, "A3_UI_Mapping.py")
_A4_PATH = os.path.join(AGENTS_DIR, "A4_The Actuator.py")


# =============================================================================
# MASTER APPLICATION
# =============================================================================

class MasterApp(ctk.CTk):
    """Root window — contains sidebar, main panel, and global console."""

    # ── Appearance ──────────────────────────────────────────────────────────
    _SIDEBAR_W   = 220
    _CONSOLE_H   = 240
    _ACCENT      = "#004852"        # Traton Blue accent
    _ACCENT_HOV  = "#006678"         # Traton Blue hover
    _SIDEBAR_BG  = "#004852"        # Traton Blue sidebar
    _MAIN_BG     = "#f4f6f9"        # light grey page background
    _CARD_BG     = "#ffffff"        # white cards
    _BORDER      = "#b0d0d4"        # teal-tinted border
    _TEXT_MUTED  = "#6b7280"        # medium grey muted text

    # ── Nav items: (label, icon, panel_factory_method_name) ─────────────────
    _NAV_ITEMS = [
        ("Perception Layer",     "🔍",  "_build_a1_panel"),
        ("CAD Extraction Layer", "🏗",  "_build_a2_panel"),
        ("Mapping Layer",        "🔗",  "_build_a3_panel"),
        ("Actuator Layer",       "⚙️", "_build_a4_panel"),
    ]

    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("SCANIA HITL — Geometric Validation Assistant")
        self.geometry("1280x820")
        self.minsize(1024, 680)
        self.configure(fg_color=self._MAIN_BG)

        # Track active panel and running threads
        self._active_nav   = None
        self._panels: dict = {}
        self._busy_flags   = {}   # panel_name → BooleanVar (True while thread runs)

        self._build_layout()
        self._redirect_console()

        # Show first panel by default
        self._switch_panel("Perception Layer")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── API key: load from Credential Manager on startup ─────────────────
        # Schedule after mainloop starts so the window is visible first.
        self.after(300, self._ensure_api_key)

    # =========================================================================
    # LAYOUT CONSTRUCTION
    # =========================================================================

    def _build_layout(self):
        # ── Outer grid: sidebar | right_column ──────────────────────────────
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── Sidebar ─────────────────────────────────────────────────────────
        self._sidebar = ctk.CTkFrame(
            self, width=self._SIDEBAR_W, corner_radius=0,
            fg_color=self._SIDEBAR_BG   # deep navy
        )
        self._sidebar.grid(row=0, column=0, sticky="nsew")
        self._sidebar.grid_propagate(False)
        self._build_sidebar()

        # ── Right column: main + console ─────────────────────────────────────
        right = ctk.CTkFrame(self, fg_color=self._MAIN_BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=0)
        right.grid_columnconfigure(0, weight=1)
        self._right_frame = right   # kept for console toggle

        # Main content area (swappable panels live here)
        self._main_frame = ctk.CTkFrame(right, fg_color=self._MAIN_BG, corner_radius=0)
        self._main_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._main_frame.grid_rowconfigure(0, weight=1)
        self._main_frame.grid_columnconfigure(0, weight=1)

        # Console pane
        self._console_visible = True
        console_outer = ctk.CTkFrame(
            right, fg_color="#e0eef0",
            border_color=self._BORDER, border_width=1,
            corner_radius=0, height=self._CONSOLE_H
        )
        console_outer.grid(row=1, column=0, sticky="nsew")
        console_outer.grid_propagate(False)
        console_outer.grid_columnconfigure(0, weight=1)
        console_outer.grid_rowconfigure(1, weight=1)
        self._console_outer = console_outer

        # Console header bar
        hdr = ctk.CTkFrame(console_outer, fg_color=self._SIDEBAR_BG, corner_radius=0, height=28)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        ctk.CTkLabel(
            hdr, text="  ● Console Output",
            font=ctk.CTkFont("Consolas", 11, "bold"),
            text_color="#ffffff", anchor="w"
        ).pack(side="left", padx=6)

        # Toggle button — also lives in the header bar so it's always visible
        self._console_toggle_btn = ctk.CTkButton(
            hdr, text="▼  Hide", width=72, height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#374151", hover_color="#4b5563",
            command=self._toggle_console
        )
        self._console_toggle_btn.pack(side="right", padx=(4, 6), pady=4)

        ctk.CTkButton(
            hdr, text="Clear", width=54, height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#006678", hover_color="#004852",
            command=self._clear_console
        ).pack(side="right", padx=(6, 0), pady=4)

        self._console = ctk.CTkTextbox(
            console_outer,
            font=ctk.CTkFont("Consolas", 11),
            fg_color="#f8fafc",
            text_color="#1e293b",
            border_width=0,
            corner_radius=0,
            wrap="word",
            state="disabled",
        )
        self._console.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # Collapsed stub — shown in place of the full console when hidden
        self._console_stub = ctk.CTkFrame(
            right, fg_color=self._SIDEBAR_BG, corner_radius=0, height=28
        )
        # Not gridded yet — toggled in
        self._console_stub.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self._console_stub, text="  ● Console Output  (hidden)",
            font=ctk.CTkFont("Consolas", 10),
            text_color="#80c4cc", anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=8)
        ctk.CTkButton(
            self._console_stub, text="▲  Show", width=72, height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#374151", hover_color="#4b5563",
            command=self._toggle_console
        ).grid(row=0, column=1, sticky="e", padx=6, pady=4)

    def _build_sidebar(self):
        ctk.CTkLabel(
            self._sidebar,
            text="  PIPELINE LAYERS",
            font=ctk.CTkFont("Segoe UI", 9, "bold"),
            text_color="#80c4cc",
            anchor="w"
        ).pack(fill="x", padx=14, pady=(18, 6))

        self._nav_buttons = {}
        for label, icon, _ in self._NAV_ITEMS:
            btn = ctk.CTkButton(
                self._sidebar,
                text=f"  {icon}  {label}",
                anchor="w",
                height=40,
                corner_radius=8,
                font=ctk.CTkFont("Segoe UI", 13),
                fg_color="transparent",
                hover_color="#006678",
                text_color="#d0eef2",
                command=lambda l=label: self._switch_panel(l),
            )
            btn.pack(fill="x", padx=10, pady=2)
            self._nav_buttons[label] = btn

        # Spacer then version footer
        ctk.CTkFrame(self._sidebar, fg_color="transparent").pack(fill="both", expand=True)

        # API key button — always visible at the bottom of the sidebar
        ctk.CTkButton(
            self._sidebar,
            text="  🔑  API Key",
            anchor="w",
            height=34,
            corner_radius=8,
            font=ctk.CTkFont("Segoe UI", 11),
            fg_color="transparent",
            hover_color="#006678",
            text_color="#80c4cc",
            command=lambda: self._show_api_key_dialog(force=True),
        ).pack(fill="x", padx=10, pady=(0, 4))

        ctk.CTkLabel(
            self._sidebar,
            text="v3.0  |  Python " + sys.version[:6],
            font=ctk.CTkFont("Segoe UI", 9),
            text_color="#6abdc5",
        ).pack(pady=(0, 14))

    # =========================================================================
    # API KEY MANAGEMENT
    # =========================================================================

    _KR_SERVICE = "SCANIA_HITL_Framework"
    _KR_USER    = "openai_api_key"

    def _get_stored_key(self) -> str:
        """Read API key: Credential Manager → os.environ → .env fallback."""
        key, _ = self._get_key_with_source()
        return key

    def _get_key_with_source(self) -> tuple:
        """Returns (api_key, source) where source is 'credential', 'env', or None."""
        # 1. Windows Credential Manager (keyring) — user explicitly saved here
        if keyring:
            try:
                val = keyring.get_password(self._KR_SERVICE, self._KR_USER)
                if val:
                    return val, 'credential'
            except Exception:
                pass
        # 2. System / process environment variable — could be from another tool
        env_val = os.environ.get("OPENAI_API_KEY", "")
        if env_val:
            return env_val, 'env'
        return "", None

    def _save_key(self, api_key: str):
        """Persist key in Windows Credential Manager and current process env."""
        os.environ["OPENAI_API_KEY"] = api_key
        if keyring:
            try:
                keyring.set_password(self._KR_SERVICE, self._KR_USER, api_key)
            except Exception as e:
                print(f"⚠  Could not save to Credential Manager: {e}")

    def _ensure_api_key(self):
        """Always show the API key dialog on startup so the user confirms which key is active."""
        self._show_api_key_dialog(force=False)

    def _show_api_key_dialog(self, force: bool = False):
        """Modal dialog to confirm / enter / update the OpenAI API key.

        Modes:
          • force=True  → update flow (called from sidebar button)
          • force=False → startup flow: always shown so user sees which key is active
            - source='credential' → green confirm banner (pre-filled, one-click)
            - source='env'        → orange warning (found in system env var)
            - source=None         → normal entry (no key found)
        """
        existing, source = self._get_key_with_source()

        win = ctk.CTkToplevel(self)
        win.title("OpenAI API Key")
        win.geometry("520x360")
        win.resizable(False, False)
        win.grab_set()
        win.lift()
        win.focus_force()

        # Header
        hdr = ctk.CTkFrame(win, fg_color=self._ACCENT, corner_radius=0, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(
            hdr, text="  🔑  OpenAI API Key",
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
            text_color="white", anchor="w"
        ).pack(side="left", padx=16, fill="y")

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=16)

        # ── Source-aware banner ──────────────────────────────────────────────
        if force:
            banner_color, banner_text = None, None
            desc = "Update the key stored securely in Windows Credential Manager."
        elif source == 'credential':
            banner_color = "#d1fae5"   # light green
            banner_text  = "✅  Key found in Windows Credential Manager. Confirm to continue."
            desc = "The key below is already saved on this machine. You can use it as-is or replace it."
        elif source == 'env':
            banner_color = "#fff3cd"   # amber
            banner_text  = "⚠  Key detected from a system Environment Variable — NOT from this app."
            desc = (
                "An OPENAI_API_KEY was found in your system environment variables\n"
                "(possibly set by another tool, e.g. Cursor, OpenAI CLI).\n"
                "Please confirm this is YOUR key, or enter a different one."
            )
        else:
            banner_color, banner_text = None, None
            desc = (
                "No API key found. Enter your OpenAI key below.\n"
                "It will be stored securely in Windows Credential Manager\n"
                "— never in a plain-text file."
            )

        if banner_text:
            banner = ctk.CTkFrame(body, fg_color=banner_color, corner_radius=6)
            banner.pack(fill="x", pady=(0, 10))
            ctk.CTkLabel(
                banner, text=banner_text,
                font=ctk.CTkFont("Segoe UI", 11, "bold"),
                text_color="#1a1a1a", anchor="w", wraplength=450
            ).pack(padx=10, pady=8, anchor="w")

        ctk.CTkLabel(
            body, text=desc,
            font=ctk.CTkFont("Segoe UI", 12),
            text_color="#374151", justify="left", anchor="w", wraplength=460
        ).pack(anchor="w", pady=(0, 12))

        # Key entry row
        entry_row = ctk.CTkFrame(body, fg_color="transparent")
        entry_row.pack(fill="x")

        key_var = ctk.StringVar(value=existing)
        key_entry = ctk.CTkEntry(
            entry_row,
            textvariable=key_var,
            show="•",
            width=380, height=38,
            font=ctk.CTkFont("Consolas", 12),
            placeholder_text="sk-...",
            fg_color="#f0f7f8", border_color=self._BORDER,
        )
        key_entry.pack(side="left", padx=(0, 8))

        _showing = [False]
        def _toggle_show():
            _showing[0] = not _showing[0]
            key_entry.configure(show="" if _showing[0] else "•")
            show_btn.configure(text="Hide" if _showing[0] else "Show")
        show_btn = ctk.CTkButton(
            entry_row, text="Show", width=60, height=38,
            font=ctk.CTkFont("Segoe UI", 11),
            fg_color="#6b7280", hover_color="#4b5563",
            command=_toggle_show
        )
        show_btn.pack(side="left")

        status_lbl = ctk.CTkLabel(
            body, text="",
            font=ctk.CTkFont("Segoe UI", 11),
            text_color="#dc2626", anchor="w"
        )
        status_lbl.pack(anchor="w", pady=(8, 0))

        # Buttons
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(anchor="e", pady=(12, 0))

        def _save():
            k = key_var.get().strip()
            if not k.startswith("sk-") or len(k) < 20:
                status_lbl.configure(text="⚠  Key must start with 'sk-' and be at least 20 characters.")
                return
            self._save_key(k)
            src_label = "confirmed from environment" if (source == 'env' and k == existing) else "saved to Windows Credential Manager"
            print(f"✅  OpenAI API key {src_label}.")
            win.destroy()

        def _cancel():
            if not self._get_stored_key():
                status_lbl.configure(text="⚠  A key is required to use the Perception Agent.")
                return
            win.destroy()

        # Label the confirm button based on context
        confirm_label = "Confirm & Continue" if (not force and source in ('credential', 'env')) else "Save & Continue"
        ctk.CTkButton(
            btn_row, text=confirm_label, width=180, height=36,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
            fg_color=self._ACCENT, hover_color=self._ACCENT_HOV,
            command=_save
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="Cancel", width=80, height=36,
            font=ctk.CTkFont("Segoe UI", 12),
            fg_color="#6b7280", hover_color="#4b5563",
            command=_cancel
        ).pack(side="left")

        win.protocol("WM_DELETE_WINDOW", _cancel)

    # =========================================================================
    # NAVIGATION
    # =========================================================================

    def _switch_panel(self, label: str):
        # Un-highlight previous
        if self._active_nav and self._active_nav in self._nav_buttons:
            self._nav_buttons[self._active_nav].configure(
                fg_color="transparent", text_color="#d0eef2"
            )

        # Highlight selected
        self._nav_buttons[label].configure(
            fg_color=self._ACCENT, text_color="white"
        )
        self._active_nav = label

        # Destroy previous panel content (not recreated every time — cached)
        for widget in self._main_frame.winfo_children():
            widget.grid_forget()

        if label not in self._panels:
            factory = {
                "Perception Layer":     self._build_a1_panel,
                "CAD Extraction Layer": self._build_a2_panel,
                "Mapping Layer":        self._build_a3_panel,
                "Actuator Layer":       self._build_a4_panel,
            }[label]
            self._panels[label] = factory()

        self._panels[label].grid(row=0, column=0, sticky="nsew")

    # =========================================================================
    # SHARED HELPERS
    # =========================================================================

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        """Returns a styled card frame with a title label already packed."""
        frame = ctk.CTkFrame(
            parent, fg_color=self._CARD_BG,
            border_color=self._BORDER, border_width=1,
            corner_radius=10
        )
        ctk.CTkLabel(
            frame, text=title,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            text_color=self._ACCENT, anchor="w"
        ).pack(anchor="w", padx=16, pady=(12, 4))
        ctk.CTkFrame(frame, height=1, fg_color=self._BORDER).pack(fill="x", padx=12)
        return frame

    def _run_in_thread(self, btn: ctk.CTkButton, fn, *args):
        """Disable btn, run fn(*args) in a daemon thread.
        On success  → button becomes green '↺  Re-run'.
        On failure  → button restores to original text/colour so user can retry.
        """
        orig_text  = btn.cget("text")
        orig_fg    = btn.cget("fg_color")
        orig_hover = btn.cget("hover_color")
        btn.configure(state="disabled", text="⏳  Running…")

        def worker():
            success = False
            try:
                fn(*args)
                success = True
            except Exception:
                import traceback
                print(f"❌ Agent error:\n{traceback.format_exc()}")
            finally:
                if success:
                    btn.after(0, lambda: btn.configure(
                        state="normal",
                        text="↺  Re-run",
                        fg_color="#16a34a",
                        hover_color="#15803d",
                    ))
                else:
                    btn.after(0, lambda: btn.configure(
                        state="normal",
                        text=orig_text,
                        fg_color=orig_fg,
                        hover_color=orig_hover,
                    ))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _status_label(self, parent) -> ctk.CTkLabel:
        lbl = ctk.CTkLabel(
            parent, text="", font=ctk.CTkFont("Segoe UI", 11),
            text_color=self._TEXT_MUTED, anchor="w"
        )
        return lbl

    def _clear_console(self):
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _toggle_console(self):
        if self._console_visible:
            # Hide the full console pane, show the slim stub
            self._console_outer.grid_remove()
            self._console_stub.grid(row=1, column=0, sticky="ew")
            self._right_frame.grid_rowconfigure(1, minsize=28)
            self._console_visible = False
        else:
            # Restore full console, remove stub
            self._console_stub.grid_remove()
            self._console_outer.grid(row=1, column=0, sticky="nsew")
            self._right_frame.grid_rowconfigure(1, minsize=0)
            self._console_visible = True

    # =========================================================================
    # PANEL A1 — PERCEPTION
    # =========================================================================

    def _build_a1_panel(self) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(self._main_frame, fg_color=self._MAIN_BG, corner_radius=0)
        panel.grid_columnconfigure(0, weight=1)

        # Page title
        ctk.CTkLabel(
            panel,
            text="🔍  Perception Layer",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color="#1e293b", anchor="w"
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            panel,
            text="Extracts dimensions, tolerances, and GD&T symbols directly from 2D PDF drawings. You will be prompted to review and approve the data before it is saved.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=self._TEXT_MUTED, anchor="w"
        ).pack(anchor="w", padx=28, pady=(0, 18))

        # ── File picker card ────────────────────────────────────────────────
        card = self._card(panel, "Input Drawing")
        card.pack(fill="x", padx=24, pady=(0, 14))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=12)

        self._a1_path_var = ctk.StringVar(value="No file selected")
        path_entry = ctk.CTkEntry(
            row, textvariable=self._a1_path_var,
            width=460, height=36, state="readonly",
            font=ctk.CTkFont("Consolas", 11),
            fg_color="#e8f4f6", border_color=self._BORDER,
            text_color="#1e293b"
        )
        path_entry.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            row, text="📂  Browse…", width=120, height=36,
            fg_color="#004852", hover_color="#006678",
            text_color="#ffffff",
            border_width=0,
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            command=self._a1_browse
        ).pack(side="left")

        # ── Run card ────────────────────────────────────────────────────────
        run_card = self._card(panel, "Run Agent")
        run_card.pack(fill="x", padx=24, pady=(0, 14))

        run_inner = ctk.CTkFrame(run_card, fg_color="transparent")
        run_inner.pack(fill="x", padx=14, pady=12)

        a1_btn = ctk.CTkButton(
            run_inner, text="▶   Run Perception Agent",
            width=240, height=44,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color=self._ACCENT, hover_color=self._ACCENT_HOV,
        )
        self._a1_btn = a1_btn  # expose so _a1_browse can reset state
        a1_btn.pack(side="left")
        a1_status = self._status_label(run_inner)
        a1_status.pack(side="left", padx=16)

        def _run_a1():
            path = self._a1_path_var.get()
            if not path or path == "No file selected" or not os.path.isfile(path):
                a1_status.configure(text="⚠  Please select a valid drawing file first.", text_color="#fb923c")
                return
            a1_status.configure(text="Running…", text_color=self._TEXT_MUTED)
            def _task():
                module = _load_module("A1_2D_Extraction", _A1_PATH)
                if module:
                    # Inject master root so the HITL Toplevel uses our Tk root
                    module._MASTER_ROOT = self
                    module.run_perception_layer(path)
                    a1_btn.after(0, lambda: a1_status.configure(
                        text="✅  Completed.", text_color="#4ade80"))
            self._run_in_thread(a1_btn, _task)

        a1_btn.configure(command=_run_a1)

        # ── Info card ────────────────────────────────────────────────────────
        info_card = self._card(panel, "Pipeline Overview")
        info_card.pack(fill="x", padx=24, pady=(0, 14))
        info_lines = [
            "1. Select 2D Drawing (PDF/Image)",
            "2. AI Vision Extraction",
            "3. Human Review & Approval",
            "4. Save to Database",
        ]
        for line in info_lines:
            ctk.CTkLabel(
                info_card, text=f"  {line}",
                font=ctk.CTkFont("Segoe UI", 12),
                text_color="#374151", anchor="w"
            ).pack(anchor="w", padx=14, pady=2)
        ctk.CTkFrame(info_card, height=10, fg_color="transparent").pack()

        return panel

    def _a1_browse(self):
        path = filedialog.askopenfilename(
            title="Select Drawing Image",
            filetypes=[
                ("Drawing images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.pdf"),
                ("All files", "*.*"),
            ]
        )
        if path:
            self._a1_path_var.set(path)
            # If a previous run completed (button is green Re-run), reset it
            # back to the original Run state so it's clear this is a fresh file.
            if hasattr(self, "_a1_btn") and self._a1_btn.cget("text") == "↺  Re-run":
                self._a1_btn.configure(
                    text="▶   Run Perception Agent",
                    fg_color=self._ACCENT,
                    hover_color=self._ACCENT_HOV,
                )

    # =========================================================================
    # PANEL A2 — CAD EXTRACTION
    # =========================================================================

    def _build_a2_panel(self) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(self._main_frame, fg_color=self._MAIN_BG, corner_radius=0)
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            panel,
            text="🏗  CAD Extraction Layer",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color="#1e293b", anchor="w"
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            panel,
            text="Scans the open CATIA 3D model to find and register all published parameters into the database.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=self._TEXT_MUTED, anchor="w", wraplength=800
        ).pack(anchor="w", padx=28, pady=(0, 18))

        # ── Pre-flight Checklist ─────────────────────────────────────────────
        req_card = self._card(panel, "✅  Pre-flight Checklist  —  tick all before running")
        req_card.pack(fill="x", padx=24, pady=(0, 14))

        ctk.CTkLabel(
            req_card,
            text="  Confirm each item below is ready. The Run button unlocks only when all three are checked.",
            font=ctk.CTkFont("Segoe UI", 11, slant="italic"),
            text_color="#64748b", anchor="w"
        ).pack(anchor="w", padx=14, pady=(4, 8))

        checks_cfg = [
            ("CATIA V5 / ENOVIA VPM is open and running",
             "Without an active CATIA session the COM connection will fail immediately."),
            ("A CATProduct (.CATProduct) is the active document",
             "Agent 2 reads from the active document — a CATPart or drawing will produce no results."),
            ("Parameters are Published in CATIA  (Tools → Publication)",
             "Only Published parameters are visible to the COM API. Unpublished ones are silently skipped."),
        ]

        check_vars = []
        run_card_ref = [None]   # forward reference — filled after run_card is built

        def _on_check_change():
            """Enable / disable Run button based on all checkboxes being ticked."""
            all_checked = all(v.get() for v in check_vars)
            btn = run_card_ref[0]
            if btn is not None:
                btn.configure(
                    state="normal" if all_checked else "disabled",
                    fg_color=self._ACCENT if all_checked else "#94a3b8",
                    hover_color=self._ACCENT_HOV if all_checked else "#94a3b8",
                )

        for item_text, hint_text in checks_cfg:
            row = ctk.CTkFrame(req_card, fg_color="#f0f7ff",
                               corner_radius=8, border_color="#bfdbfe", border_width=1)
            row.pack(fill="x", padx=14, pady=4)

            var = ctk.BooleanVar(value=False)
            check_vars.append(var)

            ctk.CTkCheckBox(
                row,
                text=item_text,
                variable=var,
                font=ctk.CTkFont("Segoe UI", 12, "bold"),
                text_color="#1e3a5f",
                fg_color=self._ACCENT,
                hover_color=self._ACCENT_HOV,
                checkmark_color="#ffffff",
                border_color="#93c5fd",
                corner_radius=4,
                command=_on_check_change,
            ).pack(anchor="w", padx=12, pady=8)

        ctk.CTkFrame(req_card, height=8, fg_color="transparent").pack()

        # ── Run card ─────────────────────────────────────────────────────────
        run_card = self._card(panel, "Run Agent")
        run_card.pack(fill="x", padx=24, pady=(0, 14))
        run_inner = ctk.CTkFrame(run_card, fg_color="transparent")
        run_inner.pack(fill="x", padx=14, pady=12)

        a2_btn = ctk.CTkButton(
            run_inner, text="▶   Run CAD Extraction",
            width=240, height=44,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color="#94a3b8", hover_color="#94a3b8",   # starts disabled/grey
            state="disabled",
        )
        a2_btn.pack(side="left")
        run_card_ref[0] = a2_btn   # wire up the forward reference

        a2_status = self._status_label(run_inner)
        a2_status.pack(side="left", padx=16)

        hint_lbl = ctk.CTkLabel(
            run_inner,
            text="⬆  Tick all checklist items above to enable",
            font=ctk.CTkFont("Segoe UI", 11, slant="italic"),
            text_color="#94a3b8",
        )
        hint_lbl.pack(side="left", padx=8)

        def _on_check_change_with_hint():
            _on_check_change()
            all_checked = all(v.get() for v in check_vars)
            hint_lbl.configure(
                text="" if all_checked else "⬆  Tick all checklist items above to enable",
                text_color="#94a3b8"
            )

        # Rewire checkboxes to the version that also updates the hint label
        for var, (item_text, _) in zip(check_vars, checks_cfg):
            # find the checkbox widget and update its command
            pass   # easier to patch the BooleanVar trace
        for var in check_vars:
            var.trace_add("write", lambda *_: _on_check_change_with_hint())

        def _run_a2():
            a2_status.configure(text="Connecting to CATIA…", text_color=self._TEXT_MUTED)
            def _task():
                module = _load_module("A2_CadExtraction", _A2_PATH)
                if module:
                    module.run_agent_2_structural_fetch()
                    a2_btn.after(0, lambda: a2_status.configure(
                        text="✅  Completed.", text_color="#4ade80"))
            self._run_in_thread(a2_btn, _task)

        a2_btn.configure(command=_run_a2)

        info_card = self._card(panel, "What this agent does")
        info_card.pack(fill="x", padx=24, pady=(0, 14))
        steps = [
            "1. Connect to Active CATIA Model",
            "2. Scan 3D Assembly Tree",
            "3. Extract Published Parameters",
            "4. Save to Database",
        ]
        for s in steps:
            ctk.CTkLabel(
                info_card, text=f"  {s}",
                font=ctk.CTkFont("Consolas", 11),
                text_color="#374151", anchor="w"
            ).pack(anchor="w", padx=14, pady=1)
        ctk.CTkFrame(info_card, height=10, fg_color="transparent").pack()

        return panel

    # =========================================================================
    # PANEL A3 — MAPPING
    # =========================================================================

    def _build_a3_panel(self) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(self._main_frame, fg_color=self._MAIN_BG, corner_radius=0)
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            panel,
            text="🔗  Mapping Layer",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color="#1e293b", anchor="w"
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            panel,
            text="Link the extracted 2D drawing tolerances to their corresponding 3D CAD parameters.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=self._TEXT_MUTED, anchor="w", wraplength=800
        ).pack(anchor="w", padx=28, pady=(0, 10))

        # ── Embedded mapper ──────────────────────────────────────────────────
        mapper_outer = self._card(panel, "Parameter Mapper")
        mapper_outer.pack(fill="both", expand=True, padx=24, pady=(0, 14))

        embed_frame = ctk.CTkFrame(mapper_outer, fg_color="transparent")
        embed_frame.pack(fill="both", expand=True, padx=8, pady=8)

        # Plain tk.Frame inside the CTkFrame — A3 uses standard tk, so bg= works here
        import tkinter as _tk_a3
        tk_container = _tk_a3.Frame(embed_frame, bg="#f5f5f5")
        tk_container.pack(fill="both", expand=True)

        # Build the mapper UI directly into tk_container (non-blocking widget build)
        def _embed_a3():
            module = _load_module("A3_UI_Mapping", _A3_PATH)
            if module is None:
                ctk.CTkLabel(embed_frame, text="⚠  Failed to load A3_UI_Mapping.py",
                             text_color="#ef4444").pack(pady=20)
                return
            try:
                module.build_mapping_ui(tk_container)
            except Exception as exc:
                ctk.CTkLabel(embed_frame, text=f"⚠  Error building mapper:\n{exc}",
                             text_color="#ef4444", wraplength=600).pack(pady=20)

        # Schedule on the event loop so the panel is fully drawn first
        panel.after(50, _embed_a3)

        return panel

    # =========================================================================
    # PANEL A4 — ACTUATOR
    # =========================================================================

    def _build_a4_panel(self) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(self._main_frame, fg_color=self._MAIN_BG, corner_radius=0)
        panel.grid_columnconfigure(0, weight=1)
        panel.pack_propagate(True)

        ctk.CTkLabel(
            panel,
            text="⚙️  Actuator Layer",
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
            text_color="#1e293b", anchor="w"
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            panel,
            text="Drives the CATIA model to its extreme tolerance limits (MMC/LMC) and automatically checks for physical clashes.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=self._TEXT_MUTED, anchor="w", wraplength=800
        ).pack(anchor="w", padx=28, pady=(0, 14))

        # ── Idle view (run controls + flow info) ─────────────────────────────
        idle_view = ctk.CTkFrame(panel, fg_color="transparent")
        idle_view.pack(fill="both", expand=True)

        run_card = self._card(idle_view, "Run Agent")
        run_card.pack(fill="x", padx=24, pady=(0, 14))
        run_inner = ctk.CTkFrame(run_card, fg_color="transparent")
        run_inner.pack(fill="x", padx=14, pady=12)

        a4_btn = ctk.CTkButton(
            run_inner, text="▶   Run Actuator & DMU Validation",
            width=280, height=44,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color="#b91c1c", hover_color="#dc2626",
        )
        a4_btn.pack(side="left")
        a4_status = self._status_label(run_inner)
        a4_status.pack(side="left", padx=16)

        flow_card = self._card(idle_view, "Execution Flow")
        flow_card.pack(fill="x", padx=24, pady=(0, 14))
        steps = [
            "1. Select Target Assembly",
            "2. Actuate Parameters (MMC & LMC)",
            "3. Run DMU Clash Analysis",
            "4. Human Clash Review",
        ]
        for s in steps:
            ctk.CTkLabel(
                flow_card, text=f"  {s}",
                font=ctk.CTkFont("Segoe UI", 12),
                text_color="#374151", anchor="w"
            ).pack(anchor="w", padx=14, pady=2)
        ctk.CTkFrame(flow_card, height=10, fg_color="transparent").pack()

        # ── HITL view — shown instead of idle_view during clash review ────────
        hitl_view = ctk.CTkFrame(panel, fg_color="transparent")
        # Not packed yet — will replace idle_view when a clash is being reviewed

        import tkinter as _tk_a4
        hitl_inner = _tk_a4.Frame(hitl_view, bg="#f4f6f9")
        hitl_inner.pack(fill="both", expand=True, padx=8, pady=8)

        def _show_hitl_area():
            idle_view.pack_forget()
            hitl_view.pack(fill="both", expand=True)

        def _hide_hitl_area():
            hitl_view.pack_forget()
            # clear stale HITL widgets
            for w in hitl_inner.winfo_children():
                w.destroy()
            idle_view.pack(fill="both", expand=True)

        # ── Assembly picker helper ────────────────────────────────────────────
        def _pick_assembly(available):
            """
            Show a modal CTkToplevel listing available assemblies.
            Returns the chosen assembly_id (int) or None if cancelled.
            """
            import threading as _t
            result = {"id": None}
            done  = _t.Event()

            def _build_picker():
                win = ctk.CTkToplevel(self)
                win.title("Select Assembly")
                win.geometry("420x320")
                win.resizable(False, False)
                win.grab_set()
                win.lift()
                win.focus_force()

                ctk.CTkLabel(win,
                    text="Multiple assemblies found.\nSelect one to run:",
                    font=ctk.CTkFont("Segoe UI", 13),
                    justify="center"
                ).pack(pady=(16, 8))

                btn_frame = ctk.CTkScrollableFrame(win, fg_color="transparent", height=160)
                btn_frame.pack(fill="x", padx=24)

                for a_id, a_name in available:
                    def _choose(aid=a_id):
                        result["id"] = aid
                        done.set()
                        win.destroy()
                    ctk.CTkButton(
                        btn_frame,
                        text=f"[{a_id}]  {a_name}",
                        font=ctk.CTkFont("Segoe UI", 12),
                        fg_color=self._ACCENT, hover_color=self._ACCENT_HOV,
                        height=36,
                        command=_choose
                    ).pack(fill="x", pady=3)

                def _cancel():
                    done.set()
                    win.destroy()
                ctk.CTkButton(win, text="Cancel", width=100,
                              fg_color="#6b7280", hover_color="#4b5563",
                              command=_cancel).pack(pady=10)

                win.protocol("WM_DELETE_WINDOW", _cancel)

            self.after(0, _build_picker)
            done.wait()
            return result["id"]

        def _run_a4():
            a4_status.configure(text="Initialising…", text_color=self._TEXT_MUTED)

            def _task():
                import sqlite3 as _sq, os as _os
                db = _os.path.join(ROOT_DIR, "eats_validation.db")

                # ── Pre-resolve assembly if needed ────────────────────────────
                chosen_assembly_id = None
                try:
                    _conn = _sq.connect(db)
                    _cur  = _conn.cursor()
                    _cur.execute('''
                        SELECT DISTINCT ca.assembly_id, ca.assembly_name
                        FROM   human_mapping m
                        JOIN   catia_assemblies ca ON ca.assembly_id = m.assembly_id
                        ORDER  BY ca.assembly_id
                    ''')
                    available = _cur.fetchall()
                    _conn.close()
                except Exception:
                    available = []

                if len(available) == 1:
                    chosen_assembly_id = available[0][0]
                    print(f"   [Master UI] Single assembly found — auto-selecting: "
                          f"{available[0][1]} (id={chosen_assembly_id})")
                elif len(available) > 1:
                    chosen_assembly_id = _pick_assembly(available)
                    if chosen_assembly_id is None:
                        a4_btn.after(0, lambda: a4_status.configure(
                            text="Cancelled.", text_color=self._TEXT_MUTED))
                        return

                module = _load_module("A4_TheActuator", _A4_PATH)
                if module:
                    module._HITL_FRAME      = hitl_inner
                    module._ASSEMBLY_CHOICE = chosen_assembly_id
                    a4_btn.after(0, _show_hitl_area)
                    try:
                        module.run_agent_3_actuation()
                    finally:
                        module._HITL_FRAME      = None
                        module._ASSEMBLY_CHOICE = None
                        a4_btn.after(0, _hide_hitl_area)
                    a4_btn.after(0, lambda: a4_status.configure(
                        text="✅  Completed.", text_color="#4ade80"))

            self._run_in_thread(a4_btn, _task)

        a4_btn.configure(command=_run_a4)
        return panel

    # =========================================================================
    # CONSOLE REDIRECT
    # =========================================================================

    def _redirect_console(self):
        self._redirector = ConsoleRedirector(self._console)
        sys.stdout = self._redirector
        sys.stderr = self._redirector
        print("✅  Master Control Center started — console redirector active.")
        print(f"   Agents dir : {AGENTS_DIR}")
        print(f"   Database   : {os.path.join(ROOT_DIR, 'eats_validation.db')}\n")

    # =========================================================================
    # CLOSE HANDLER
    # =========================================================================

    def _on_close(self):
        # Restore stdout/stderr before exiting so any atexit handlers work
        if hasattr(self, "_redirector"):
            self._redirector.restore()
        self.destroy()


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    app = MasterApp()
    app.mainloop()
