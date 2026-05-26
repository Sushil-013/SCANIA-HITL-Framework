import sqlite3
import re
import sys
import pythoncom
import win32com.client
import os

# Always resolve DB to the project root (or next to the .exe when frozen).
if getattr(sys, 'frozen', False):
    DB_PATH = os.path.join(os.path.dirname(sys.executable), 'eats_validation.db')
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eats_validation.db')

# =============================================================
# DATABASE SETUP
# =============================================================
#
#  SCHEMA (mirrors CATIA hierarchy exactly)
#
#  catia_assemblies  (PARENT - one row per open CATProduct)
#  ├── assembly_id   PK
#  ├── assembly_name       e.g. "2679796"
#  ├── file_path
#  ├── extracted_at
#  └── total_params
#
#  catia_parameters  (CHILD - one row per published parameter)
#  ├── id            PK
#  ├── assembly_id   FK → catia_assemblies
#  ├── part_instance       which sub-product/part owns the param
#  ├── part_path           full dot-path from root  e.g. "2679796.Bracket_1.Part1"
#  ├── publication_name    the published name the user set
#  ├── internal_param_name the real parameter name inside the part
#  ├── relation_formula    driving formula / expression if any
#  ├── unit                mm / deg / etc.
#  └── current_value
#
# =============================================================
def _derive_assembly_tag(assembly_name):
    """Turn an assembly name into a clean tag, e.g. '3289049 4' → '3289049_4'."""
    tag = re.sub(r'[^A-Za-z0-9_\-]', '_', assembly_name)
    tag = re.sub(r'_+', '_', tag).strip('_')
    return tag


def setup_catia_tables(cursor):
    # Drop old tables if they exist with a stale schema, then recreate cleanly
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS catia_assemblies (
            assembly_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            assembly_name  TEXT NOT NULL,
            project_tag    TEXT DEFAULT '',
            file_path      TEXT,
            extracted_at   TEXT DEFAULT (datetime('now','localtime')),
            total_params   INTEGER DEFAULT 0
        )
    ''')
    # Migration: project_tag on catia_assemblies for pre-existing DBs
    try:
        cursor.execute("ALTER TABLE catia_assemblies ADD COLUMN project_tag TEXT DEFAULT ''")
    except Exception:
        pass

    # Check whether catia_parameters has the expected columns; if not, drop and recreate
    cursor.execute("PRAGMA table_info(catia_parameters)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    required_cols = {'assembly_id', 'part_instance', 'part_path',
                     'publication_name', 'internal_param_name',
                     'relation_formula', 'unit', 'current_value'}
    if existing_cols and not required_cols.issubset(existing_cols):
        print("⚠️  Old catia_parameters schema detected — dropping and recreating...")
        cursor.execute('DROP TABLE IF EXISTS catia_parameters')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS catia_parameters (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            assembly_id          INTEGER NOT NULL,
            project_tag          TEXT DEFAULT '',
            part_instance        TEXT,
            part_path            TEXT,
            publication_name     TEXT NOT NULL,
            internal_param_name  TEXT,
            relation_formula     TEXT,
            unit                 TEXT,
            current_value        REAL,
            FOREIGN KEY (assembly_id) REFERENCES catia_assemblies(assembly_id)
                ON DELETE CASCADE,
            UNIQUE (assembly_id, publication_name, part_path)
        )
    ''')
    # Migration: project_tag on catia_parameters for pre-existing DBs
    try:
        cursor.execute("ALTER TABLE catia_parameters ADD COLUMN project_tag TEXT DEFAULT ''")
    except Exception:
        pass


# =============================================================
# HELPER: Read a parameter's value, unit, formula safely
# =============================================================
def _read_param(param):
    """Return (internal_name, current_val, unit, formula) from a CATIA Parameter COM object."""
    try:
        internal_name = str(param.Name)
    except Exception:
        internal_name = "N/A"

    # Value
    current_val = None
    for attr in ('Value', 'ValueAsString'):
        try:
            current_val = float(getattr(param, attr))
            break
        except Exception:
            pass

    # Unit
    unit = "mm"
    try:
        unit = str(param.Unit.Symbol)
    except Exception:
        pass

    # Formula / relation  — CATIA V5 exposes this via param.Relations or param.Formula
    relation_formula = None
    for attempt in (
        lambda: str(param.Formula.Body),
        lambda: str(param.Relations.Item(1).Body),
    ):
        try:
            relation_formula = attempt()
            break
        except Exception:
            pass

    return internal_name, current_val, unit, relation_formula


# =============================================================
# HELPER: Find a parameter by publication name inside a product node
#
# CATIA V5 COM does NOT reliably expose Publication.ValuatedElement
# via Python's late-binding IDispatch.
# The robust alternative:
#   product.ReferenceProduct.Parent  →  CATPart document
#   part_doc.Part.Parameters         →  full parameter list
#   match by name against the publication name
# =============================================================
def _find_param_for_publication(product, pub_name):
    """
    Try several COM paths to locate the CATIA Parameter object that a
    published name points to. Returns the param object or None.
    """
    attempts = []

    # Path 1: product → ReferenceProduct → parent CATPart doc → Parameters
    try:
        ref = product.ReferenceProduct
        part_doc = ref.Parent
        params = part_doc.Part.Parameters

        # 1a. Direct lookup by exact name
        try:
            return params.GetItem(pub_name)
        except Exception:
            pass

        # 1b. Scan all parameters — pub name may be a substring of internal name
        for k in range(1, params.Count + 1):
            try:
                p = params.Item(k)
                p_name = str(p.Name)
                if p_name == pub_name or p_name.endswith('\\' + pub_name) \
                        or p_name.endswith('/' + pub_name):
                    return p
            except Exception:
                pass
        attempts.append("ReferenceProduct path scanned, no match")

    except Exception as e:
        attempts.append(f"ReferenceProduct path failed: {e}")

    # Path 2: product → Publications.Item.ValuatedElement (original, may work on some CATIA builds)
    try:
        pubs = product.Publications
        for k in range(1, pubs.Count + 1):
            try:
                pub = pubs.Item(k)
                if str(pub.Name) == pub_name:
                    ve = pub.ValuatedElement   # works on some CATIA builds
                    _ = ve.Name                # confirm it's a parameter, not geometry
                    return ve
            except Exception:
                pass
        attempts.append("ValuatedElement scan found no match")
    except Exception as e:
        attempts.append(f"ValuatedElement path failed: {e}")

    return None


# =============================================================
# RECURSIVE TREE WALKER
# =============================================================
def walk_product_tree(product, assembly_id, assembly_tag, cursor, breadcrumb="", depth=0):
    """
    Recursively visits every Product node.
    - Checks Publications at every level (not just root).
    - Records the full dot-path from the root assembly.
    """
    indent = "  " * depth
    node_name = product.Name
    current_path = f"{breadcrumb}.{node_name}" if breadcrumb else node_name

    extracted = 0

    # --- Check Publications on this product node ---
    try:
        pubs = product.Publications
        pub_count = pubs.Count
    except Exception:
        pub_count = 0

    if pub_count > 0:
        print(f"{indent}📦 [{node_name}] → {pub_count} publication(s)")

        for i in range(1, pub_count + 1):
            try:
                pub      = pubs.Item(i)
                pub_name = pub.Name

                param = _find_param_for_publication(product, pub_name)

                if param is None:
                    print(f"{indent}  ⚠️ '{pub_name}' — could not locate underlying parameter "
                          f"(may be published geometry, not a dimension parameter)")
                    continue

                internal_name, current_val, unit, relation_formula = _read_param(param)

                cursor.execute('''
                    INSERT INTO catia_parameters
                        (assembly_id, project_tag, part_instance, part_path, publication_name,
                         internal_param_name, relation_formula, unit, current_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(assembly_id, publication_name, part_path) DO UPDATE SET
                        project_tag         = excluded.project_tag,
                        current_value       = excluded.current_value,
                        internal_param_name = excluded.internal_param_name,
                        relation_formula    = excluded.relation_formula,
                        unit                = excluded.unit
                ''', (
                    assembly_id, assembly_tag, node_name, current_path, pub_name,
                    internal_name, relation_formula, unit, current_val
                ))

                val_str = f"{current_val} {unit}" if current_val is not None else "N/A"
                print(f"{indent}  ✅ {pub_name}")
                print(f"{indent}     Part instance : {node_name}")
                print(f"{indent}     Path          : {current_path}")
                print(f"{indent}     Internal name : {internal_name}")
                print(f"{indent}     Formula       : {relation_formula or '—'}")
                print(f"{indent}     Value         : {val_str}\n")
                extracted += 1

            except Exception as e:
                print(f"{indent}  ⚠️ Skipping publication #{i} on '{node_name}': {e}")

    # --- Recurse into sub-products ---
    try:
        sub_count = product.Products.Count
    except Exception:
        sub_count = 0

    for j in range(1, sub_count + 1):
        try:
            child = product.Products.Item(j)
            extracted += walk_product_tree(child, assembly_id, assembly_tag, cursor,
                                           breadcrumb=current_path, depth=depth + 1)
        except Exception as e:
            print(f"{indent}  ⚠️ Cannot traverse child #{j}: {e}")

    return extracted


def create_assembly_view(conn, assembly_tag):
    """
    Create (or replace) a SQLite VIEW named vw_asm_<assembly_tag> that shows only
    the catia_parameters rows for this assembly.  Mirrors the Option A view created
    by A1 for semantic_tolerances — gives a clean per-project slice in any DB browser.
    """
    safe_tag  = assembly_tag.replace('-', '_')
    view_name = f"vw_asm_{safe_tag}"
    cursor = conn.cursor()
    try:
        cursor.execute(f'DROP VIEW IF EXISTS "{view_name}"')
        cursor.execute(f'''
            CREATE VIEW "{view_name}" AS
            SELECT  cp.id,
                    cp.assembly_id,
                    cp.project_tag,
                    ca.assembly_name,
                    cp.part_instance,
                    cp.part_path,
                    cp.publication_name,
                    cp.internal_param_name,
                    cp.relation_formula,
                    cp.unit,
                    cp.current_value
            FROM    catia_parameters cp
            JOIN    catia_assemblies ca ON ca.assembly_id = cp.assembly_id
            WHERE   cp.project_tag = \'{assembly_tag}\'
        ''')
        conn.commit()
        print(f"  \U0001f4ca SQLite view created: {view_name}")
    except Exception as e:
        print(f"  \u26a0  Could not create view '{view_name}': {e}")


# =============================================================
# MAIN ENTRY POINT
# =============================================================
def run_agent_2_structural_fetch():
    print("\n" + "=" * 60)
    print("=== AGENT 2: CATIA PUBLISHED PARAMETER EXTRACTION ===")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()
    setup_catia_tables(cursor)
    conn.commit()

    # --- Connect to CATIA ---
    try:
        pythoncom.CoInitialize()   # Required when called from a non-COM-initialised thread (e.g. PyInstaller windowed EXE)
        catia = win32com.client.Dispatch("CATIA.Application")
        product_doc = catia.ActiveDocument
        main_product = product_doc.Product
        assembly_name = main_product.Name
        try:
            file_path = product_doc.FullName
        except Exception:
            file_path = "Unknown"
    except Exception as e:
        print(f"❌ Cannot connect to CATIA. Make sure a CATProduct is open.\n   {e}")
        conn.close()
        return

    print(f"✅ CATIA connected")
    print(f"   Assembly : {assembly_name}")
    print(f"   File     : {file_path}\n")

    assembly_tag = _derive_assembly_tag(assembly_name)

    # --- Register this assembly as a parent row ---
    cursor.execute(
        'INSERT INTO catia_assemblies (assembly_name, project_tag, file_path) VALUES (?, ?, ?)',
        (assembly_name, assembly_tag, file_path)
    )
    conn.commit()
    assembly_id = cursor.lastrowid
    print(f"\U0001f4cb Assembly registered \u2192 assembly_id={assembly_id}  "
          f"project_tag='{assembly_tag}'\n")

    # --- Walk the full product tree recursively ---
    print("\U0001f50d Scanning full product tree for published parameters...\n")
    total = walk_product_tree(main_product, assembly_id, assembly_tag, cursor, breadcrumb="", depth=0)
    conn.commit()

    # --- Update parent row with final count ---
    cursor.execute(
        'UPDATE catia_assemblies SET total_params = ? WHERE assembly_id = ?',
        (total, assembly_id)
    )
    conn.commit()

    # --- Create per-assembly view (Option A) ---
    create_assembly_view(conn, assembly_tag)

    # --- Print DB summary ---
    print("\n" + "=" * 60)
    print("--- DATABASE STATE (catia_assemblies + catia_parameters) ---")

    cursor.execute('SELECT * FROM catia_assemblies ORDER BY assembly_id')
    for asm in cursor.fetchall():
        # asm cols: assembly_id[0], assembly_name[1], project_tag[2], file_path[3],
        #           extracted_at[4], total_params[5]
        tag_str = asm[2] if asm[2] else asm[1]
        print(f"\n  Assembly [{asm[0]}]: {asm[1]}  "
              f"tag='{tag_str}'  |  {asm[5]} param(s)  |  {asm[4]}")
        print(f"  File: {asm[3]}")

        cursor.execute(
            'SELECT id, part_instance, part_path, publication_name, '
            'internal_param_name, relation_formula, unit, current_value '
            'FROM catia_parameters WHERE assembly_id = ? ORDER BY part_path, id',
            (asm[0],)
        )
        dims = cursor.fetchall()
        if dims:
            print(f"\n  {'ID':<5} {'Part Instance':<20} {'Publication':<25} "
                  f"{'Internal Name':<25} {'Formula':<20} {'Value'}")
            print("  " + "-" * 110)
            for r in dims:
                val_str = f"{r[7]} {r[6]}" if r[7] is not None else "N/A"
                formula_str = (r[5][:18] + "..") if r[5] and len(r[5]) > 20 else (r[5] or "—")
                print(f"  {r[0]:<5} {r[1]:<20} {r[3]:<25} {r[4]:<25} {formula_str:<20} {val_str}")
            print(f"\n  └─ Path breakdown:")
            seen_paths = set()
            for r in dims:
                if r[2] not in seen_paths:
                    print(f"     {r[2]}")
                    seen_paths.add(r[2])
        else:
            print("  (no parameters found for this assembly)")

    conn.close()
    print("\n" + "=" * 60)
    if total == 0:
        print("⚠️  0 publications extracted.")
        print("   Possible reasons:")
        print("   1. Parameters are not Published - go to Tools > Publication in CATIA and publish them")
        print("   2. The active document is a Part (.CATPart), not an Assembly (.CATProduct)")
        print("   3. Publications exist but are not linked to Parameters (linked to geometry instead)")
    else:
        print(f"✅ Done — {total} published parameter(s) stored under assembly_id={assembly_id}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_agent_2_structural_fetch()