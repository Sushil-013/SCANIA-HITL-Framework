import argparse
import json
import sqlite3
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # pragma: no cover - UI import is environment-dependent
    tk = None
    messagebox = None
    ttk = None

try:
    from .user_parameter_agent import safe_text, utc_now_iso
except ImportError:
    from openai_pipeline.pipeline_stack.catia_agents.user_parameter_agent import safe_text, utc_now_iso

try:
    from ..helpers.sqlite_importer import connect_db, create_tables, migrate_tables
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.sqlite_importer import connect_db, create_tables, migrate_tables

try:
    from ..helpers.tk_ui_sizing import apply_standard_window
except ImportError:
    from openai_pipeline.pipeline_stack.helpers.tk_ui_sizing import apply_standard_window


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Open a UI that lets you manually link extracted 2D drawing dimensions to "
            "CATIA published parameters stored in the drawing pipeline SQLite database."
        )
    )
    parser.add_argument("--db", required=True, type=str, help="Path to the drawing pipeline SQLite database.")
    parser.add_argument("--document_id", default=None, type=str, help="Optional drawing document_id filter.")
    parser.add_argument("--catia_part_number", default=None, type=str, help="Optional CATIA part number filter.")
    parser.add_argument("--summary_json", default=None, type=str, help="Optional JSON summary output path.")
    return parser.parse_args()


def resolve_document_identity(conn, document_id=None):
    sql = """
        SELECT document_id, part_number
        FROM documents
    """
    params = []
    if document_id:
        sql += " WHERE document_id = ?"
        params.append(document_id)
    sql += " ORDER BY document_id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError("No drawing document row was found in the SQLite database.")
    return {"document_id": row["document_id"], "part_number": row["part_number"]}


def load_dimensions(conn, document_id):
    rows = conn.execute(
        """
        SELECT
            d.dimension_id,
            d.view_id,
            d.raw_text,
            d.nominal_value,
            d.tolerance_plus,
            d.tolerance_minus,
            d.status,
            d.human_review_status,
            cdl.catia_part_number AS linked_catia_part_number,
            cdl.publication_name AS linked_publication_name,
            cdl.parameter_name AS linked_parameter_name
        FROM dimensions AS d
        LEFT JOIN catia_dimension_links AS cdl
            ON cdl.document_id = d.document_id
           AND cdl.dimension_id = d.dimension_id
        WHERE d.document_id = ?
        ORDER BY
            CASE
                WHEN LOWER(d.dimension_id) GLOB 'd[0-9]*' THEN CAST(SUBSTR(LOWER(d.dimension_id), 2) AS INTEGER)
                ELSE 999999
            END,
            d.dimension_id
        """,
        (document_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_published_parameters(conn, document_id, catia_part_number=None):
    sql = """
        SELECT
            cpp.catia_part_number,
            cpp.publication_name,
            cpp.parameter_path,
            cpp.parameter_name,
            cpp.value_text,
            cpp.formula_text,
            cpp.active_text,
            cpp.parameter_type,
            cdl.dimension_id AS linked_dimension_id
        FROM catia_published_parameters AS cpp
        LEFT JOIN catia_dimension_links AS cdl
            ON cdl.document_id = cpp.document_id
           AND cdl.catia_part_number = cpp.catia_part_number
           AND cdl.publication_name = cpp.publication_name
        WHERE cpp.document_id = ?
    """
    params = [document_id]
    if catia_part_number:
        sql += " AND cpp.catia_part_number = ?"
        params.append(catia_part_number)
    sql += """
        ORDER BY
            cpp.catia_part_number,
            cpp.publication_name
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def load_existing_links(conn, document_id):
    rows = conn.execute(
        """
        SELECT
            dimension_id,
            catia_part_number,
            publication_name,
            parameter_name,
            parameter_path,
            parameter_value_text
        FROM catia_dimension_links
        WHERE document_id = ?
        ORDER BY dimension_id
        """,
        (document_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def refresh_dimension_pairs(conn, document_id=None):
    params = []
    delete_where = ""
    select_where = ""
    if document_id:
        delete_where = " WHERE document_id = ?"
        select_where = " WHERE cdl.document_id = ?"
        params.append(document_id)

    conn.execute("DELETE FROM catia_dimension_pairs{}".format(delete_where), params)
    conn.execute(
        """
        INSERT INTO catia_dimension_pairs (
            document_id,
            drawing_part_number,
            dimension_id,
            dimension_raw_text,
            nominal_value,
            tolerance_plus,
            tolerance_minus,
            catia_part_number,
            catia_instance_name,
            catia_tree_path,
            publication_name,
            parameter_path,
            parameter_name,
            parameter_value_text,
            formula_text,
            active_text,
            user_access_mode,
            parameter_type,
            source_document_name,
            source_document_path,
            source_document_type,
            link_status,
            link_notes,
            created_at_utc,
            updated_at_utc
        )
        SELECT
            cdl.document_id,
            cdl.drawing_part_number,
            cdl.dimension_id,
            cdl.dimension_raw_text,
            cdl.nominal_value,
            cdl.tolerance_plus,
            cdl.tolerance_minus,
            cdl.catia_part_number,
            cpp.catia_instance_name,
            cpp.catia_tree_path,
            cdl.publication_name,
            cdl.parameter_path,
            cdl.parameter_name,
            cdl.parameter_value_text,
            cpp.formula_text,
            cpp.active_text,
            cpp.user_access_mode,
            cpp.parameter_type,
            cpp.source_document_name,
            cpp.source_document_path,
            cpp.source_document_type,
            cdl.link_status,
            cdl.link_notes,
            cdl.created_at_utc,
            cdl.updated_at_utc
        FROM catia_dimension_links AS cdl
        LEFT JOIN catia_published_parameters AS cpp
            ON cpp.document_id = cdl.document_id
           AND cpp.catia_part_number = cdl.catia_part_number
           AND cpp.publication_name = cdl.publication_name
        {}
        ORDER BY cdl.dimension_id
        """.format(select_where),
        params,
    )
    conn.commit()


def upsert_dimension_link(
    conn,
    document_id,
    drawing_part_number,
    dimension_row,
    parameter_row,
):
    now_text = utc_now_iso()
    conn.execute(
        """
        DELETE FROM catia_dimension_links
        WHERE document_id = ?
          AND (
                dimension_id = ?
                OR (catia_part_number = ? AND publication_name = ?)
              )
        """,
        (
            document_id,
            dimension_row["dimension_id"],
            parameter_row["catia_part_number"],
            parameter_row["publication_name"],
        ),
    )
    conn.execute(
        """
        INSERT INTO catia_dimension_links (
            document_id,
            drawing_part_number,
            dimension_id,
            dimension_raw_text,
            nominal_value,
            tolerance_plus,
            tolerance_minus,
            catia_part_number,
            publication_name,
            parameter_path,
            parameter_name,
            parameter_value_text,
            link_status,
            link_notes,
            created_at_utc,
            updated_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'linked', ?, ?, ?)
        """,
        (
            document_id,
            drawing_part_number,
            dimension_row["dimension_id"],
            dimension_row["raw_text"],
            dimension_row["nominal_value"],
            dimension_row["tolerance_plus"],
            dimension_row["tolerance_minus"],
            parameter_row["catia_part_number"],
            parameter_row["publication_name"],
            parameter_row["parameter_path"],
            parameter_row["parameter_name"],
            parameter_row["value_text"],
            "Linked from manual UI selection.",
            now_text,
            now_text,
        ),
    )
    refresh_dimension_pairs(conn, document_id=document_id)


def delete_dimension_link(conn, document_id, dimension_id):
    conn.execute(
        """
        DELETE FROM catia_dimension_links
        WHERE document_id = ? AND dimension_id = ?
        """,
        (document_id, dimension_id),
    )
    refresh_dimension_pairs(conn, document_id=document_id)


def build_summary(conn, document_id, catia_part_number=None):
    total_dimensions = conn.execute(
        "SELECT COUNT(*) FROM dimensions WHERE document_id = ?",
        (document_id,),
    ).fetchone()[0]
    linked_dimensions = conn.execute(
        "SELECT COUNT(*) FROM catia_dimension_links WHERE document_id = ?",
        (document_id,),
    ).fetchone()[0]
    paired_dimensions = conn.execute(
        "SELECT COUNT(*) FROM catia_dimension_pairs WHERE document_id = ?",
        (document_id,),
    ).fetchone()[0]

    if catia_part_number:
        total_published = conn.execute(
            """
            SELECT COUNT(*)
            FROM catia_published_parameters
            WHERE document_id = ? AND catia_part_number = ?
            """,
            (document_id, catia_part_number),
        ).fetchone()[0]
    else:
        total_published = conn.execute(
            """
            SELECT COUNT(*)
            FROM catia_published_parameters
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()[0]

    unlinked_dimension_rows = conn.execute(
        """
        SELECT d.dimension_id
        FROM dimensions AS d
        LEFT JOIN catia_dimension_links AS cdl
            ON cdl.document_id = d.document_id
           AND cdl.dimension_id = d.dimension_id
        WHERE d.document_id = ? AND cdl.dimension_id IS NULL
        ORDER BY d.dimension_id
        """,
        (document_id,),
    ).fetchall()

    return {
        "status": "completed",
        "document_id": document_id,
        "catia_part_number": catia_part_number,
        "total_dimensions": int(total_dimensions),
        "linked_dimensions": int(linked_dimensions),
        "unlinked_dimensions": int(max(0, total_dimensions - linked_dimensions)),
        "total_published_parameters": int(total_published),
        "paired_dimensions": int(paired_dimensions),
        "all_dimensions_linked": bool(total_dimensions > 0 and linked_dimensions == total_dimensions),
        "unlinked_dimension_ids": [row[0] for row in unlinked_dimension_rows],
        "table_name": "catia_dimension_links",
        "paired_table_name": "catia_dimension_pairs",
    }


class DimensionLinkApp:
    def __init__(self, root, db_path, document_id, drawing_part_number, catia_part_number=None):
        self.root = root
        self.db_path = Path(db_path).resolve()
        self.document_id = document_id
        self.drawing_part_number = drawing_part_number
        self.catia_part_number = catia_part_number
        self.summary = None

        self.dimension_rows = []
        self.parameter_rows = []
        self.dimension_lookup = {}
        self.parameter_lookup = {}

        self.root.title("CATIA Dimension Link UI")
        apply_standard_window(self.root)

        self.status_var = tk.StringVar(value="Loading dimension and CATIA parameter data...")
        self.summary_var = tk.StringVar(value="")

        self._build_ui()
        self.refresh_tables()

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.configure("CatiaLink.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 12))
        except Exception:
            pass

        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=(
                "Select one 2D dimension on the left and one CATIA published parameter on the right, "
                "then click Link Selected."
            ),
            wraplength=1400,
        ).pack(anchor="w")
        ttk.Label(container, textvariable=self.status_var, foreground="#2f4f4f").pack(anchor="w", pady=(6, 8))
        ttk.Label(container, textvariable=self.summary_var, foreground="#1f5f3f").pack(anchor="w", pady=(0, 10))

        paned = ttk.Panedwindow(container, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left_frame = ttk.LabelFrame(paned, text="2D Extracted Dimensions", padding=8)
        middle_frame = ttk.Frame(paned, padding=12)
        right_frame = ttk.LabelFrame(paned, text="CATIA Published Parameters", padding=8)
        paned.add(left_frame, weight=7)
        paned.add(middle_frame, weight=2)
        paned.add(right_frame, weight=7)

        dimension_columns = ("dimension_id", "raw_text", "nominal", "tol_plus", "tol_minus", "linked_to")
        self.dimension_tree = ttk.Treeview(left_frame, columns=dimension_columns, show="headings", height=28)
        for column, heading, width in (
            ("dimension_id", "ID", 80),
            ("raw_text", "Raw Text", 320),
            ("nominal", "Nominal", 90),
            ("tol_plus", "Tol +", 90),
            ("tol_minus", "Tol -", 90),
            ("linked_to", "Linked CATIA Publication", 220),
        ):
            self.dimension_tree.heading(column, text=heading)
            self.dimension_tree.column(column, width=width, anchor="w")
        dim_scroll_y = ttk.Scrollbar(left_frame, orient="vertical", command=self.dimension_tree.yview)
        dim_scroll_x = ttk.Scrollbar(left_frame, orient="horizontal", command=self.dimension_tree.xview)
        self.dimension_tree.configure(yscrollcommand=dim_scroll_y.set, xscrollcommand=dim_scroll_x.set)
        self.dimension_tree.pack(side="left", fill="both", expand=True)
        dim_scroll_y.pack(side="right", fill="y")
        dim_scroll_x.pack(side="bottom", fill="x")

        parameter_columns = ("publication_name", "parameter_name", "parameter_path", "value_text", "parameter_type", "linked_to")
        self.parameter_tree = ttk.Treeview(right_frame, columns=parameter_columns, show="headings", height=28)
        for column, heading, width in (
            ("publication_name", "Publication", 220),
            ("parameter_name", "Parameter", 180),
            ("parameter_path", "Parameter Path", 280),
            ("value_text", "Value / Formula", 180),
            ("parameter_type", "Type", 120),
            ("linked_to", "Linked 2D Dimension", 160),
        ):
            self.parameter_tree.heading(column, text=heading)
            self.parameter_tree.column(column, width=width, anchor="w")
        param_scroll_y = ttk.Scrollbar(right_frame, orient="vertical", command=self.parameter_tree.yview)
        param_scroll_x = ttk.Scrollbar(right_frame, orient="horizontal", command=self.parameter_tree.xview)
        self.parameter_tree.configure(yscrollcommand=param_scroll_y.set, xscrollcommand=param_scroll_x.set)
        self.parameter_tree.pack(side="left", fill="both", expand=True)
        param_scroll_y.pack(side="right", fill="y")
        param_scroll_x.pack(side="bottom", fill="x")

        ttk.Button(middle_frame, text="Link Selected", command=self.link_selected, style="CatiaLink.TButton", width=18).pack(fill="x", pady=(180, 10))
        ttk.Button(middle_frame, text="Unlink Dimension", command=self.unlink_selected_dimension, style="CatiaLink.TButton", width=18).pack(fill="x", pady=10)
        ttk.Button(middle_frame, text="Refresh", command=self.refresh_tables, style="CatiaLink.TButton", width=18).pack(fill="x", pady=10)
        ttk.Button(middle_frame, text="Done", command=self.finish, style="CatiaLink.TButton", width=18).pack(fill="x", pady=(28, 10))

        self.dimension_tree.tag_configure("linked", background="#e8f6ea")
        self.parameter_tree.tag_configure("linked", background="#eef3fb")

    def refresh_tables(self):
        conn = connect_db(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            create_tables(conn)
            migrate_tables(conn)
            self.dimension_rows = load_dimensions(conn, self.document_id)
            self.parameter_rows = load_published_parameters(conn, self.document_id, self.catia_part_number)
            summary = build_summary(conn, self.document_id, self.catia_part_number)
        finally:
            conn.close()

        self.dimension_lookup = {}
        self.parameter_lookup = {}

        for item in self.dimension_tree.get_children():
            self.dimension_tree.delete(item)
        for item in self.parameter_tree.get_children():
            self.parameter_tree.delete(item)

        for row in self.dimension_rows:
            item_id = row["dimension_id"]
            self.dimension_lookup[item_id] = row
            linked_to = row.get("linked_publication_name") or ""
            tags = ("linked",) if linked_to else ()
            self.dimension_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row.get("dimension_id") or "",
                    row.get("raw_text") or "",
                    row.get("nominal_value") if row.get("nominal_value") is not None else "",
                    row.get("tolerance_plus") if row.get("tolerance_plus") is not None else "",
                    row.get("tolerance_minus") if row.get("tolerance_minus") is not None else "",
                    linked_to,
                ),
                tags=tags,
            )

        for row in self.parameter_rows:
            item_id = "{}::{}".format(row.get("catia_part_number") or "", row.get("publication_name") or "")
            self.parameter_lookup[item_id] = row
            linked_to = row.get("linked_dimension_id") or ""
            tags = ("linked",) if linked_to else ()
            parameter_display = row.get("parameter_name") or row.get("publication_name") or ""
            parameter_path = row.get("parameter_path") or parameter_display
            value_display = (
                row.get("value_text")
                or row.get("formula_text")
                or row.get("active_text")
                or ""
            )
            self.parameter_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row.get("publication_name") or "",
                    parameter_display,
                    parameter_path,
                    value_display,
                    row.get("parameter_type") or "",
                    linked_to,
                ),
                tags=tags,
            )

        status_text = "Loaded {} 2D dimensions and {} CATIA published parameters.".format(
            len(self.dimension_rows),
            len(self.parameter_rows),
        )
        if self.catia_part_number:
            status_text += " Filtered CATIA part: {}.".format(self.catia_part_number)
        self.status_var.set(status_text)
        self.summary_var.set(
            "Linked {}/{} dimensions. {}".format(
                summary["linked_dimensions"],
                summary["total_dimensions"],
                "All extracted dimensions are linked."
                if summary["all_dimensions_linked"]
                else "Some dimensions still need mapping.",
            )
        )
        self.summary = summary

    def link_selected(self):
        dimension_selection = self.dimension_tree.selection()
        parameter_selection = self.parameter_tree.selection()
        if not dimension_selection:
            messagebox.showerror("Missing Dimension", "Select one 2D dimension to link.")
            return
        if not parameter_selection:
            messagebox.showerror("Missing CATIA Parameter", "Select one CATIA published parameter to link.")
            return

        dimension_row = self.dimension_lookup.get(dimension_selection[0])
        parameter_row = self.parameter_lookup.get(parameter_selection[0])
        if dimension_row is None or parameter_row is None:
            messagebox.showerror("Selection Error", "Could not resolve the selected rows.")
            return

        replacement_notes = []
        if dimension_row.get("linked_publication_name"):
            replacement_notes.append(
                "Dimension {} is currently linked to {}.".format(
                    dimension_row["dimension_id"],
                    dimension_row["linked_publication_name"],
                )
            )
        if parameter_row.get("linked_dimension_id"):
            replacement_notes.append(
                "Publication {} is currently linked to {}.".format(
                    parameter_row["publication_name"],
                    parameter_row["linked_dimension_id"],
                )
            )
        if replacement_notes:
            proceed = messagebox.askyesno(
                "Replace Existing Link?",
                "\n".join(replacement_notes) + "\n\nReplace the existing link with the new selection?",
            )
            if not proceed:
                return

        conn = connect_db(str(self.db_path))
        try:
            create_tables(conn)
            migrate_tables(conn)
            upsert_dimension_link(
                conn,
                self.document_id,
                self.drawing_part_number,
                dimension_row,
                parameter_row,
            )
        finally:
            conn.close()

        self.refresh_tables()
        self.status_var.set(
            "Linked {} -> {}.".format(
                dimension_row["dimension_id"],
                parameter_row["publication_name"],
            )
        )

    def unlink_selected_dimension(self):
        dimension_selection = self.dimension_tree.selection()
        if not dimension_selection:
            messagebox.showerror("Missing Dimension", "Select one linked 2D dimension to unlink.")
            return

        dimension_row = self.dimension_lookup.get(dimension_selection[0])
        if dimension_row is None:
            messagebox.showerror("Selection Error", "Could not resolve the selected dimension.")
            return
        if not dimension_row.get("linked_publication_name"):
            messagebox.showinfo("Nothing To Unlink", "The selected dimension is not currently linked.")
            return

        conn = connect_db(str(self.db_path))
        try:
            create_tables(conn)
            migrate_tables(conn)
            delete_dimension_link(conn, self.document_id, dimension_row["dimension_id"])
        finally:
            conn.close()

        self.refresh_tables()
        self.status_var.set("Removed the link for {}.".format(dimension_row["dimension_id"]))

    def finish(self):
        conn = connect_db(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            create_tables(conn)
            migrate_tables(conn)
            refresh_dimension_pairs(conn, document_id=self.document_id)
            self.summary = build_summary(conn, self.document_id, self.catia_part_number)
        finally:
            conn.close()
        self.root.destroy()


def run_dimension_link_ui_from_sqlite(db_path, document_id=None, catia_part_number=None, summary_json=None, progress_callback=None):
    if tk is None or ttk is None or messagebox is None:
        raise RuntimeError("Tkinter is not available in this Python environment, so the link UI cannot be opened.")

    resolved_db_path = Path(db_path).resolve()
    conn = connect_db(str(resolved_db_path))
    conn.row_factory = sqlite3.Row
    try:
        create_tables(conn)
        migrate_tables(conn)
        identity = resolve_document_identity(conn, document_id=document_id)
        refresh_dimension_pairs(conn, document_id=identity["document_id"])
        published_rows = load_published_parameters(conn, identity["document_id"], catia_part_number)
        dimension_rows = load_dimensions(conn, identity["document_id"])
        if not dimension_rows:
            raise RuntimeError("No extracted 2D dimensions were found for document {}.".format(identity["document_id"]))
        if not published_rows:
            raise RuntimeError(
                "No CATIA published parameters were found for document {}{}.".format(
                    identity["document_id"],
                    "" if not catia_part_number else " and CATIA part {}".format(catia_part_number),
                )
            )
    finally:
        conn.close()

    if progress_callback:
        progress_callback(
            "Layer 2 Stage 03/03 - CATIA Dimension Link UI: opening the manual mapping window to pair extracted 2D dimensions with CATIA published parameters"
        )

    root = tk.Tk()
    app = DimensionLinkApp(
        root,
        db_path=resolved_db_path,
        document_id=identity["document_id"],
        drawing_part_number=identity["part_number"],
        catia_part_number=catia_part_number,
    )
    root.mainloop()

    summary = app.summary or {
        "status": "closed",
        "document_id": identity["document_id"],
        "catia_part_number": catia_part_number,
        "table_name": "catia_dimension_links",
    }
    summary["db_path"] = str(resolved_db_path)
    if summary_json:
        summary_path = Path(summary_json).resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["summary_json"] = str(summary_path)
    if progress_callback:
        progress_callback(
            "Layer 2 Completed 03/03 - CATIA post-run mapping finished: saved the final 2D-to-CATIA mapping table with {}/{} linked dimensions".format(
                summary.get("linked_dimensions", 0),
                summary.get("total_dimensions", 0),
            )
        )
    return summary


def main():
    args = parse_args()
    summary = run_dimension_link_ui_from_sqlite(
        db_path=args.db,
        document_id=args.document_id,
        catia_part_number=args.catia_part_number,
        summary_json=args.summary_json,
    )
    print(
        "CATIA dimension-link UI completed. Linked {}/{} dimensions in {}.".format(
            summary.get("linked_dimensions", 0),
            summary.get("total_dimensions", 0),
            summary.get("db_path"),
        )
    )


if __name__ == "__main__":
    main()
