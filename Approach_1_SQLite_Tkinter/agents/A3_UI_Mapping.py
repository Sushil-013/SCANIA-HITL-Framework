import sqlite3
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import os

# Always point at the project-root DB regardless of working directory (or next to .exe when frozen).
if getattr(sys, 'frozen', False):
    DB_PATH = os.path.join(os.path.dirname(sys.executable), 'eats_validation.db')
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eats_validation.db')


# ──────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def ensure_mapping_table():
    """Create human_mapping if it does not exist yet."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS human_mapping (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vlm_id          INTEGER,
            cad_id          INTEGER,
            drawing_id      INTEGER,
            assembly_id     INTEGER,
            vlm_text        TEXT,
            cad_text        TEXT,
            mapped_at       TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (drawing_id)  REFERENCES drawings(drawing_id),
            FOREIGN KEY (assembly_id) REFERENCES catia_assemblies(assembly_id)
        )
    ''')
    conn.commit()
    conn.close()


def get_filter_options():
    """
    Return (drawing_opts, assembly_opts) where each is a list of (id, display_str).
    Used to populate the context-filter comboboxes.
    """
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    try:
        cur.execute('SELECT drawing_id, COALESCE(NULLIF(project_tag,\'\'), file_name) FROM drawings ORDER BY drawing_id')
        drawings = [(r[0], f"[DWG {r[0]}]  {r[1]}") for r in cur.fetchall()]
    except Exception:
        drawings = []
    try:
        _asm_sql = "SELECT assembly_id, COALESCE(NULLIF(project_tag,''), assembly_name) FROM catia_assemblies ORDER BY assembly_id"
        cur.execute(_asm_sql)
        assemblies = [(r[0], f"[ASM {r[0]}]  {r[1]}") for r in cur.fetchall()]
    except Exception:
        assemblies = []
    conn.close()
    return drawings, assemblies


def get_filtered_data(drawing_id, assembly_id):
    """
    Return (vlm_rows, cad_rows, mapping_rows) filtered to the selected drawing and assembly.

    vlm  cols: id[0], drawing_id[1], file_name[2], feature_name[3], nominal_value[4],
               upper_tolerance[5], lower_tolerance[6], verification_status[7], local_row[8]
               (local_row resets to 1 for each drawing — used for display only)
    cad  cols: id[0], assembly_id[1], assembly_name[2], publication_name[3],
               part_instance[4], current_value[5], unit[6]
    map  cols: id[0], vlm_id[1], cad_id[2], vlm_text[3], cad_text[4], mapped_at[5]
    """
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # 2D dimensions — sorted by nominal_value ASC (ties broken by feature_name).
    # ROW_NUMBER window matches so #1 = smallest value within each drawing.
    if drawing_id is not None:
        cur.execute('''
            SELECT st.id, st.drawing_id, d.file_name,
                   st.feature_name, st.nominal_value,
                   st.upper_tolerance, st.lower_tolerance,
                   COALESCE(st.verification_status, 'Pending'),
                   ROW_NUMBER() OVER (
                       PARTITION BY st.drawing_id
                       ORDER BY st.nominal_value ASC, st.feature_name ASC
                   ) AS local_row
            FROM   semantic_tolerances st
            JOIN   drawings d ON d.drawing_id = st.drawing_id
            WHERE  st.drawing_id = ?
            ORDER  BY st.nominal_value ASC, st.feature_name ASC
        ''', (drawing_id,))
    else:
        cur.execute('''
            SELECT st.id, st.drawing_id, d.file_name,
                   st.feature_name, st.nominal_value,
                   st.upper_tolerance, st.lower_tolerance,
                   COALESCE(st.verification_status, 'Pending'),
                   ROW_NUMBER() OVER (
                       PARTITION BY st.drawing_id
                       ORDER BY st.nominal_value ASC, st.feature_name ASC
                   ) AS local_row
            FROM   semantic_tolerances st
            JOIN   drawings d ON d.drawing_id = st.drawing_id
            ORDER  BY st.drawing_id, st.nominal_value ASC, st.feature_name ASC
        ''')
    vlm_rows = cur.fetchall()

    # 3D parameters — filtered by assembly
    # Wrapped in try/except: catia_parameters / catia_assemblies won't exist
    # until Agent 2 (CAD Extraction) has been run at least once.
    try:
        if assembly_id is not None:
            cur.execute('''
                SELECT cp.id, cp.assembly_id, ca.assembly_name,
                       cp.publication_name, cp.part_instance,
                       cp.current_value, cp.unit
                FROM   catia_parameters cp
                JOIN   catia_assemblies ca ON ca.assembly_id = cp.assembly_id
                WHERE  cp.assembly_id = ?
                ORDER  BY cp.part_instance ASC,
                          CAST(cp.current_value AS REAL) ASC,
                          cp.publication_name ASC
            ''', (assembly_id,))
        else:
            cur.execute('''
                SELECT cp.id, cp.assembly_id, ca.assembly_name,
                       cp.publication_name, cp.part_instance,
                       cp.current_value, cp.unit
                FROM   catia_parameters cp
                JOIN   catia_assemblies ca ON ca.assembly_id = cp.assembly_id
                ORDER  BY cp.assembly_id, cp.part_instance ASC,
                          CAST(cp.current_value AS REAL) ASC,
                          cp.publication_name ASC
            ''')
        cad_rows = cur.fetchall()
    except Exception:
        cad_rows = []

    # Mappings — filtered to only this drawing+assembly session
    try:
        parts = []
        params = []
        sql = '''
            SELECT m.id, m.vlm_id, m.cad_id, m.vlm_text, m.cad_text, m.mapped_at
            FROM   human_mapping m
        '''
        conditions = []
        if drawing_id is not None:
            conditions.append('m.drawing_id = ?')
            params.append(drawing_id)
        if assembly_id is not None:
            conditions.append('m.assembly_id = ?')
            params.append(assembly_id)
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY m.id'
        cur.execute(sql, params)
        mapping_rows = cur.fetchall()
    except Exception:
        mapping_rows = []

    conn.close()
    return vlm_rows, cad_rows, mapping_rows


def save_mapping(vlm_row, cad_row):
    """Insert one mapping record; returns the new mapping id."""
    ensure_mapping_table()   # guarantee table exists before INSERT
    vlm_text = f"{vlm_row[3]}  nominal={vlm_row[4]}  +{vlm_row[5]}/{vlm_row[6]}"
    cad_text  = f"{cad_row[3]}  part={cad_row[4]}  val={cad_row[5]} {cad_row[6]}"
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute(
        'INSERT INTO human_mapping '
        '(vlm_id, cad_id, drawing_id, assembly_id, vlm_text, cad_text) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (vlm_row[0], cad_row[0], vlm_row[1], cad_row[1], vlm_text, cad_text)
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id, vlm_text, cad_text


def delete_mapping(mapping_id):
    """Remove a mapping row by its primary key."""
    ensure_mapping_table()   # guarantee table exists
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM human_mapping WHERE id = ?', (mapping_id,))
    conn.commit()
    conn.close()


def get_part_instances(assembly_id):
    """Return sorted list of distinct part_instance values for an assembly."""
    if assembly_id is None:
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute(
            'SELECT DISTINCT part_instance FROM catia_parameters '
            'WHERE assembly_id = ? AND part_instance IS NOT NULL '
            'ORDER BY part_instance',
            (assembly_id,)
        )
        parts = [r[0] for r in cur.fetchall() if r[0]]
        conn.close()
        return parts
    except Exception:
        return []


# Create the table immediately on import so it always exists before any function
# tries to use it (covers both standalone and master_app embedded modes).
ensure_mapping_table()

# ──────────────────────────────────────────────────────────────────────────────
# EMBEDDABLE UI  (called by master_app.py with a parent frame)
# ──────────────────────────────────────────────────────────────────────────────

def build_mapping_ui(parent_frame):
    """
    Build the entire 2D↔3D mapping interface INSIDE parent_frame.
    No new Tk root / Toplevel is created — everything lives in the frame
    supplied by the caller (e.g. the Master Control Center panel).
    """
    ensure_mapping_table()

    BG   = "#f5f5f5"
    NAV  = "#004852"   # Traton Blue (matches master sidebar)

    # clear any previous content
    for w in parent_frame.winfo_children():
        w.destroy()
    parent_frame.configure(bg=BG)

    # ── Load filter options ───────────────────────────────────────────────
    drawing_opts, assembly_opts = get_filter_options()

    active_drawing_id   = drawing_opts[0][0]  if drawing_opts  else None
    active_assembly_id  = assembly_opts[0][0] if assembly_opts else None
    active_part_filter  = None   # None = "All Parts"
    part_instance_opts  = get_part_instances(active_assembly_id)
    vlm_rows, cad_rows, mapping_rows = get_filtered_data(active_drawing_id, active_assembly_id)

    # Hard block only when there are no 2D drawings at all
    if not drawing_opts:
        tk.Label(parent_frame, text="⚠  No 2D drawing data found — run Perception (Agent 1) first.",
                 font=("Segoe UI", 12), bg=BG, fg="#b45309",
                 anchor="center").pack(expand=True)
        return

    # Soft warning banner if CAD data is missing (Agent 2 not run yet)
    cad_missing = not assembly_opts
    if cad_missing:
        warn_bar = tk.Frame(parent_frame, bg="#fff3cd")
        warn_bar.pack(fill=tk.X)
        tk.Label(warn_bar,
                 text="⚠  No 3D CAD data found.  Run CAD Extraction (Agent 2) first — "
                      "the 3D panel will be empty until then.",
                 font=("Segoe UI", 9), bg="#fff3cd", fg="#856404",
                 anchor="w").pack(side=tk.LEFT, padx=10, pady=4)

    mapped_vlm_ids = {r[1] for r in mapping_rows}
    mapped_cad_ids = {r[2] for r in mapping_rows}

    # ── Header bar ───────────────────────────────────────────────────────
    hdr = tk.Frame(parent_frame, bg=NAV, height=36)
    hdr.pack(fill=tk.X)
    hdr.pack_propagate(False)
    tk.Label(hdr, text="  🔗  2D ↔ 3D Parameter Mapper",
             font=('Segoe UI', 11, 'bold'), bg=NAV, fg="white").pack(side=tk.LEFT, padx=14, fill=tk.Y)

    # ── Context Filter bar ───────────────────────────────────────────────
    flt_frame = tk.Frame(parent_frame, bg="#d0eaed", pady=5)
    flt_frame.pack(fill=tk.X)

    tk.Label(flt_frame, text="  Session Context:",
             font=('Segoe UI', 9, 'bold'), bg="#dce8f7").pack(side=tk.LEFT, padx=(10, 4))

    tk.Label(flt_frame, text="2D Drawing:", font=('Segoe UI', 9), bg="#dce8f7").pack(side=tk.LEFT)
    dwg_var = tk.StringVar()
    dwg_cb  = ttk.Combobox(flt_frame, textvariable=dwg_var, state="readonly",
                            width=34, font=('Segoe UI', 9))
    dwg_cb['values'] = [d[1] for d in drawing_opts]
    dwg_cb.current(0)
    dwg_cb.pack(side=tk.LEFT, padx=(2, 12))

    tk.Label(flt_frame, text="3D Assembly:", font=('Segoe UI', 9), bg="#d0eaed").pack(side=tk.LEFT)
    asm_var = tk.StringVar()
    asm_cb  = ttk.Combobox(flt_frame, textvariable=asm_var, state="readonly",
                            width=34, font=('Segoe UI', 9))
    asm_cb['values'] = [a[1] for a in assembly_opts]
    if assembly_opts:
        asm_cb.current(0)
    else:
        asm_var.set("— no CAD data yet —")
    asm_cb.pack(side=tk.LEFT, padx=(2, 12))

    # Part filter — narrows 3D side to one specific part instance
    tk.Label(flt_frame, text="Part:", font=('Segoe UI', 9), bg="#d0eaed").pack(side=tk.LEFT)
    part_var = tk.StringVar(value="All Parts")
    part_cb  = ttk.Combobox(flt_frame, textvariable=part_var, state="readonly",
                             width=26, font=('Segoe UI', 9))
    part_cb['values'] = ["All Parts"] + part_instance_opts
    part_cb.current(0)
    part_cb.pack(side=tk.LEFT, padx=(2, 12))

    filter_lbl = tk.Label(flt_frame, text="", font=('Segoe UI', 8, 'italic'),
                          bg="#d0eaed", fg="#555")
    filter_lbl.pack(side=tk.LEFT, padx=6)

    refresh_btn = tk.Button(flt_frame, text="↺  Refresh DB",
                            font=('Segoe UI', 9), bg=NAV, fg="white",
                            relief=tk.FLAT, cursor="hand2", padx=8)
    refresh_btn.pack(side=tk.RIGHT, padx=10, pady=3)

    # ── Top section: two tables side by side ─────────────────────────────
    top_frame = tk.Frame(parent_frame, bg=BG)
    top_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 0))

    # LEFT — 2D Drawing Data ──────────────────────────────────────────────
    left_frame = tk.LabelFrame(top_frame, text="  2D Drawing Data  ",
                               font=('Segoe UI', 9, 'bold'), bg=BG, padx=4, pady=4)
    left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

    vlm_cols = ("vlm_id", "drawing", "feature_name", "nominal", "tolerance", "status")
    vlm_tree = ttk.Treeview(left_frame, columns=vlm_cols, show="headings",
                             selectmode="browse", height=10)
    vlm_tree.heading("vlm_id",       text="#")
    vlm_tree.heading("drawing",      text="Drawing")
    vlm_tree.heading("feature_name", text="Feature Name")
    vlm_tree.heading("nominal",      text="Nominal")
    vlm_tree.heading("tolerance",    text="Tolerance")
    vlm_tree.heading("status",       text="Audit Status")
    vlm_tree.column("vlm_id",       width=38,  anchor=tk.CENTER, stretch=False)
    vlm_tree.column("drawing",      width=130, anchor=tk.W,      stretch=False)
    vlm_tree.column("feature_name", width=155, anchor=tk.W)
    vlm_tree.column("nominal",      width=62,  anchor=tk.CENTER, stretch=False)
    vlm_tree.column("tolerance",    width=95,  anchor=tk.CENTER, stretch=False)
    vlm_tree.column("status",       width=100, anchor=tk.CENTER, stretch=False)
    vlm_tree.tag_configure('verified', background='#d4edda', foreground='#155724')
    vlm_tree.tag_configure('pending',  background='#fff3cd', foreground='#856404')
    vlm_tree.tag_configure('rejected', background='#f8d7da', foreground='#721c24')
    vlm_sb = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=vlm_tree.yview)
    vlm_tree.configure(yscrollcommand=vlm_sb.set)
    vlm_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vlm_sb.pack(side=tk.LEFT, fill=tk.Y)

    # RIGHT — 3D CAD Data ─────────────────────────────────────────────────
    right_frame = tk.LabelFrame(top_frame, text="  3D CAD Data  ",
                                font=('Segoe UI', 9, 'bold'), bg=BG, padx=4, pady=4)
    right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

    cad_cols = ("cad_seq", "parameter", "value")
    cad_tree = ttk.Treeview(right_frame, columns=cad_cols, show="headings",
                             selectmode="browse", height=10)
    cad_tree.heading("cad_seq",   text="#")
    cad_tree.heading("parameter", text="Parameter")
    cad_tree.heading("value",     text="Value")
    cad_tree.column("cad_seq",   width=38,  anchor=tk.CENTER, stretch=False)
    cad_tree.column("parameter", width=245, anchor=tk.W)
    cad_tree.column("value",     width=95,  anchor=tk.CENTER, stretch=False)
    # Part header rows — styled, non-selectable
    cad_tree.tag_configure('part_hdr',  background='#004852', foreground='white',
                            font=('Segoe UI', 9, 'bold'))
    cad_tree.tag_configure('cad_row',   background='#ffffff', foreground='#1e293b')
    cad_tree.tag_configure('cad_alt',   background='#e8f4f5', foreground='#1e293b')
    cad_sb = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=cad_tree.yview)
    cad_tree.configure(yscrollcommand=cad_sb.set)
    cad_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    cad_sb.pack(side=tk.LEFT, fill=tk.Y)

    # ── Middle: Link button ───────────────────────────────────────────────
    mid_frame = tk.Frame(parent_frame, bg=BG)
    mid_frame.pack(fill=tk.X, padx=8, pady=(5, 0))

    link_btn = tk.Button(mid_frame, text="🔗  Link Selected",
                         font=('Segoe UI', 10, 'bold'),
                         bg=NAV, fg="white",
                         padx=18, pady=5, relief=tk.FLAT, cursor="hand2")
    link_btn.pack(side=tk.LEFT, padx=(4, 0))

    status_lbl = tk.Label(mid_frame, text="", font=('Segoe UI', 9),
                          bg=BG, fg="#555")
    status_lbl.pack(side=tk.LEFT, padx=12)

    # ── Caution strip (hidden until a mismatch is detected) ───────────────
    caution_frame = tk.Frame(parent_frame, bg="#fff3cd",
                             highlightbackground="#e5a800", highlightthickness=2)
    # Not packed yet — shown on demand

    caution_title = tk.Label(caution_frame,
                             text="", font=('Segoe UI', 10, 'bold'),
                             bg="#fff3cd", fg="#7c4a00", anchor="w")
    caution_title.pack(fill=tk.X, padx=14, pady=(10, 2))

    caution_detail = tk.Label(caution_frame,
                              text="", font=('Segoe UI', 9),
                              bg="#fff3cd", fg="#5a3a00",
                              justify=tk.LEFT, anchor="w")
    caution_detail.pack(fill=tk.X, padx=14, pady=(0, 8))

    caution_btn_frame = tk.Frame(caution_frame, bg="#fff3cd")
    caution_btn_frame.pack(fill=tk.X, padx=14, pady=(0, 10))

    confirm_btn = tk.Button(caution_btn_frame,
                            text="✅  Yes, Link Anyway",
                            font=('Segoe UI', 9, 'bold'),
                            bg="#004852", fg="white",
                            padx=14, pady=4, relief=tk.FLAT, cursor="hand2")
    confirm_btn.pack(side=tk.LEFT, padx=(0, 10))

    reselect_btn = tk.Button(caution_btn_frame,
                             text="↩  Re-select",
                             font=('Segoe UI', 9),
                             bg="#6b7280", fg="white",
                             padx=14, pady=4, relief=tk.FLAT, cursor="hand2")
    reselect_btn.pack(side=tk.LEFT)

    def _show_caution(v_row, c_row):
        """Populate and reveal the caution strip."""
        nominal_2d = v_row[4]
        value_3d   = c_row[5]
        try:
            diff = abs(float(nominal_2d) - float(value_3d))
            diff_str = f"{diff:.4g} mm difference"
        except Exception:
            diff_str = "values could not be compared numerically"

        caution_title.config(
            text=f"⚠   Nominal Mismatch Detected")
        caution_detail.config(
            text=(f"  2D Feature  :  {v_row[3]}   →   nominal = {nominal_2d} mm\n"
                  f"  3D Parameter:  {c_row[3]}   →   value   = {value_3d} {c_row[6] or 'mm'}\n"
                  f"  ─────────────────────────────────────────────────\n"
                  f"  {diff_str}  —  are you sure this is the correct mapping?"))

        def _confirm():
            _hide_caution()
            _do_link(v_row, c_row)

        def _reselect():
            _hide_caution()
            status_lbl.config(text="↩  Re-select both rows and try again.", fg="#555")

        confirm_btn.config(command=_confirm)
        reselect_btn.config(command=_reselect)
        caution_frame.pack(fill=tk.X, padx=8, pady=(4, 2))
        caution_frame.lift()

    def _hide_caution():
        caution_frame.pack_forget()

    # ── Bottom: Saved Mappings ────────────────────────────────────────────
    bot_outer = tk.Frame(parent_frame, bg=BG)
    bot_outer.pack(fill=tk.BOTH, padx=8, pady=(0, 8))

    bot_frame = tk.LabelFrame(bot_outer, text="  Saved Mappings  ",
                               font=('Segoe UI', 9, 'bold'), bg=BG, padx=4, pady=4)
    bot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    map_cols = ("map_id", "feature_2d", "param_3d")
    map_tree = ttk.Treeview(bot_frame, columns=map_cols, show="headings",
                             selectmode="browse", height=6)
    map_tree.heading("map_id",     text="ID")
    map_tree.heading("feature_2d", text="2D Feature")
    map_tree.heading("param_3d",   text="3D Parameter")
    map_tree.column("map_id",     width=40,  anchor=tk.CENTER, stretch=False)
    map_tree.column("feature_2d", width=380, anchor=tk.W)
    map_tree.column("param_3d",   width=380, anchor=tk.W)
    map_sb = ttk.Scrollbar(bot_frame, orient=tk.VERTICAL, command=map_tree.yview)
    map_tree.configure(yscrollcommand=map_sb.set)
    map_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    map_sb.pack(side=tk.LEFT, fill=tk.Y)

    unlink_btn = tk.Button(bot_outer, text="❌  Unlink\nSelected",
                           font=('Segoe UI', 9, 'bold'),
                           bg="#8B0000", fg="white",
                           padx=10, pady=8, relief=tk.FLAT, cursor="hand2")
    unlink_btn.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.CENTER)

    # ── In-memory lookups ────────────────────────────────────────────────
    vlm_map  = {}
    cad_map  = {}
    map_iids = {}

    # ── Populate helpers ─────────────────────────────────────────────────
    def _populate_cad_tree(part_filter):
        """Rebuild only the 3D side. part_filter=None means grouped All Parts view."""
        for item in cad_tree.get_children():
            cad_tree.delete(item)
        cad_map.clear()

        # Decide which rows to show
        visible = [r for r in cad_rows if r[0] not in mapped_cad_ids]
        if part_filter and part_filter != "All Parts":
            visible = [r for r in visible if r[4] == part_filter]

        if part_filter and part_filter != "All Parts":
            # ── Filtered view: flat list, no headers ─────────────────────
            for seq, row in enumerate(visible, 1):
                val_str = f"{row[5]} {row[6]}" if row[6] else str(row[5])
                iid = str(row[0])
                tag = 'cad_alt' if seq % 2 == 0 else 'cad_row'
                cad_tree.insert("", tk.END, iid=iid,
                                values=(f"#{seq}", row[3], val_str), tags=(tag,))
                cad_map[iid] = row
        else:
            # ── All Parts: grouped with part header separator rows ────────
            # Group by part_instance preserving order
            seen_parts = []
            grouped = {}
            for row in visible:
                pi = row[4] or '(unnamed)'
                if pi not in grouped:
                    seen_parts.append(pi)
                    grouped[pi] = []
                grouped[pi].append(row)
            for pi in seen_parts:
                # Header row — iid prefixed so it's never in cad_map
                hdr_iid = f"__hdr__{pi}"
                hdr_text = f"▸  {pi}  ({len(grouped[pi])} param{'s' if len(grouped[pi])!=1 else ''})"
                cad_tree.insert("", tk.END, iid=hdr_iid,
                                values=("  ", hdr_text, ""), tags=('part_hdr',))
                for seq, row in enumerate(grouped[pi], 1):
                    val_str = f"{row[5]} {row[6]}" if row[6] else str(row[5])
                    iid = str(row[0])
                    tag = 'cad_alt' if seq % 2 == 0 else 'cad_row'
                    cad_tree.insert("", tk.END, iid=iid,
                                    values=(f"#{seq}", row[3], val_str), tags=(tag,))
                    cad_map[iid] = row

    def populate_top_tables():
        for item in vlm_tree.get_children():
            vlm_tree.delete(item)
        vlm_map.clear()
        _STATUS_TAG = {'Verified': 'verified', 'Pending': 'pending'}
        for row in vlm_rows:
            if row[0] in mapped_vlm_ids:
                continue
            tol      = f"+{row[5]} / {row[6]}"
            status   = row[7] if len(row) > 7 else 'Pending'
            local_no = row[8] if len(row) > 8 else row[0]
            tag      = _STATUS_TAG.get(status, 'rejected')
            iid      = str(row[0])
            fname_short = os.path.splitext(row[2])[0][:18] if row[2] else ''
            vlm_tree.insert("", tk.END, iid=iid,
                             values=(f"#{local_no}", fname_short, row[3], row[4], tol, status),
                             tags=(tag,))
            vlm_map[iid] = row
        _populate_cad_tree(active_part_filter)

    def populate_mapping_table():
        for item in map_tree.get_children():
            map_tree.delete(item)
        map_iids.clear()
        for row in mapping_rows:
            iid = str(row[0])
            map_tree.insert("", tk.END, iid=iid, values=(row[0], row[3], row[4]))
            map_iids[iid] = (row[1], row[2])

    populate_top_tables()
    populate_mapping_table()

    # ── Part filter change — only rebuilds the 3D side ──────────────────
    def on_part_change(*_):
        nonlocal active_part_filter
        chosen = part_var.get()
        active_part_filter = None if chosen == "All Parts" else chosen
        _populate_cad_tree(active_part_filter)
        filter_lbl.config(
            text=f"  {len(vlm_map)} dim(s)  |  {len(cad_map)} param(s)  |  {len(mapping_rows)} mapping(s)")

    # ── Context filter change ─────────────────────────────────────────────
    def on_filter_change(*_):
        nonlocal active_drawing_id, active_assembly_id, active_part_filter, part_instance_opts
        dwg_label = dwg_var.get()
        asm_label = asm_var.get()
        active_drawing_id  = next((d[0] for d in drawing_opts  if d[1] == dwg_label), None)
        active_assembly_id = next((a[0] for a in assembly_opts if a[1] == asm_label), None)
        # Reset part filter whenever assembly changes
        active_part_filter = None
        part_instance_opts[:] = get_part_instances(active_assembly_id)
        part_cb['values'] = ["All Parts"] + part_instance_opts
        part_var.set("All Parts")
        new_vlm, new_cad, new_map = get_filtered_data(active_drawing_id, active_assembly_id)
        vlm_rows[:]     = new_vlm
        cad_rows[:]     = new_cad
        mapping_rows[:] = new_map
        mapped_vlm_ids.clear(); mapped_vlm_ids.update(r[1] for r in mapping_rows)
        mapped_cad_ids.clear(); mapped_cad_ids.update(r[2] for r in mapping_rows)
        populate_top_tables()
        populate_mapping_table()
        filter_lbl.config(
            text=f"  {len(vlm_rows)} dim(s)  |  {len(cad_rows)} param(s)  |  {len(mapping_rows)} mapping(s)")

    def on_refresh():
        """Re-query filter options (in case A1/A2 ran since panel was opened)."""
        nonlocal drawing_opts, assembly_opts
        drawing_opts, assembly_opts = get_filter_options()
        dwg_cb['values'] = [d[1] for d in drawing_opts]
        asm_cb['values'] = [a[1] for a in assembly_opts]
        if drawing_opts: dwg_cb.current(0)
        if assembly_opts: asm_cb.current(0)
        part_var.set("All Parts")
        on_filter_change()
        status_lbl.config(text="✅  DB refreshed.", fg="#005500")

    dwg_cb.bind("<<ComboboxSelected>>", on_filter_change)
    asm_cb.bind("<<ComboboxSelected>>", on_filter_change)
    part_cb.bind("<<ComboboxSelected>>", on_part_change)
    refresh_btn.config(command=on_refresh)
    on_filter_change()

    # ── Link action ───────────────────────────────────────────────────────
    def _do_link(v_row, c_row):
        """Execute the actual DB save + UI update — called after any caution is cleared."""
        new_id, vlm_text, cad_text = save_mapping(v_row, c_row)
        mapped_vlm_ids.add(v_row[0]); mapped_cad_ids.add(c_row[0])
        mapping_rows.append((new_id, v_row[0], c_row[0], vlm_text, cad_text, "now"))
        map_iids[str(new_id)] = (v_row[0], c_row[0])
        iid_v = str(v_row[0]); iid_c = str(c_row[0])
        if vlm_tree.exists(iid_v): vlm_tree.delete(iid_v)
        if iid_v in vlm_map: del vlm_map[iid_v]
        if cad_tree.exists(iid_c): cad_tree.delete(iid_c)
        if iid_c in cad_map: del cad_map[iid_c]
        map_tree.insert("", tk.END, iid=str(new_id), values=(new_id, vlm_text, cad_text))
        status_lbl.config(text=f"✅  Linked: {v_row[3]}  ↔  {c_row[3]}", fg="#005500")
        filter_lbl.config(
            text=f"  {len(vlm_map)} dim(s)  |  {len(cad_map)} param(s)  |  {len(map_iids)} mapping(s)")

    def on_link():
        v_sel = vlm_tree.selection()
        c_sel = cad_tree.selection()
        if not v_sel or not c_sel:
            status_lbl.config(text="⚠  Select one row from each table first.", fg="#aa4400")
            return
        # Guard: ignore clicks on part-header separator rows
        if c_sel[0].startswith("__hdr__") or c_sel[0] not in cad_map:
            status_lbl.config(text="⚠  Select a parameter row, not a part header.", fg="#aa4400")
            return
        _hide_caution()
        v_row = vlm_map[v_sel[0]]
        c_row = cad_map[c_sel[0]]
        # ── Mismatch check ────────────────────────────────────────────────
        try:
            nominal_2d = float(v_row[4])
            value_3d   = float(c_row[5])
            mismatch   = abs(nominal_2d - value_3d) > 1e-6
        except (TypeError, ValueError):
            mismatch = str(v_row[4]).strip() != str(c_row[5]).strip()
        if mismatch:
            _show_caution(v_row, c_row)
            return
        # Nominals match — link silently
        _do_link(v_row, c_row)

    link_btn.config(command=on_link)

    # ── Unlink action ─────────────────────────────────────────────────────
    def on_unlink():
        m_sel = map_tree.selection()
        if not m_sel:
            status_lbl.config(text="⚠  Select a row in Saved Mappings to unlink.", fg="#aa4400")
            return
        m_iid     = m_sel[0]
        mapping_id = int(m_iid)
        vlm_id, cad_id = map_iids[m_iid]
        delete_mapping(mapping_id)
        mapped_vlm_ids.discard(vlm_id); mapped_cad_ids.discard(cad_id)
        _STATUS_TAG = {'Verified': 'verified', 'Pending': 'pending'}
        for row in vlm_rows:
            if row[0] == vlm_id:
                tol      = f"+{row[5]} / {row[6]}"
                status   = row[7] if len(row) > 7 else 'Pending'
                local_no = row[8] if len(row) > 8 else row[0]
                tag      = _STATUS_TAG.get(status, 'rejected')
                iid      = str(row[0])
                fname_short = os.path.splitext(row[2])[0][:18] if row[2] else ''
                vlm_tree.insert("", tk.END, iid=iid,
                                values=(f"#{local_no}", fname_short, row[3], row[4], tol, status),
                                tags=(tag,))
                vlm_map[iid] = row
                break
        for row in cad_rows:
            if row[0] == cad_id:
                # Restore using grouped view so it appears in the right part section
                _populate_cad_tree(active_part_filter)
                break
        map_tree.delete(m_iid)
        mapping_rows[:] = [r for r in mapping_rows if r[0] != mapping_id]
        del map_iids[m_iid]
        status_lbl.config(text=f"🗑  Unlinked mapping ID {mapping_id}.", fg="#555")

    unlink_btn.config(command=on_unlink)


# ──────────────────────────────────────────────────────────────────────────────
# STANDALONE  (python A3_UI_Mapping.py)
# ──────────────────────────────────────────────────────────────────────────────

def run_ui():
    """Standalone wrapper — creates its own Tk window."""
    root = tk.Tk()
    root.title("SCANIA HITL — Parameter Mapper")
    root.geometry("1100x700")
    root.resizable(True, True)
    build_mapping_ui(root)
    root.mainloop()


if __name__ == "__main__":
    run_ui()
