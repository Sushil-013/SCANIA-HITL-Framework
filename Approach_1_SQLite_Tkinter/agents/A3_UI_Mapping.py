import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox
import os

# Always point at the project-root DB regardless of working directory
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
               upper_tolerance[5], lower_tolerance[6], verification_status[7]
    cad  cols: id[0], assembly_id[1], assembly_name[2], publication_name[3],
               part_instance[4], current_value[5], unit[6]
    map  cols: id[0], vlm_id[1], cad_id[2], vlm_text[3], cad_text[4], mapped_at[5]
    """
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # 2D dimensions — filtered by drawing
    if drawing_id is not None:
        cur.execute('''
            SELECT st.id, st.drawing_id, d.file_name,
                   st.feature_name, st.nominal_value,
                   st.upper_tolerance, st.lower_tolerance,
                   COALESCE(st.verification_status, 'Pending')
            FROM   semantic_tolerances st
            JOIN   drawings d ON d.drawing_id = st.drawing_id
            WHERE  st.drawing_id = ?
            ORDER  BY st.id
        ''', (drawing_id,))
    else:
        cur.execute('''
            SELECT st.id, st.drawing_id, d.file_name,
                   st.feature_name, st.nominal_value,
                   st.upper_tolerance, st.lower_tolerance,
                   COALESCE(st.verification_status, 'Pending')
            FROM   semantic_tolerances st
            JOIN   drawings d ON d.drawing_id = st.drawing_id
            ORDER  BY st.drawing_id, st.id
        ''')
    vlm_rows = cur.fetchall()

    # 3D parameters — filtered by assembly
    if assembly_id is not None:
        cur.execute('''
            SELECT cp.id, cp.assembly_id, ca.assembly_name,
                   cp.publication_name, cp.part_instance,
                   cp.current_value, cp.unit
            FROM   catia_parameters cp
            JOIN   catia_assemblies ca ON ca.assembly_id = cp.assembly_id
            WHERE  cp.assembly_id = ?
            ORDER  BY cp.id
        ''', (assembly_id,))
    else:
        cur.execute('''
            SELECT cp.id, cp.assembly_id, ca.assembly_name,
                   cp.publication_name, cp.part_instance,
                   cp.current_value, cp.unit
            FROM   catia_parameters cp
            JOIN   catia_assemblies ca ON ca.assembly_id = cp.assembly_id
            ORDER  BY cp.assembly_id, cp.id
        ''')
    cad_rows = cur.fetchall()

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
    NAV  = "#1c3a6e"   # dark navy (matches master sidebar)

    # clear any previous content
    for w in parent_frame.winfo_children():
        w.destroy()
    parent_frame.configure(bg=BG)

    # ── Load filter options ───────────────────────────────────────────────
    drawing_opts, assembly_opts = get_filter_options()

    active_drawing_id  = drawing_opts[0][0]  if drawing_opts  else None
    active_assembly_id = assembly_opts[0][0] if assembly_opts else None
    vlm_rows, cad_rows, mapping_rows = get_filtered_data(active_drawing_id, active_assembly_id)

    # friendly inline warning if DB is empty
    if not drawing_opts or not assembly_opts:
        msg = ("No 2D drawing data found — run Perception first."
               if not drawing_opts else
               "No 3D assembly data found — run CAD Extraction first.")
        tk.Label(parent_frame, text=f"⚠  {msg}",
                 font=("Segoe UI", 12), bg=BG, fg="#b45309",
                 anchor="center").pack(expand=True)
        return

    mapped_vlm_ids = {r[1] for r in mapping_rows}
    mapped_cad_ids = {r[2] for r in mapping_rows}

    # ── Header bar ───────────────────────────────────────────────────────
    hdr = tk.Frame(parent_frame, bg=NAV, height=36)
    hdr.pack(fill=tk.X)
    hdr.pack_propagate(False)
    tk.Label(hdr, text="  🔗  2D ↔ 3D Parameter Mapper",
             font=('Segoe UI', 11, 'bold'), bg=NAV, fg="white").pack(side=tk.LEFT, padx=14, fill=tk.Y)

    # ── Context Filter bar ───────────────────────────────────────────────
    flt_frame = tk.Frame(parent_frame, bg="#dce8f7", pady=5)
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

    tk.Label(flt_frame, text="3D Assembly:", font=('Segoe UI', 9), bg="#dce8f7").pack(side=tk.LEFT)
    asm_var = tk.StringVar()
    asm_cb  = ttk.Combobox(flt_frame, textvariable=asm_var, state="readonly",
                            width=34, font=('Segoe UI', 9))
    asm_cb['values'] = [a[1] for a in assembly_opts]
    asm_cb.current(0)
    asm_cb.pack(side=tk.LEFT, padx=(2, 12))

    filter_lbl = tk.Label(flt_frame, text="", font=('Segoe UI', 8, 'italic'),
                          bg="#dce8f7", fg="#555")
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

    vlm_cols = ("vlm_id", "feature_name", "nominal", "tolerance", "status")
    vlm_tree = ttk.Treeview(left_frame, columns=vlm_cols, show="headings",
                             selectmode="browse", height=10)
    vlm_tree.heading("vlm_id",       text="ID")
    vlm_tree.heading("feature_name", text="Feature Name")
    vlm_tree.heading("nominal",      text="Nominal")
    vlm_tree.heading("tolerance",    text="Tolerance")
    vlm_tree.heading("status",       text="Audit Status")
    vlm_tree.column("vlm_id",       width=35,  anchor=tk.CENTER, stretch=False)
    vlm_tree.column("feature_name", width=175, anchor=tk.W)
    vlm_tree.column("nominal",      width=65,  anchor=tk.CENTER, stretch=False)
    vlm_tree.column("tolerance",    width=100, anchor=tk.CENTER, stretch=False)
    vlm_tree.column("status",       width=110, anchor=tk.CENTER, stretch=False)
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

    cad_cols = ("cad_id", "part_instance", "parameter", "value")
    cad_tree = ttk.Treeview(right_frame, columns=cad_cols, show="headings",
                             selectmode="browse", height=10)
    cad_tree.heading("cad_id",        text="ID")
    cad_tree.heading("part_instance", text="Part Instance")
    cad_tree.heading("parameter",     text="Parameter")
    cad_tree.heading("value",         text="Value")
    cad_tree.column("cad_id",        width=35,  anchor=tk.CENTER, stretch=False)
    cad_tree.column("part_instance", width=155, anchor=tk.W)
    cad_tree.column("parameter",     width=175, anchor=tk.W)
    cad_tree.column("value",         width=85,  anchor=tk.CENTER, stretch=False)
    cad_sb = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=cad_tree.yview)
    cad_tree.configure(yscrollcommand=cad_sb.set)
    cad_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    cad_sb.pack(side=tk.LEFT, fill=tk.Y)

    # ── Middle: Link button ───────────────────────────────────────────────
    mid_frame = tk.Frame(parent_frame, bg=BG)
    mid_frame.pack(fill=tk.X, padx=8, pady=5)

    link_btn = tk.Button(mid_frame, text="🔗  Link Selected",
                         font=('Segoe UI', 10, 'bold'),
                         bg=NAV, fg="white",
                         padx=18, pady=5, relief=tk.FLAT, cursor="hand2")
    link_btn.pack(side=tk.LEFT, padx=(4, 0))

    status_lbl = tk.Label(mid_frame, text="", font=('Segoe UI', 9),
                          bg=BG, fg="#555")
    status_lbl.pack(side=tk.LEFT, padx=12)

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
    def populate_top_tables():
        for tree in (vlm_tree, cad_tree):
            for item in tree.get_children():
                tree.delete(item)
        vlm_map.clear(); cad_map.clear()
        _STATUS_TAG = {'Verified': 'verified', 'Pending': 'pending'}
        for row in vlm_rows:
            if row[0] in mapped_vlm_ids:
                continue
            tol    = f"+{row[5]} / {row[6]}"
            status = row[7] if len(row) > 7 else 'Pending'
            tag    = _STATUS_TAG.get(status, 'rejected')
            iid    = str(row[0])
            vlm_tree.insert("", tk.END, iid=iid,
                             values=(row[0], row[3], row[4], tol, status), tags=(tag,))
            vlm_map[iid] = row
        for row in cad_rows:
            if row[0] in mapped_cad_ids:
                continue
            val_str = f"{row[5]} {row[6]}" if row[6] else str(row[5])
            iid = str(row[0])
            cad_tree.insert("", tk.END, iid=iid,
                             values=(row[0], row[4], row[3], val_str))
            cad_map[iid] = row

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

    # ── Context filter change ─────────────────────────────────────────────
    def on_filter_change(*_):
        nonlocal active_drawing_id, active_assembly_id
        dwg_label = dwg_var.get()
        asm_label = asm_var.get()
        active_drawing_id  = next((d[0] for d in drawing_opts  if d[1] == dwg_label), None)
        active_assembly_id = next((a[0] for a in assembly_opts if a[1] == asm_label), None)
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
        on_filter_change()
        status_lbl.config(text="✅  DB refreshed.", fg="#005500")

    dwg_cb.bind("<<ComboboxSelected>>", on_filter_change)
    asm_cb.bind("<<ComboboxSelected>>", on_filter_change)
    refresh_btn.config(command=on_refresh)
    on_filter_change()

    # ── Link action ───────────────────────────────────────────────────────
    def on_link():
        v_sel = vlm_tree.selection()
        c_sel = cad_tree.selection()
        if not v_sel or not c_sel:
            status_lbl.config(text="⚠  Select one row from each table first.", fg="#aa4400")
            return
        v_row = vlm_map[v_sel[0]]
        c_row = cad_map[c_sel[0]]
        new_id, vlm_text, cad_text = save_mapping(v_row, c_row)
        mapped_vlm_ids.add(v_row[0]); mapped_cad_ids.add(c_row[0])
        mapping_rows.append((new_id, v_row[0], c_row[0], vlm_text, cad_text, "now"))
        map_iids[str(new_id)] = (v_row[0], c_row[0])
        vlm_tree.delete(v_sel[0]); del vlm_map[v_sel[0]]
        cad_tree.delete(c_sel[0]); del cad_map[c_sel[0]]
        map_tree.insert("", tk.END, iid=str(new_id), values=(new_id, vlm_text, cad_text))
        status_lbl.config(text=f"✅  Linked: {v_row[3]}  ↔  {c_row[3]}", fg="#005500")
        filter_lbl.config(
            text=f"  {len(vlm_map)} dim(s)  |  {len(cad_map)} param(s)  |  {len(map_iids)} mapping(s)")

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
                tol    = f"+{row[5]} / {row[6]}"
                status = row[7] if len(row) > 7 else 'Pending'
                tag    = _STATUS_TAG.get(status, 'rejected')
                iid    = str(row[0])
                vlm_tree.insert("", tk.END, iid=iid,
                                values=(row[0], row[3], row[4], tol, status), tags=(tag,))
                vlm_map[iid] = row
                break
        for row in cad_rows:
            if row[0] == cad_id:
                val_str = f"{row[5]} {row[6]}" if row[6] else str(row[5])
                iid = str(row[0])
                cad_tree.insert("", tk.END, iid=iid,
                                values=(row[0], row[4], row[3], val_str))
                cad_map[iid] = row
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
