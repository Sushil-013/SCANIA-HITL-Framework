"""
Pipeline stage 07:
sqlite_importer.py - imports the saved extraction JSON into SQLite.
"""

import argparse
import json
import sqlite3
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Import drawing extraction JSON into SQLite")
    parser.add_argument("--json", required=True, type=str, help="path to *_drawing_features.json")
    parser.add_argument(
        "--db",
        default=None,
        type=str,
        help="SQLite database path; defaults to drawing_data.db next to the JSON file",
    )
    return parser.parse_args()


def connect_db(db_path):
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def resolve_db_path(json_path, db_path=None):
    if db_path:
        return Path(db_path)
    return Path(json_path).resolve().parent / "drawing_data.db"


def resolve_part_number(payload):
    drawing_number = str(payload.get("drawing_number") or "").strip()
    document_id = str(payload.get("document_id") or "").strip()
    return drawing_number or document_id


def resolve_document_number(payload):
    return resolve_part_number(payload)


def create_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            part_number TEXT NOT NULL,
            document_number TEXT NOT NULL,
            drawing_role TEXT NOT NULL,
            drawing_name TEXT NOT NULL,
            drawing_number TEXT,
            source_file_name TEXT,
            source_json_path TEXT NOT NULL,
            unit TEXT,
            unit_inferred INTEGER NOT NULL,
            part_description TEXT,
            summary_text TEXT,
            title_block_json TEXT,
            bill_of_materials_json TEXT,
            notes_json TEXT
        );

        CREATE TABLE IF NOT EXISTS dimensions (
            document_id TEXT NOT NULL,
            part_number TEXT NOT NULL,
            view_id TEXT,
            dimension_id TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            dimension_type TEXT NOT NULL,
            nominal_value REAL,
            tolerance_class TEXT NOT NULL,
            tolerance_format TEXT NOT NULL,
            tolerance_pattern TEXT NOT NULL,
            tolerance_plus REAL,
            tolerance_minus REAL,
            status TEXT NOT NULL,
            is_unassigned INTEGER NOT NULL DEFAULT 0,
            human_review_status TEXT NOT NULL DEFAULT 'pending',
            human_reviewed_at TEXT,
            human_review_notes TEXT,
            UNIQUE(document_id, dimension_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS manual_dimensions (
            document_id TEXT NOT NULL,
            part_number TEXT NOT NULL,
            view_id TEXT,
            dimension_id TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            dimension_type TEXT NOT NULL,
            nominal_value REAL,
            tolerance_class TEXT NOT NULL,
            tolerance_format TEXT NOT NULL,
            tolerance_pattern TEXT NOT NULL,
            tolerance_plus REAL,
            tolerance_minus REAL,
            status TEXT NOT NULL,
            is_unassigned INTEGER NOT NULL DEFAULT 0,
            human_review_status TEXT NOT NULL DEFAULT 'approved',
            human_reviewed_at TEXT,
            human_review_notes TEXT,
            created_at TEXT,
            UNIQUE(document_id, dimension_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tolerances (
            tolerance_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            part_number TEXT NOT NULL,
            view_id TEXT,
            dimension_id TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            nominal_value REAL,
            tolerance_class TEXT NOT NULL,
            tolerance_format TEXT NOT NULL,
            tolerance_pattern TEXT NOT NULL,
            tolerance_plus REAL,
            tolerance_minus REAL,
            status TEXT NOT NULL,
            is_unassigned INTEGER NOT NULL DEFAULT 0,
            UNIQUE(document_id, dimension_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS feature_control_frames (
            fcf_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            part_number TEXT NOT NULL,
            view_id TEXT,
            fcf_id TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            status TEXT NOT NULL,
            is_unassigned INTEGER NOT NULL DEFAULT 0,
            UNIQUE(document_id, fcf_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_published_parameters (
            published_parameter_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT NOT NULL,
            catia_part_number TEXT NOT NULL,
            catia_instance_name TEXT,
            catia_tree_path TEXT,
            publication_name TEXT NOT NULL,
            parameter_path TEXT,
            parameter_name TEXT,
            value_text TEXT,
            formula_text TEXT,
            active_text TEXT,
            user_access_mode INTEGER,
            parameter_type TEXT,
            source_document_name TEXT,
            source_document_path TEXT,
            source_document_type TEXT,
            extracted_at_utc TEXT NOT NULL,
            UNIQUE(document_id, catia_part_number, publication_name),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dimension_links (
            link_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT NOT NULL,
            dimension_id TEXT NOT NULL,
            dimension_raw_text TEXT NOT NULL,
            nominal_value REAL,
            tolerance_plus REAL,
            tolerance_minus REAL,
            catia_part_number TEXT NOT NULL,
            publication_name TEXT NOT NULL,
            parameter_path TEXT,
            parameter_name TEXT,
            parameter_value_text TEXT,
            link_status TEXT NOT NULL DEFAULT 'linked',
            link_notes TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(document_id, dimension_id),
            UNIQUE(document_id, catia_part_number, publication_name),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dimension_pairs (
            pair_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT NOT NULL,
            dimension_id TEXT NOT NULL,
            dimension_raw_text TEXT NOT NULL,
            nominal_value REAL,
            tolerance_plus REAL,
            tolerance_minus REAL,
            catia_part_number TEXT NOT NULL,
            catia_instance_name TEXT,
            catia_tree_path TEXT,
            publication_name TEXT NOT NULL,
            parameter_path TEXT,
            parameter_name TEXT,
            parameter_value_text TEXT,
            formula_text TEXT,
            active_text TEXT,
            user_access_mode INTEGER,
            parameter_type TEXT,
            source_document_name TEXT,
            source_document_path TEXT,
            source_document_type TEXT,
            link_status TEXT NOT NULL DEFAULT 'linked',
            link_notes TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(document_id, dimension_id),
            UNIQUE(document_id, catia_part_number, publication_name),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_max (
            dmu_result_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT,
            analysis_mode TEXT,
            clearance_mm REAL,
            part_number TEXT,
            part_number_a TEXT,
            part_number_b TEXT,
            actuated_dimension_id TEXT,
            actuated_dimension_raw_text TEXT,
            actuated_catia_part_number TEXT,
            actuated_publication_name TEXT,
            actuated_parameter_name TEXT,
            actuated_parameter_path TEXT,
            target_value_mm REAL,
            results_json_path TEXT NOT NULL,
            results_csv_path TEXT,
            results_txt_path TEXT,
            row_number INTEGER NOT NULL,
            product1 TEXT,
            product2 TEXT,
            conflict_type TEXT,
            value_mm REAL,
            status TEXT,
            info TEXT,
            keep_text TEXT,
            comment_text TEXT,
            location_text TEXT,
            image_path TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(run_id, row_number),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_nominal (
            row_number INTEGER NOT NULL,
            product1 TEXT,
            product2 TEXT,
            conflict_type TEXT,
            value_mm REAL,
            status TEXT,
            info TEXT,
            keep_text TEXT,
            comment_text TEXT,
            location_text TEXT,
            image_path TEXT
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_nominal_link (
            nominal_link_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            nominal_rowid INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(run_id, row_number),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_paired (
            pair_index INTEGER NOT NULL,
            dimension_id TEXT,
            dimension_text TEXT,
            tolerance_mode TEXT NOT NULL,
            product1 TEXT,
            product2 TEXT,
            conflict_type TEXT,
            value_mm REAL,
            status TEXT,
            image_path TEXT
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_paired_link (
            paired_link_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            pair_index INTEGER NOT NULL,
            tolerance_mode TEXT NOT NULL,
            run_id TEXT,
            paired_rowid INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(document_id, pair_index, tolerance_mode),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_results_min (
            dmu_result_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT,
            analysis_mode TEXT,
            clearance_mm REAL,
            part_number TEXT,
            part_number_a TEXT,
            part_number_b TEXT,
            actuated_dimension_id TEXT,
            actuated_dimension_raw_text TEXT,
            actuated_catia_part_number TEXT,
            actuated_publication_name TEXT,
            actuated_parameter_name TEXT,
            actuated_parameter_path TEXT,
            target_value_mm REAL,
            results_json_path TEXT NOT NULL,
            results_csv_path TEXT,
            results_txt_path TEXT,
            row_number INTEGER NOT NULL,
            product1 TEXT,
            product2 TEXT,
            conflict_type TEXT,
            value_mm REAL,
            status TEXT,
            info TEXT,
            keep_text TEXT,
            comment_text TEXT,
            location_text TEXT,
            image_path TEXT,
            created_at_utc TEXT NOT NULL,
            UNIQUE(run_id, row_number),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_runs_max (
            dmu_run_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT,
            analysis_mode TEXT,
            clearance_mm REAL,
            part_number TEXT,
            part_number_a TEXT,
            part_number_b TEXT,
            actuated_dimension_id TEXT,
            actuated_dimension_raw_text TEXT,
            actuated_catia_part_number TEXT,
            actuated_publication_name TEXT,
            actuated_parameter_name TEXT,
            actuated_parameter_path TEXT,
            target_value_mm REAL,
            results_json_path TEXT NOT NULL,
            results_csv_path TEXT,
            results_txt_path TEXT,
            result_row_count INTEGER NOT NULL DEFAULT 0,
            clash_count INTEGER NOT NULL DEFAULT 0,
            clearance_count INTEGER NOT NULL DEFAULT 0,
            contact_count INTEGER NOT NULL DEFAULT 0,
            ui_opened INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_runs_nominal (
            dmu_run_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT,
            analysis_mode TEXT,
            clearance_mm REAL,
            part_number TEXT,
            part_number_a TEXT,
            part_number_b TEXT,
            actuated_dimension_id TEXT,
            actuated_dimension_raw_text TEXT,
            actuated_catia_part_number TEXT,
            actuated_publication_name TEXT,
            actuated_parameter_name TEXT,
            actuated_parameter_path TEXT,
            target_value_mm REAL,
            results_json_path TEXT NOT NULL,
            results_csv_path TEXT,
            results_txt_path TEXT,
            result_row_count INTEGER NOT NULL DEFAULT 0,
            clash_count INTEGER NOT NULL DEFAULT 0,
            clearance_count INTEGER NOT NULL DEFAULT 0,
            contact_count INTEGER NOT NULL DEFAULT 0,
            ui_opened INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catia_dmu_runs_min (
            dmu_run_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            drawing_part_number TEXT,
            analysis_mode TEXT,
            clearance_mm REAL,
            part_number TEXT,
            part_number_a TEXT,
            part_number_b TEXT,
            actuated_dimension_id TEXT,
            actuated_dimension_raw_text TEXT,
            actuated_catia_part_number TEXT,
            actuated_publication_name TEXT,
            actuated_parameter_name TEXT,
            actuated_parameter_path TEXT,
            target_value_mm REAL,
            results_json_path TEXT NOT NULL,
            results_csv_path TEXT,
            results_txt_path TEXT,
            result_row_count INTEGER NOT NULL DEFAULT 0,
            clash_count INTEGER NOT NULL DEFAULT 0,
            clearance_count INTEGER NOT NULL DEFAULT 0,
            contact_count INTEGER NOT NULL DEFAULT 0,
            ui_opened INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        );
        """
    )


def recreate_views(conn):
    conn.executescript(
        """
        DROP VIEW IF EXISTS drawing_registry;
        CREATE VIEW drawing_registry AS
        SELECT
            document_id,
            part_number,
            document_number,
            drawing_role,
            drawing_name,
            drawing_number,
            source_file_name,
            source_json_path
        FROM documents;

        DROP VIEW IF EXISTS part_registry;
        CREATE VIEW part_registry AS
        SELECT
            part_number,
            document_number,
            document_id,
            drawing_role,
            drawing_name,
            drawing_number,
            source_file_name,
            source_json_path,
            unit,
            unit_inferred,
            part_description,
            summary_text
        FROM documents
        ORDER BY part_number;

        DROP VIEW IF EXISTS part_dimensions;
        CREATE VIEW part_dimensions AS
        SELECT
            d.part_number,
            doc.drawing_name,
            doc.drawing_role,
            d.document_id,
            CASE WHEN d.is_unassigned = 1 THEN 'unassigned' ELSE COALESCE(d.view_id, 'unknown') END AS view_name,
            CASE WHEN d.is_unassigned = 1 THEN 'Unassigned dimension' ELSE '' END AS view_description,
            d.dimension_id,
            d.raw_text,
            d.dimension_type,
            CASE
                WHEN d.is_unassigned = 1 THEN 'unassigned dimension'
                WHEN COALESCE(d.view_id, '') <> '' THEN d.view_id
                ELSE d.dimension_type
            END AS dimension_description,
            d.nominal_value,
            d.tolerance_class,
            d.tolerance_format,
            d.tolerance_pattern,
            d.tolerance_plus,
            d.tolerance_minus,
            d.status,
            d.is_unassigned,
            d.human_review_status,
            d.human_reviewed_at,
            d.human_review_notes
        FROM dimensions AS d
        JOIN documents AS doc
            ON doc.document_id = d.document_id
        ORDER BY d.part_number, view_name, d.dimension_id;

        DROP VIEW IF EXISTS part_views;

        DROP VIEW IF EXISTS part_summary;
        CREATE VIEW part_summary AS
        SELECT
            doc.part_number,
            doc.document_number,
            doc.document_id,
            doc.drawing_name,
            doc.drawing_role,
            COUNT(DISTINCT COALESCE(d.view_id, f.view_id)) AS view_count,
            COUNT(DISTINCT d.dimension_id) AS dimension_count,
            COUNT(DISTINCT t.dimension_id) AS tolerance_count,
            COUNT(DISTINCT f.fcf_id) AS feature_control_frame_count
        FROM documents AS doc
        LEFT JOIN dimensions AS d
            ON d.document_id = doc.document_id
        LEFT JOIN tolerances AS t
            ON t.document_id = doc.document_id
        LEFT JOIN feature_control_frames AS f
            ON f.document_id = doc.document_id
        GROUP BY
            doc.part_number,
            doc.document_id,
            doc.drawing_name,
            doc.drawing_role
        ORDER BY doc.part_number;

        DROP VIEW IF EXISTS document_registry;
        CREATE VIEW document_registry AS
        SELECT
            document_number,
            document_id,
            drawing_name,
            drawing_role,
            drawing_number,
            source_file_name,
            source_json_path,
            unit,
            unit_inferred,
            part_description,
            summary_text
        FROM documents
        ORDER BY document_number;

        DROP VIEW IF EXISTS document_views;

        DROP VIEW IF EXISTS document_dimensions;
        CREATE VIEW document_dimensions AS
        SELECT
            d.document_id,
            CASE WHEN d.is_unassigned = 1 THEN 'unassigned' ELSE COALESCE(d.view_id, 'unknown') END AS view_name,
            CASE WHEN d.is_unassigned = 1 THEN 'Unassigned dimension' ELSE '' END AS view_description,
            d.dimension_id,
            d.raw_text,
            d.dimension_type,
            CASE
                WHEN d.is_unassigned = 1 THEN 'unassigned dimension'
                WHEN COALESCE(d.view_id, '') <> '' THEN d.view_id
                ELSE d.dimension_type
            END AS dimension_description,
            d.nominal_value,
            d.tolerance_class,
            d.tolerance_format,
            d.tolerance_pattern,
            d.tolerance_plus,
            d.tolerance_minus,
            d.status,
            d.is_unassigned,
            d.human_review_status,
            d.human_reviewed_at,
            d.human_review_notes
        FROM dimensions AS d
        ORDER BY d.document_id, view_name, d.dimension_id;

        DROP VIEW IF EXISTS document_tolerances;
        CREATE VIEW document_tolerances AS
        SELECT
            t.document_id,
            CASE WHEN t.is_unassigned = 1 THEN 'unassigned' ELSE COALESCE(t.view_id, 'unknown') END AS view_name,
            CASE WHEN t.is_unassigned = 1 THEN 'Unassigned tolerance' ELSE '' END AS view_description,
            t.dimension_id,
            t.raw_text,
            COALESCE(d.dimension_type, 'unknown') AS dimension_type,
            CASE
                WHEN t.is_unassigned = 1 THEN 'unassigned tolerance'
                WHEN COALESCE(t.view_id, '') <> '' THEN t.view_id
                ELSE COALESCE(d.dimension_type, 'tolerance')
            END AS tolerance_description,
            t.nominal_value,
            t.tolerance_class,
            t.tolerance_format,
            t.tolerance_pattern,
            t.tolerance_plus,
            t.tolerance_minus,
            t.status,
            t.is_unassigned,
            COALESCE(d.human_review_status, 'pending') AS human_review_status,
            d.human_reviewed_at,
            d.human_review_notes
        FROM tolerances AS t
        LEFT JOIN dimensions AS d
            ON d.document_id = t.document_id AND d.dimension_id = t.dimension_id
        ORDER BY t.document_id, view_name, t.dimension_id;

        DROP VIEW IF EXISTS document_feature_control_frames;
        CREATE VIEW document_feature_control_frames AS
        SELECT
            doc.document_number,
            document_id,
            view_id,
            fcf_id,
            raw_text,
            status,
            is_unassigned
        FROM feature_control_frames AS f
        JOIN documents AS doc
            ON doc.document_id = f.document_id
        ORDER BY doc.document_number, view_id, fcf_id;

        DROP VIEW IF EXISTS document_summary;
        CREATE VIEW document_summary AS
        SELECT
            doc.document_number,
            doc.document_id,
            doc.drawing_name,
            COUNT(DISTINCT COALESCE(d.view_id, f.view_id)) AS view_count,
            COUNT(DISTINCT d.dimension_id) AS dimension_count,
            COUNT(DISTINCT t.dimension_id) AS tolerance_count,
            COUNT(DISTINCT f.fcf_id) AS feature_control_frame_count
        FROM documents AS doc
        LEFT JOIN dimensions AS d
            ON d.document_id = doc.document_id
        LEFT JOIN tolerances AS t
            ON t.document_id = doc.document_id
        LEFT JOIN feature_control_frames AS f
            ON f.document_id = doc.document_id
        GROUP BY
            doc.document_number,
            doc.document_id,
            doc.drawing_name
        ORDER BY doc.document_number;

        DROP VIEW IF EXISTS document_catia_published_parameters;
        CREATE VIEW document_catia_published_parameters AS
        SELECT
            cpp.document_id,
            cpp.drawing_part_number,
            cpp.catia_part_number,
            cpp.catia_instance_name,
            cpp.catia_tree_path,
            cpp.publication_name,
            cpp.parameter_path,
            cpp.parameter_name,
            cpp.value_text,
            cpp.formula_text,
            cpp.active_text,
            cpp.user_access_mode,
            cpp.parameter_type,
            cpp.source_document_name,
            cpp.source_document_path,
            cpp.source_document_type,
            cpp.extracted_at_utc
        FROM catia_published_parameters AS cpp
        ORDER BY
            cpp.document_id,
            cpp.catia_part_number,
            cpp.publication_name;

        DROP VIEW IF EXISTS document_catia_dimension_links;
        CREATE VIEW document_catia_dimension_links AS
        SELECT
            cdl.document_id,
            cdl.drawing_part_number,
            cdl.dimension_id,
            cdl.dimension_raw_text,
            cdl.nominal_value,
            cdl.tolerance_plus,
            cdl.tolerance_minus,
            cdl.catia_part_number,
            cdl.publication_name,
            cdl.parameter_path,
            cdl.parameter_name,
            cdl.parameter_value_text,
            cdl.link_status,
            cdl.link_notes,
            cdl.created_at_utc,
            cdl.updated_at_utc
        FROM catia_dimension_links AS cdl
        ORDER BY
            cdl.document_id,
            cdl.dimension_id;

        DROP VIEW IF EXISTS document_catia_dimension_pairs;
        CREATE VIEW document_catia_dimension_pairs AS
        SELECT
            cdp.document_id,
            cdp.drawing_part_number,
            cdp.dimension_id,
            cdp.dimension_raw_text,
            cdp.nominal_value,
            cdp.tolerance_plus,
            cdp.tolerance_minus,
            cdp.catia_part_number,
            cdp.catia_instance_name,
            cdp.catia_tree_path,
            cdp.publication_name,
            cdp.parameter_path,
            cdp.parameter_name,
            cdp.parameter_value_text,
            cdp.formula_text,
            cdp.active_text,
            cdp.user_access_mode,
            cdp.parameter_type,
            cdp.source_document_name,
            cdp.source_document_path,
            cdp.source_document_type,
            cdp.link_status,
            cdp.link_notes,
            cdp.created_at_utc,
            cdp.updated_at_utc
        FROM catia_dimension_pairs AS cdp
        ORDER BY
            cdp.document_id,
            cdp.dimension_id;

        DROP VIEW IF EXISTS document_catia_dmu_results_max;
        CREATE VIEW document_catia_dmu_results_max AS
        SELECT
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            row_number,
            product1,
            product2,
            conflict_type,
            value_mm,
            status,
            info,
            keep_text,
            comment_text,
            location_text,
            image_path,
            created_at_utc
        FROM catia_dmu_results_max
        ORDER BY created_at_utc, row_number;

        DROP VIEW IF EXISTS document_catia_dmu_results_nominal;
        CREATE VIEW document_catia_dmu_results_nominal AS
        SELECT
            nominal_link.run_id,
            nominal_link.document_id,
            nominal.row_number,
            nominal.product1,
            nominal.product2,
            nominal.conflict_type,
            nominal.value_mm,
            nominal.status,
            nominal.info,
            nominal.keep_text,
            nominal.comment_text,
            nominal.location_text,
            nominal.image_path
        FROM catia_dmu_results_nominal AS nominal
        JOIN catia_dmu_results_nominal_link AS nominal_link
            ON nominal_link.nominal_rowid = nominal.rowid
        ORDER BY nominal_link.created_at_utc, nominal.row_number;

        DROP VIEW IF EXISTS document_catia_dmu_results_paired;
        CREATE VIEW document_catia_dmu_results_paired AS
        SELECT
            pair_index,
            dimension_id,
            dimension_text,
            tolerance_mode,
            product1,
            product2,
            conflict_type,
            value_mm,
            status,
            image_path
        FROM catia_dmu_results_paired
        ORDER BY
            pair_index,
            CASE
                WHEN LOWER(COALESCE(tolerance_mode, '')) = 'max' THEN 0
                WHEN LOWER(COALESCE(tolerance_mode, '')) = 'min' THEN 1
                ELSE 2
            END;

        DROP VIEW IF EXISTS document_catia_dmu_results_min;
        CREATE VIEW document_catia_dmu_results_min AS
        SELECT
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            row_number,
            product1,
            product2,
            conflict_type,
            value_mm,
            status,
            info,
            keep_text,
            comment_text,
            location_text,
            image_path,
            created_at_utc
        FROM catia_dmu_results_min
        ORDER BY created_at_utc, row_number;

        DROP VIEW IF EXISTS document_catia_dmu_runs_max;
        CREATE VIEW document_catia_dmu_runs_max AS
        SELECT
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            result_row_count,
            clash_count,
            clearance_count,
            contact_count,
            ui_opened,
            created_at_utc
        FROM catia_dmu_runs_max
        ORDER BY created_at_utc, run_id;

        DROP VIEW IF EXISTS document_catia_dmu_runs_nominal;
        CREATE VIEW document_catia_dmu_runs_nominal AS
        SELECT
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            result_row_count,
            clash_count,
            clearance_count,
            contact_count,
            ui_opened,
            created_at_utc
        FROM catia_dmu_runs_nominal
        ORDER BY created_at_utc, run_id;

        DROP VIEW IF EXISTS document_catia_dmu_runs_min;
        CREATE VIEW document_catia_dmu_runs_min AS
        SELECT
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            result_row_count,
            clash_count,
            clearance_count,
            contact_count,
            ui_opened,
            created_at_utc
        FROM catia_dmu_runs_min
        ORDER BY created_at_utc, run_id;
        """
    )


def ensure_part_number_indexes(conn):
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_part_number ON documents(part_number);
        CREATE INDEX IF NOT EXISTS idx_dimensions_part_number ON dimensions(part_number);
        CREATE INDEX IF NOT EXISTS idx_manual_dimensions_part_number ON manual_dimensions(part_number);
        CREATE INDEX IF NOT EXISTS idx_manual_dimensions_document_id ON manual_dimensions(document_id);
        CREATE INDEX IF NOT EXISTS idx_tolerances_part_number ON tolerances(part_number);
        CREATE INDEX IF NOT EXISTS idx_fcf_part_number ON feature_control_frames(part_number);
        CREATE INDEX IF NOT EXISTS idx_catia_published_parameters_document_id ON catia_published_parameters(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_published_parameters_part_number ON catia_published_parameters(catia_part_number);
        CREATE INDEX IF NOT EXISTS idx_catia_dimension_links_document_id ON catia_dimension_links(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dimension_links_dimension_id ON catia_dimension_links(document_id, dimension_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dimension_pairs_document_id ON catia_dimension_pairs(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dimension_pairs_dimension_id ON catia_dimension_pairs(document_id, dimension_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_max_document_id ON catia_dmu_results_max(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_max_run_id ON catia_dmu_results_max(run_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_nominal_link_document_id ON catia_dmu_results_nominal_link(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_nominal_link_run_id ON catia_dmu_results_nominal_link(run_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_nominal_link_rowid ON catia_dmu_results_nominal_link(nominal_rowid);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_paired_link_document_id ON catia_dmu_results_paired_link(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_paired_link_pair ON catia_dmu_results_paired_link(document_id, pair_index, tolerance_mode);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_paired_link_rowid ON catia_dmu_results_paired_link(paired_rowid);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_min_document_id ON catia_dmu_results_min(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_results_min_run_id ON catia_dmu_results_min(run_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_max_document_id ON catia_dmu_runs_max(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_max_run_id ON catia_dmu_runs_max(run_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_nominal_document_id ON catia_dmu_runs_nominal(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_nominal_run_id ON catia_dmu_runs_nominal(run_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_min_document_id ON catia_dmu_runs_min(document_id);
        CREATE INDEX IF NOT EXISTS idx_catia_dmu_runs_min_run_id ON catia_dmu_runs_min(run_id);
        """
    )


def ensure_document_number_indexes(conn):
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_document_number ON documents(document_number);
        """
    )


def ensure_column(conn, table_name, column_name, column_sql):
    columns = {row[1] for row in conn.execute("PRAGMA table_info({})".format(table_name))}
    if column_name not in columns:
        conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table_name, column_name, column_sql))


def table_columns(conn, table_name):
    return [row[1] for row in conn.execute("PRAGMA table_info({})".format(table_name))]


def drop_all_views(conn):
    view_names = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    ]
    for view_name in view_names:
        conn.execute("DROP VIEW IF EXISTS {}".format(view_name))


def migrate_catia_dmu_results_nominal_schema(conn):
    desired_columns = [
        "row_number",
        "product1",
        "product2",
        "conflict_type",
        "value_mm",
        "status",
        "info",
        "keep_text",
        "comment_text",
        "location_text",
        "image_path",
    ]
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "catia_dmu_results_nominal" not in existing_tables:
        return

    current_columns = table_columns(conn, "catia_dmu_results_nominal")
    if current_columns == desired_columns and "catia_dmu_results_nominal_link" in existing_tables:
        return

    if {"run_id", "document_id"}.issubset(set(current_columns)):
        old_rows = conn.execute(
            """
            SELECT
                run_id,
                document_id,
                row_number,
                product1,
                product2,
                conflict_type,
                value_mm,
                status,
                info,
                keep_text,
                comment_text,
                location_text,
                image_path,
                COALESCE(created_at_utc, '')
            FROM catia_dmu_results_nominal
            ORDER BY rowid
            """
        ).fetchall()
    else:
        select_columns = {column for column in current_columns}
        info_expr = "info" if "info" in select_columns else "NULL"
        keep_expr = "keep_text" if "keep_text" in select_columns else "NULL"
        comment_expr = "comment_text" if "comment_text" in select_columns else "NULL"
        location_expr = "location_text" if "location_text" in select_columns else "NULL"
        image_expr = "image_path" if "image_path" in select_columns else "NULL"
        old_rows = conn.execute(
            """
            SELECT
                nominal_link.run_id,
                nominal_link.document_id,
                nominal.row_number,
                nominal.product1,
                nominal.product2,
                nominal.conflict_type,
                nominal.value_mm,
                nominal.status,
                {info_expr},
                {keep_expr},
                {comment_expr},
                {location_expr},
                {image_expr},
                COALESCE(nominal_link.created_at_utc, '')
            FROM catia_dmu_results_nominal AS nominal
            LEFT JOIN catia_dmu_results_nominal_link AS nominal_link
                ON nominal_link.nominal_rowid = nominal.rowid
            ORDER BY nominal.rowid
            """.format(
                info_expr=info_expr,
                keep_expr=keep_expr,
                comment_expr=comment_expr,
                location_expr=location_expr,
                image_expr=image_expr,
            )
        ).fetchall()

    drop_all_views(conn)
    conn.execute("DROP TABLE IF EXISTS catia_dmu_results_nominal_link")
    conn.execute("ALTER TABLE catia_dmu_results_nominal RENAME TO catia_dmu_results_nominal_legacy")
    conn.execute(
        """
        CREATE TABLE catia_dmu_results_nominal (
            row_number INTEGER NOT NULL,
            product1 TEXT,
            product2 TEXT,
            conflict_type TEXT,
            value_mm REAL,
            status TEXT,
            info TEXT,
            keep_text TEXT,
            comment_text TEXT,
            location_text TEXT,
            image_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE catia_dmu_results_nominal_link (
            nominal_link_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            nominal_rowid INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(run_id, row_number),
            FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
        )
        """
    )

    for row in old_rows:
        insert_result = conn.execute(
            """
            INSERT INTO catia_dmu_results_nominal (
                row_number,
                product1,
                product2,
                conflict_type,
                value_mm,
                status,
                info,
                keep_text,
                comment_text,
                location_text,
                image_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                row[11],
                row[12],
            ),
        )
        conn.execute(
            """
            INSERT INTO catia_dmu_results_nominal_link (
                run_id,
                document_id,
                row_number,
                nominal_rowid,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                row[2],
                insert_result.lastrowid,
                row[13] or "",
            ),
        )

    conn.execute("DROP TABLE catia_dmu_results_nominal_legacy")


def clear_paired_dmu_results_for_document(conn, document_id):
    conn.execute(
        """
        DELETE FROM catia_dmu_results_paired
        WHERE rowid IN (
            SELECT paired_rowid
            FROM catia_dmu_results_paired_link
            WHERE document_id = ?
        )
        """,
        (document_id,),
    )
    conn.execute(
        "DELETE FROM catia_dmu_results_paired_link WHERE document_id = ?",
        (document_id,),
    )


def save_paired_dmu_results(
    conn,
    document_id,
    pair_index,
    dimension_id,
    dimension_text,
    max_run_id,
    max_result_row,
    min_run_id,
    min_result_row,
    created_at_utc,
):
    def _upsert_one_row(tolerance_mode, run_id, result_row):
        conn.execute(
            """
            DELETE FROM catia_dmu_results_paired
            WHERE rowid IN (
                SELECT paired_rowid
                FROM catia_dmu_results_paired_link
                WHERE document_id = ?
                  AND pair_index = ?
                  AND LOWER(COALESCE(tolerance_mode, '')) = LOWER(?)
            )
            """,
            (document_id, pair_index, tolerance_mode),
        )
        conn.execute(
            """
            DELETE FROM catia_dmu_results_paired_link
            WHERE document_id = ?
              AND pair_index = ?
              AND LOWER(COALESCE(tolerance_mode, '')) = LOWER(?)
            """,
            (document_id, pair_index, tolerance_mode),
        )

        result_row = result_row or {}
        insert_result = conn.execute(
            """
            INSERT INTO catia_dmu_results_paired (
                pair_index,
                dimension_id,
                dimension_text,
                tolerance_mode,
                product1,
                product2,
                conflict_type,
                value_mm,
                status,
                image_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair_index,
                dimension_id,
                dimension_text,
                tolerance_mode,
                result_row.get("Product1"),
                result_row.get("Product2"),
                result_row.get("Type"),
                result_row.get("Value"),
                result_row.get("Status"),
                result_row.get("Image"),
            ),
        )
        conn.execute(
            """
            INSERT INTO catia_dmu_results_paired_link (
                document_id,
                pair_index,
                tolerance_mode,
                run_id,
                paired_rowid,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                pair_index,
                tolerance_mode,
                run_id,
                insert_result.lastrowid,
                created_at_utc,
            ),
        )

    _upsert_one_row("Max", max_run_id, max_result_row)
    _upsert_one_row("Min", min_run_id, min_result_row)


def migrate_tables(conn):
    conn.execute("DROP TABLE IF EXISTS datums")
    conn.execute("DROP TABLE IF EXISTS views")
    ensure_column(conn, "documents", "part_number", "TEXT")
    ensure_column(conn, "documents", "document_number", "TEXT")
    ensure_column(conn, "documents", "drawing_role", "TEXT NOT NULL DEFAULT 'part'")
    ensure_column(conn, "documents", "drawing_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "documents", "drawing_number", "TEXT")
    ensure_column(conn, "documents", "source_file_name", "TEXT")
    ensure_column(conn, "documents", "part_description", "TEXT")
    ensure_column(conn, "documents", "summary_text", "TEXT")
    ensure_column(conn, "documents", "title_block_json", "TEXT")
    ensure_column(conn, "documents", "bill_of_materials_json", "TEXT")
    ensure_column(conn, "documents", "notes_json", "TEXT")
    ensure_column(conn, "dimensions", "part_number", "TEXT")
    ensure_column(conn, "dimensions", "tolerance_class", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "dimensions", "tolerance_format", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "dimensions", "tolerance_pattern", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "dimensions", "human_review_status", "TEXT NOT NULL DEFAULT 'pending'")
    ensure_column(conn, "dimensions", "human_reviewed_at", "TEXT")
    ensure_column(conn, "dimensions", "human_review_notes", "TEXT")
    ensure_column(conn, "manual_dimensions", "part_number", "TEXT")
    ensure_column(conn, "manual_dimensions", "view_id", "TEXT")
    ensure_column(conn, "manual_dimensions", "raw_text", "TEXT")
    ensure_column(conn, "manual_dimensions", "dimension_type", "TEXT")
    ensure_column(conn, "manual_dimensions", "nominal_value", "REAL")
    ensure_column(conn, "manual_dimensions", "tolerance_class", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "manual_dimensions", "tolerance_format", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "manual_dimensions", "tolerance_pattern", "TEXT NOT NULL DEFAULT 'none'")
    ensure_column(conn, "manual_dimensions", "tolerance_plus", "REAL")
    ensure_column(conn, "manual_dimensions", "tolerance_minus", "REAL")
    ensure_column(conn, "manual_dimensions", "status", "TEXT NOT NULL DEFAULT 'manual_added'")
    ensure_column(conn, "manual_dimensions", "is_unassigned", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "manual_dimensions", "human_review_status", "TEXT NOT NULL DEFAULT 'approved'")
    ensure_column(conn, "manual_dimensions", "human_reviewed_at", "TEXT")
    ensure_column(conn, "manual_dimensions", "human_review_notes", "TEXT")
    ensure_column(conn, "manual_dimensions", "created_at", "TEXT")
    ensure_column(conn, "tolerances", "part_number", "TEXT")
    ensure_column(conn, "feature_control_frames", "part_number", "TEXT")
    ensure_column(conn, "catia_published_parameters", "drawing_part_number", "TEXT")
    ensure_column(conn, "catia_published_parameters", "catia_part_number", "TEXT")
    ensure_column(conn, "catia_published_parameters", "catia_instance_name", "TEXT")
    ensure_column(conn, "catia_published_parameters", "catia_tree_path", "TEXT")
    ensure_column(conn, "catia_published_parameters", "publication_name", "TEXT")
    ensure_column(conn, "catia_published_parameters", "parameter_path", "TEXT")
    ensure_column(conn, "catia_published_parameters", "parameter_name", "TEXT")
    ensure_column(conn, "catia_published_parameters", "value_text", "TEXT")
    ensure_column(conn, "catia_published_parameters", "formula_text", "TEXT")
    ensure_column(conn, "catia_published_parameters", "active_text", "TEXT")
    ensure_column(conn, "catia_published_parameters", "user_access_mode", "INTEGER")
    ensure_column(conn, "catia_published_parameters", "parameter_type", "TEXT")
    ensure_column(conn, "catia_published_parameters", "source_document_name", "TEXT")
    ensure_column(conn, "catia_published_parameters", "source_document_path", "TEXT")
    ensure_column(conn, "catia_published_parameters", "source_document_type", "TEXT")
    ensure_column(conn, "catia_published_parameters", "extracted_at_utc", "TEXT")
    ensure_column(conn, "catia_dimension_links", "drawing_part_number", "TEXT")
    ensure_column(conn, "catia_dimension_links", "dimension_id", "TEXT")
    ensure_column(conn, "catia_dimension_links", "dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dimension_links", "nominal_value", "REAL")
    ensure_column(conn, "catia_dimension_links", "tolerance_plus", "REAL")
    ensure_column(conn, "catia_dimension_links", "tolerance_minus", "REAL")
    ensure_column(conn, "catia_dimension_links", "catia_part_number", "TEXT")
    ensure_column(conn, "catia_dimension_links", "publication_name", "TEXT")
    ensure_column(conn, "catia_dimension_links", "parameter_path", "TEXT")
    ensure_column(conn, "catia_dimension_links", "parameter_name", "TEXT")
    ensure_column(conn, "catia_dimension_links", "parameter_value_text", "TEXT")
    ensure_column(conn, "catia_dimension_links", "link_status", "TEXT")
    ensure_column(conn, "catia_dimension_links", "link_notes", "TEXT")
    ensure_column(conn, "catia_dimension_links", "created_at_utc", "TEXT")
    ensure_column(conn, "catia_dimension_links", "updated_at_utc", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_dimension_id", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_catia_part_number", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_publication_name", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_parameter_name", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "actuated_parameter_path", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "target_value_mm", "REAL")
    ensure_column(conn, "catia_dmu_results_max", "comment_text", "TEXT")
    ensure_column(conn, "catia_dmu_results_max", "location_text", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_dimension_id", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_catia_part_number", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_publication_name", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_parameter_name", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "actuated_parameter_path", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "target_value_mm", "REAL")
    ensure_column(conn, "catia_dmu_results_min", "comment_text", "TEXT")
    ensure_column(conn, "catia_dmu_results_min", "location_text", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_dimension_id", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_catia_part_number", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_publication_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_parameter_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "actuated_parameter_path", "TEXT")
    ensure_column(conn, "catia_dmu_runs_max", "target_value_mm", "REAL")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_dimension_id", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_catia_part_number", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_publication_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_parameter_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "actuated_parameter_path", "TEXT")
    ensure_column(conn, "catia_dmu_runs_nominal", "target_value_mm", "REAL")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_dimension_id", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_dimension_raw_text", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_catia_part_number", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_publication_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_parameter_name", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "actuated_parameter_path", "TEXT")
    ensure_column(conn, "catia_dmu_runs_min", "target_value_mm", "REAL")
    migrate_catia_dmu_results_nominal_schema(conn)
    remove_coordinate_columns(conn)
    backfill_part_numbers(conn)
    ensure_part_number_indexes(conn)
    ensure_document_number_indexes(conn)
    recreate_views(conn)


def backfill_part_numbers(conn):
    conn.execute(
        """
        UPDATE documents
        SET part_number = COALESCE(NULLIF(TRIM(drawing_number), ''), document_id)
        WHERE part_number IS NULL OR TRIM(part_number) = ''
        """
    )
    conn.execute(
        """
        UPDATE documents
        SET document_number = COALESCE(NULLIF(TRIM(drawing_number), ''), document_id)
        WHERE document_number IS NULL OR TRIM(document_number) = ''
        """
    )
    conn.execute(
        """
        UPDATE dimensions
        SET part_number = COALESCE(
            (SELECT documents.part_number FROM documents WHERE documents.document_id = dimensions.document_id),
            dimensions.document_id
        )
        WHERE part_number IS NULL OR TRIM(part_number) = ''
        """
    )
    conn.execute(
        """
        UPDATE manual_dimensions
        SET part_number = COALESCE(
            (SELECT documents.part_number FROM documents WHERE documents.document_id = manual_dimensions.document_id),
            manual_dimensions.document_id
        )
        WHERE part_number IS NULL OR TRIM(part_number) = ''
        """
    )
    conn.execute(
        """
        UPDATE tolerances
        SET part_number = COALESCE(
            (SELECT documents.part_number FROM documents WHERE documents.document_id = tolerances.document_id),
            tolerances.document_id
        )
        WHERE part_number IS NULL OR TRIM(part_number) = ''
        """
    )
    conn.execute(
        """
        UPDATE feature_control_frames
        SET part_number = COALESCE(
            (SELECT documents.part_number FROM documents WHERE documents.document_id = feature_control_frames.document_id),
            feature_control_frames.document_id
        )
        WHERE part_number IS NULL OR TRIM(part_number) = ''
        """
    )


def rebuild_table_without_coordinate_columns(conn, table_name, create_sql, columns_to_copy):
    temp_table = "{}__new".format(table_name)
    conn.execute("DROP TABLE IF EXISTS {}".format(temp_table))
    conn.execute(create_sql.format(table_name=temp_table))
    conn.execute(
        "INSERT INTO {temp} ({cols}) SELECT {cols} FROM {orig}".format(
            temp=temp_table,
            orig=table_name,
            cols=", ".join(columns_to_copy),
        )
    )
    conn.execute("DROP TABLE {}".format(table_name))
    conn.execute("ALTER TABLE {} RENAME TO {}".format(temp_table, table_name))


def remove_coordinate_columns(conn):
    table_specs = {
        "dimensions": {
            "columns_to_drop": {"x1", "y1", "x2", "y2", "dimension_pk", "document_number"},
            "columns_to_copy": [
                "document_id",
                "part_number",
                "view_id",
                "dimension_id",
                "raw_text",
                "dimension_type",
                "nominal_value",
                "tolerance_class",
                "tolerance_format",
                "tolerance_pattern",
                "tolerance_plus",
                "tolerance_minus",
                "status",
                "is_unassigned",
                "human_review_status",
                "human_reviewed_at",
                "human_review_notes",
            ],
            "create_sql": """
                CREATE TABLE {table_name} (
                    document_id TEXT NOT NULL,
                    part_number TEXT NOT NULL,
                    view_id TEXT,
                    dimension_id TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    dimension_type TEXT NOT NULL,
                    nominal_value REAL,
                    tolerance_class TEXT NOT NULL,
                    tolerance_format TEXT NOT NULL,
                    tolerance_pattern TEXT NOT NULL,
                    tolerance_plus REAL,
                    tolerance_minus REAL,
                    status TEXT NOT NULL,
                    is_unassigned INTEGER NOT NULL DEFAULT 0,
                    human_review_status TEXT NOT NULL DEFAULT 'pending',
                    human_reviewed_at TEXT,
                    human_review_notes TEXT,
                    UNIQUE(document_id, dimension_id),
                    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                )
            """,
        },
        "tolerances": {
            "columns_to_drop": {"x1", "y1", "x2", "y2", "document_number"},
            "columns_to_copy": [
                "tolerance_pk",
                "document_id",
                "part_number",
                "view_id",
                "dimension_id",
                "raw_text",
                "nominal_value",
                "tolerance_class",
                "tolerance_format",
                "tolerance_pattern",
                "tolerance_plus",
                "tolerance_minus",
                "status",
                "is_unassigned",
            ],
            "create_sql": """
                CREATE TABLE {table_name} (
                    tolerance_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    part_number TEXT NOT NULL,
                    view_id TEXT,
                    dimension_id TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    nominal_value REAL,
                    tolerance_class TEXT NOT NULL,
                    tolerance_format TEXT NOT NULL,
                    tolerance_pattern TEXT NOT NULL,
                    tolerance_plus REAL,
                    tolerance_minus REAL,
                    status TEXT NOT NULL,
                    is_unassigned INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(document_id, dimension_id),
                    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                )
            """,
        },
        "feature_control_frames": {
            "columns_to_drop": {"x1", "y1", "x2", "y2", "document_number"},
            "columns_to_copy": [
                "fcf_pk",
                "document_id",
                "part_number",
                "view_id",
                "fcf_id",
                "raw_text",
                "status",
                "is_unassigned",
            ],
            "create_sql": """
                CREATE TABLE {table_name} (
                    fcf_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    part_number TEXT NOT NULL,
                    view_id TEXT,
                    fcf_id TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_unassigned INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(document_id, fcf_id),
                    FOREIGN KEY(document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                )
            """,
        },
    }

    for table_name, spec in table_specs.items():
        columns = {row[1] for row in conn.execute("PRAGMA table_info({})".format(table_name))}
        if spec["columns_to_drop"] & columns:
            rebuild_table_without_coordinate_columns(
                conn,
                table_name,
                spec["create_sql"],
                spec["columns_to_copy"],
            )


def make_unique_id(conn, table_name, id_column, document_id, base_id):
    candidate = base_id or "item"
    suffix = 2
    while True:
        row = conn.execute(
            "SELECT 1 FROM {} WHERE document_id = ? AND {} = ? LIMIT 1".format(table_name, id_column),
            (document_id, candidate),
        ).fetchone()
        if row is None:
            return candidate
        candidate = "{}_{}".format(base_id or "item", suffix)
        suffix += 1


def upsert_document(conn, document_id, part_number, document_number, source_json_path, payload):
    conn.execute(
        """
        INSERT INTO documents (
            document_id, part_number, document_number, drawing_role, drawing_name, drawing_number, source_file_name, source_json_path,
            unit, unit_inferred, part_description, summary_text, title_block_json,
            bill_of_materials_json, notes_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            part_number = excluded.part_number,
            document_number = excluded.document_number,
            drawing_role = excluded.drawing_role,
            drawing_name = excluded.drawing_name,
            drawing_number = excluded.drawing_number,
            source_file_name = excluded.source_file_name,
            source_json_path = excluded.source_json_path,
            unit = excluded.unit,
            unit_inferred = excluded.unit_inferred,
            part_description = excluded.part_description,
            summary_text = excluded.summary_text,
            title_block_json = excluded.title_block_json,
            bill_of_materials_json = excluded.bill_of_materials_json,
            notes_json = excluded.notes_json
        """,
        (
            document_id,
            part_number,
            document_number,
            payload.get("drawing_role", "part"),
            payload.get("drawing_name") or document_id,
            payload.get("drawing_number"),
            payload.get("source_file_name"),
            source_json_path,
            payload.get("unit"),
            1 if payload.get("unit_inferred") else 0,
            payload.get("part_description"),
            payload.get("summary_text"),
            json.dumps(payload.get("title_block", {}), ensure_ascii=False),
            json.dumps(payload.get("bill_of_materials", {}), ensure_ascii=False),
            json.dumps(payload.get("notes", []), ensure_ascii=False),
        ),
    )


def clear_document_rows(conn, document_id):
    conn.execute("DELETE FROM tolerances WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM feature_control_frames WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM dimensions WHERE document_id = ?", (document_id,))


def rehydrate_manual_dimensions(conn, document_id):
    rows = conn.execute(
        """
        SELECT
            document_id,
            part_number,
            view_id,
            dimension_id,
            raw_text,
            dimension_type,
            nominal_value,
            tolerance_class,
            tolerance_format,
            tolerance_pattern,
            tolerance_plus,
            tolerance_minus,
            status,
            is_unassigned,
            human_review_status,
            human_reviewed_at,
            human_review_notes
        FROM manual_dimensions
        WHERE document_id = ?
        ORDER BY
            CASE
                WHEN LOWER(dimension_id) GLOB 'd[0-9]*' THEN CAST(SUBSTR(LOWER(dimension_id), 2) AS INTEGER)
                ELSE 999999
            END,
            dimension_id
        """,
        (document_id,),
    ).fetchall()

    for row in rows:
        conn.execute(
            """
            INSERT INTO dimensions (
                document_id, part_number, view_id, dimension_id, raw_text, dimension_type,
                nominal_value, tolerance_class, tolerance_format, tolerance_pattern,
                tolerance_plus, tolerance_minus, status, is_unassigned,
                human_review_status, human_reviewed_at, human_review_notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

        manual_dimension = {
            "raw_text": row[4],
            "nominal_value": row[6],
            "tolerance_class": row[7],
            "tolerance_format": row[8],
            "tolerance_pattern": row[9],
            "tolerance_plus": row[10],
            "tolerance_minus": row[11],
            "status": row[12],
        }
        if has_tolerance_data(manual_dimension):
            conn.execute(
                """
                INSERT INTO tolerances (
                    document_id, part_number, view_id, dimension_id, raw_text, nominal_value,
                    tolerance_class, tolerance_format, tolerance_pattern,
                    tolerance_plus, tolerance_minus, status, is_unassigned
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                ),
            )


def insert_dimension(conn, document_id, part_number, view_id, dimension, is_unassigned, dimension_id):
    conn.execute(
        """
        INSERT INTO dimensions (
            document_id, part_number, view_id, dimension_id, raw_text, dimension_type,
            nominal_value, tolerance_class, tolerance_format, tolerance_pattern,
            tolerance_plus, tolerance_minus, status, is_unassigned,
            human_review_status, human_reviewed_at, human_review_notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            part_number,
            view_id,
            dimension_id,
            dimension.get("raw_text"),
            dimension.get("dimension_type"),
            dimension.get("nominal_value"),
            dimension.get("tolerance_class", "none"),
            dimension.get("tolerance_format", "none"),
            dimension.get("tolerance_pattern", "none"),
            dimension.get("tolerance_plus"),
            dimension.get("tolerance_minus"),
            dimension.get("status"),
            1 if is_unassigned else 0,
            "pending",
            None,
            None,
        ),
    )
    return dimension_id


def has_tolerance_data(dimension):
    if dimension.get("tolerance_class", "none") not in ("none", "", None):
        return True
    if dimension.get("tolerance_format", "none") not in ("none", "", None):
        return True
    if dimension.get("tolerance_pattern", "none") not in ("none", "", None):
        return True
    return dimension.get("tolerance_plus") is not None or dimension.get("tolerance_minus") is not None


def _catia_dmu_results_table_name(tolerance_mode):
    normalized_mode = str(tolerance_mode or "").strip().lower()
    if normalized_mode == "nominal":
        return "catia_dmu_results_nominal"
    if normalized_mode == "max":
        return "catia_dmu_results_max"
    if normalized_mode == "min":
        return "catia_dmu_results_min"
    raise ValueError("Unsupported CATIA DMU tolerance mode: {}".format(tolerance_mode))


def _catia_dmu_runs_table_name(tolerance_mode):
    normalized_mode = str(tolerance_mode or "").strip().lower()
    if normalized_mode == "nominal":
        return "catia_dmu_runs_nominal"
    if normalized_mode == "max":
        return "catia_dmu_runs_max"
    if normalized_mode == "min":
        return "catia_dmu_runs_min"
    raise ValueError("Unsupported CATIA DMU tolerance mode: {}".format(tolerance_mode))


def import_catia_dmu_results(conn, tolerance_mode, document_id, drawing_part_number, dmu_inputs, results_json_path, created_at_utc, run_metadata=None):
    table_name = _catia_dmu_results_table_name(tolerance_mode)
    runs_table_name = _catia_dmu_runs_table_name(tolerance_mode)
    nominal_link_table_name = "catia_dmu_results_nominal_link"
    json_path = Path(results_json_path).resolve()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    run_root = json_path.parent
    run_id = str(payload.get("run_id") or run_root.name)
    results_csv_path = run_root / "results.csv"
    results_txt_path = run_root / "results.txt"
    run_metadata = run_metadata or {}
    type_counts = {}
    for row in results:
        normalized_type = str(row.get("Type") or "unknown").strip().lower()
        type_counts[normalized_type] = int(type_counts.get(normalized_type, 0)) + 1

    conn.execute("DELETE FROM {} WHERE run_id = ?".format(runs_table_name), (run_id,))
    if table_name == "catia_dmu_results_nominal":
        conn.execute(
            """
            DELETE FROM catia_dmu_results_nominal
            WHERE rowid IN (
                SELECT nominal_rowid
                FROM catia_dmu_results_nominal_link
                WHERE run_id = ?
            )
            """,
            (run_id,),
        )
        conn.execute("DELETE FROM {} WHERE run_id = ?".format(nominal_link_table_name), (run_id,))
    else:
        conn.execute("DELETE FROM {} WHERE run_id = ?".format(table_name), (run_id,))
    conn.execute(
        """
        INSERT INTO {table_name} (
            run_id,
            document_id,
            drawing_part_number,
            analysis_mode,
            clearance_mm,
            part_number,
            part_number_a,
            part_number_b,
            actuated_dimension_id,
            actuated_dimension_raw_text,
            actuated_catia_part_number,
            actuated_publication_name,
            actuated_parameter_name,
            actuated_parameter_path,
            target_value_mm,
            results_json_path,
            results_csv_path,
            results_txt_path,
            result_row_count,
            clash_count,
            clearance_count,
            contact_count,
            ui_opened,
            created_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """.format(table_name=runs_table_name),
        (
            run_id,
            document_id,
            drawing_part_number,
            payload.get("analysis_mode"),
            payload.get("clearance_mm"),
            (dmu_inputs or {}).get("part_number"),
            (dmu_inputs or {}).get("part_number_a"),
            (dmu_inputs or {}).get("part_number_b"),
            run_metadata.get("dimension_id"),
            run_metadata.get("dimension_raw_text"),
            run_metadata.get("catia_part_number"),
            run_metadata.get("publication_name"),
            run_metadata.get("parameter_name"),
            run_metadata.get("parameter_path"),
            run_metadata.get("target_value_mm"),
            str(json_path),
            str(results_csv_path.resolve()) if results_csv_path.exists() else None,
            str(results_txt_path.resolve()) if results_txt_path.exists() else None,
            len(results),
            int(type_counts.get("clash", 0)),
            int(type_counts.get("clearance", 0)),
            int(type_counts.get("contact", 0)),
            1 if run_metadata.get("ui_opened") else 0,
            created_at_utc,
        ),
    )

    for row in results:
        if table_name == "catia_dmu_results_nominal":
            insert_result = conn.execute(
                """
                INSERT INTO catia_dmu_results_nominal (
                    row_number,
                    product1,
                    product2,
                    conflict_type,
                    value_mm,
                    status,
                    info,
                    keep_text,
                    comment_text,
                    location_text,
                    image_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("No"),
                    row.get("Product1"),
                    row.get("Product2"),
                    row.get("Type"),
                    row.get("Value"),
                    row.get("Status"),
                    row.get("Info"),
                    row.get("Keep"),
                    row.get("Comment"),
                    row.get("Location"),
                    row.get("Image"),
                ),
            )
            conn.execute(
                """
                INSERT INTO catia_dmu_results_nominal_link (
                    run_id,
                    document_id,
                    row_number,
                    nominal_rowid,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    document_id,
                    row.get("No"),
                    insert_result.lastrowid,
                    created_at_utc,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO {table_name} (
                    run_id,
                    document_id,
                    drawing_part_number,
                    analysis_mode,
                    clearance_mm,
                    part_number,
                    part_number_a,
                    part_number_b,
                    actuated_dimension_id,
                    actuated_dimension_raw_text,
                    actuated_catia_part_number,
                    actuated_publication_name,
                    actuated_parameter_name,
                    actuated_parameter_path,
                    target_value_mm,
                    results_json_path,
                    results_csv_path,
                    results_txt_path,
                    row_number,
                    product1,
                    product2,
                    conflict_type,
                    value_mm,
                    status,
                    info,
                    keep_text,
                    comment_text,
                    location_text,
                    image_path,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.format(table_name=table_name),
                (
                    run_id,
                    document_id,
                    drawing_part_number,
                    payload.get("analysis_mode"),
                    payload.get("clearance_mm"),
                    (dmu_inputs or {}).get("part_number"),
                    (dmu_inputs or {}).get("part_number_a"),
                    (dmu_inputs or {}).get("part_number_b"),
                    run_metadata.get("dimension_id"),
                    run_metadata.get("dimension_raw_text"),
                    run_metadata.get("catia_part_number"),
                    run_metadata.get("publication_name"),
                    run_metadata.get("parameter_name"),
                    run_metadata.get("parameter_path"),
                    run_metadata.get("target_value_mm"),
                    str(json_path),
                    str(results_csv_path.resolve()) if results_csv_path.exists() else None,
                    str(results_txt_path.resolve()) if results_txt_path.exists() else None,
                    row.get("No"),
                    row.get("Product1"),
                    row.get("Product2"),
                    row.get("Type"),
                    row.get("Value"),
                    row.get("Status"),
                    row.get("Info"),
                    row.get("Keep"),
                    row.get("Comment"),
                    row.get("Location"),
                    row.get("Image"),
                    created_at_utc,
                ),
            )

    return {
        "table_name": table_name,
        "runs_table_name": runs_table_name,
        "run_id": run_id,
        "stored_row_count": len(results),
        "type_counts": type_counts,
        "results_json_path": str(json_path),
        "results_csv_path": str(results_csv_path.resolve()) if results_csv_path.exists() else None,
        "results_txt_path": str(results_txt_path.resolve()) if results_txt_path.exists() else None,
    }


def import_catia_dmu_results_to_sqlite(
    db_path,
    tolerance_mode,
    document_id,
    drawing_part_number,
    dmu_inputs,
    results_json_path,
    created_at_utc,
    run_metadata=None,
):
    conn = connect_db(db_path)
    try:
        create_tables(conn)
        migrate_tables(conn)
        summary = import_catia_dmu_results(
            conn=conn,
            tolerance_mode=tolerance_mode,
            document_id=document_id,
            drawing_part_number=drawing_part_number,
            dmu_inputs=dmu_inputs,
            results_json_path=results_json_path,
            created_at_utc=created_at_utc,
            run_metadata=run_metadata,
        )
        conn.commit()
        return summary
    finally:
        conn.close()


def save_paired_dmu_results_to_sqlite(
    db_path,
    document_id,
    pair_index,
    dimension_id,
    dimension_text,
    max_run_id,
    max_result_row,
    min_run_id,
    min_result_row,
    created_at_utc,
):
    conn = connect_db(db_path)
    try:
        create_tables(conn)
        migrate_tables(conn)
        save_paired_dmu_results(
            conn=conn,
            document_id=document_id,
            pair_index=pair_index,
            dimension_id=dimension_id,
            dimension_text=dimension_text,
            max_run_id=max_run_id,
            max_result_row=max_result_row,
            min_run_id=min_run_id,
            min_result_row=min_result_row,
            created_at_utc=created_at_utc,
        )
        conn.commit()
    finally:
        conn.close()


def clear_paired_dmu_results_for_document_to_sqlite(db_path, document_id):
    conn = connect_db(db_path)
    try:
        create_tables(conn)
        migrate_tables(conn)
        clear_paired_dmu_results_for_document(conn, document_id)
        conn.commit()
    finally:
        conn.close()


def insert_tolerance(conn, document_id, part_number, document_number, view_id, dimension_id, dimension, is_unassigned):
    conn.execute(
        """
        INSERT INTO tolerances (
            document_id, part_number, view_id, dimension_id, raw_text, nominal_value,
            tolerance_class, tolerance_format, tolerance_pattern,
            tolerance_plus, tolerance_minus, status, is_unassigned
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            part_number,
            view_id,
            dimension_id,
            dimension.get("raw_text"),
            dimension.get("nominal_value"),
            dimension.get("tolerance_class", "none"),
            dimension.get("tolerance_format", "none"),
            dimension.get("tolerance_pattern", "none"),
            dimension.get("tolerance_plus"),
            dimension.get("tolerance_minus"),
            dimension.get("status"),
            1 if is_unassigned else 0,
        ),
    )


def insert_fcf(conn, document_id, part_number, document_number, view_id, fcf, is_unassigned):
    fcf_id = make_unique_id(conn, "feature_control_frames", "fcf_id", document_id, fcf.get("fcf_id"))
    conn.execute(
        """
        INSERT INTO feature_control_frames (
            document_id, part_number, view_id, fcf_id, raw_text, status, is_unassigned
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            part_number,
            view_id,
            fcf_id,
            fcf.get("raw_text"),
            fcf.get("status"),
            1 if is_unassigned else 0,
        ),
    )


def import_extraction_json_to_sqlite(json_path, db_path):
    json_file = Path(json_path)
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    document_id = payload["document_id"]
    part_number = resolve_part_number(payload)
    document_number = resolve_document_number(payload)
    resolved_db_path = resolve_db_path(json_file, db_path=db_path)

    conn = connect_db(resolved_db_path)
    try:
        create_tables(conn)
        migrate_tables(conn)
        upsert_document(conn, document_id, part_number, document_number, str(json_file.resolve()), payload)
        clear_document_rows(conn, document_id)

        dimension_counter = 1
        for view in payload.get("views", []):
            view_id = view.get("view_id")

            for dimension in view.get("dimensions", []):
                dimension_id = "d{}".format(dimension_counter)
                dimension_counter += 1
                insert_dimension(conn, document_id, part_number, view_id, dimension, is_unassigned=False, dimension_id=dimension_id)
                if has_tolerance_data(dimension):
                    insert_tolerance(conn, document_id, part_number, document_number, view_id, dimension_id, dimension, is_unassigned=False)

            for fcf in view.get("feature_control_frames", []):
                insert_fcf(conn, document_id, part_number, document_number, view_id, fcf, is_unassigned=False)

        for dimension in payload.get("unassigned", {}).get("dimensions", []):
            dimension_id = "d{}".format(dimension_counter)
            dimension_counter += 1
            insert_dimension(conn, document_id, part_number, None, dimension, is_unassigned=True, dimension_id=dimension_id)
            if has_tolerance_data(dimension):
                insert_tolerance(conn, document_id, part_number, document_number, None, dimension_id, dimension, is_unassigned=True)

        for fcf in payload.get("unassigned", {}).get("feature_control_frames", []):
            insert_fcf(conn, document_id, part_number, document_number, None, fcf, is_unassigned=True)

        rehydrate_manual_dimensions(conn, document_id)

        conn.commit()
    finally:
        conn.close()


def main():
    args = parse_args()
    resolved_db_path = resolve_db_path(args.json, db_path=args.db)
    import_extraction_json_to_sqlite(args.json, str(resolved_db_path))
    print("Imported JSON into database: {}".format(resolved_db_path.resolve()))


if __name__ == "__main__":
    main()
