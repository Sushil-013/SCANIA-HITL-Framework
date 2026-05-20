import openai
import base64
import glob
import io
import json
import os
import re
import sqlite3
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from dotenv import load_dotenv
from PIL import Image

sys.stdout.reconfigure(encoding='utf-8')

# Always point at Approach_1_SQLite_Tkinter/eats_validation.db regardless of CWD
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eats_validation.db')

# Injected by master_app.py before import so Toplevel windows use the
# existing Tk root instead of spawning a second one.
_MASTER_ROOT = None

# Load the keys from your hidden .env file
load_dotenv()
client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ===========================================================================
# ARCHITECTURE -- Tiled-Vision Linear Pipeline
#
#   preprocess_document()          PDF -> PIL image (300 DPI) or direct load
#   generate_image_tiles()         1 image -> 4 overlapping quadrant tiles
#
#   Agent 1A  (VISION x4)          Each tile scanned once -- raw text output
#   Agent 1B  (TEXT ONLY)          Raw text -> structured JSON
#   Agent 1C  (TEXT ONLY)          QA audit: compares raw text vs JSON, adds gaps
#
#   Python filter                  Drop any row where upper_tol=0 AND lower_tol=0
#                                  AND no gdt_symbol
#   HITL Gatekeeper                Engineer approves / modifies / rejects
#   save_to_relational_db()        Writes to eats_validation.db
#
# ===========================================================================


# ---------------------------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------------------------
def setup_database():
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('PRAGMA foreign_keys = ON')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drawings (
            drawing_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name        TEXT NOT NULL,
            file_path        TEXT NOT NULL,
            project_tag      TEXT DEFAULT '',
            processed_at     TEXT DEFAULT (datetime('now','localtime')),
            total_dimensions INTEGER DEFAULT 0,
            status           TEXT DEFAULT 'PROCESSING'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS semantic_tolerances (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            drawing_id          INTEGER NOT NULL,
            project_tag         TEXT DEFAULT '',
            feature_name        TEXT NOT NULL,
            nominal_value       REAL,
            upper_tolerance     REAL,
            lower_tolerance     REAL,
            verification_status TEXT,
            confidence_score    REAL,
            raw_text_seen       TEXT,
            datum_refs          TEXT DEFAULT '',
            gdt_symbol          TEXT DEFAULT '',
            applies_to_surface  TEXT DEFAULT '',
            FOREIGN KEY (drawing_id) REFERENCES drawings(drawing_id)
                ON DELETE CASCADE,
            UNIQUE (drawing_id, feature_name)
        )
    ''')

    for col, default in [
        ('project_tag',         "''"),
        ('datum_refs',          "''"),
        ('gdt_symbol',          "''"),
        ('applies_to_surface',  "''"),
        ('verification_status', "'Pending'"),
    ]:
        try:
            cursor.execute(
                f"ALTER TABLE semantic_tolerances ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    try:
        cursor.execute("ALTER TABLE drawings ADD COLUMN project_tag TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()
    return conn


def _derive_project_tag(file_name):
    stem = os.path.splitext(file_name)[0]
    tag  = re.sub(r'[^A-Za-z0-9_\-]', '_', stem)
    tag  = re.sub(r'_+', '_', tag).strip('_')
    return tag


def register_drawing(db_conn, file_path):
    file_name   = os.path.basename(file_path)
    project_tag = _derive_project_tag(file_name)
    cursor = db_conn.cursor()
    cursor.execute(
        'INSERT INTO drawings (file_name, file_path, project_tag, status) VALUES (?, ?, ?, ?)',
        (file_name, file_path, project_tag, 'PROCESSING')
    )
    db_conn.commit()
    drawing_id = cursor.lastrowid
    print(f"[DWG] Drawing registered -> drawing_id={drawing_id}  "
          f"project_tag='{project_tag}'  file='{file_name}'")
    return drawing_id, project_tag


def create_project_views(db_conn, project_tag):
    safe_tag  = project_tag.replace('-', '_')
    view_name = f"vw_{safe_tag}"
    cursor    = db_conn.cursor()
    try:
        cursor.execute(f'DROP VIEW IF EXISTS "{view_name}"')
        cursor.execute(f'''
            CREATE VIEW "{view_name}" AS
            SELECT  st.id, st.drawing_id, st.project_tag, d.file_name,
                    st.feature_name, st.nominal_value, st.upper_tolerance,
                    st.lower_tolerance, st.verification_status, st.datum_refs,
                    st.gdt_symbol, st.applies_to_surface,
                    st.confidence_score, st.raw_text_seen
            FROM    semantic_tolerances st
            JOIN    drawings d ON d.drawing_id = st.drawing_id
            WHERE   st.project_tag = \'{project_tag}\'
        ''')
        db_conn.commit()
        print(f"  [VIEW] SQLite view created: {view_name}")
    except Exception as e:
        print(f"  [!] Could not create view '{view_name}': {e}")


def finalise_drawing(db_conn, drawing_id, project_tag):
    cursor = db_conn.cursor()
    cursor.execute(
        'SELECT COUNT(*) FROM semantic_tolerances WHERE drawing_id = ?', (drawing_id,))
    count = cursor.fetchone()[0]
    cursor.execute(
        'UPDATE drawings SET total_dimensions = ?, status = ? WHERE drawing_id = ?',
        (count, 'COMPLETED', drawing_id)
    )
    db_conn.commit()
    create_project_views(db_conn, project_tag)


# ---------------------------------------------------------------------------
# STEP 0 -- DOCUMENT PRE-PROCESSOR
# ---------------------------------------------------------------------------
def preprocess_document(file_path):
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.pdf':
        try:
            import fitz
            doc  = fitz.open(file_path)
            page = doc[0]
            mat  = fitz.Matrix(300 / 72, 300 / 72)
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc.close()
            print(f"  [preprocess] PDF rendered via PyMuPDF at 300 DPI ({img.width}x{img.height} px)")
            return img
        except ImportError:
            pass

        try:
            from pdf2image import convert_from_path
            pages = convert_from_path(file_path, dpi=300)
            img   = pages[0].convert('RGB')
            print(f"  [preprocess] PDF rendered via pdf2image at 300 DPI ({img.width}x{img.height} px)")
            return img
        except ImportError:
            raise RuntimeError(
                "PDF support requires PyMuPDF or pdf2image.\n"
                "Install with:  pip install pymupdf   or   pip install pdf2image"
            )

    img = Image.open(file_path).convert('RGB')
    print(f"  [preprocess] Image loaded ({img.width}x{img.height} px)")
    return img


# ---------------------------------------------------------------------------
# STEP 1 -- IMAGE TILING  (4 overlapping quadrants)
# ---------------------------------------------------------------------------
def generate_image_tiles(pil_image, overlap=0.18):
    W, H  = pil_image.size
    mx    = W // 2
    my    = H // 2
    ox    = int(W * overlap / 2)
    oy    = int(H * overlap / 2)

    quads = [
        ('Top-Left',     (0,       0,       mx + ox, my + oy)),
        ('Top-Right',    (mx - ox, 0,       W,       my + oy)),
        ('Bottom-Left',  (0,       my - oy, mx + ox, H)),
        ('Bottom-Right', (mx - ox, my - oy, W,       H)),
    ]

    tiles = []
    for label, box in quads:
        cropped = pil_image.crop(box)
        buf     = io.BytesIO()
        cropped.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        w, h = cropped.size
        print(f"  [tile] {label:<14} {w}x{h} px")
        tiles.append({'label': label, 'b64': b64})

    return tiles


# ---------------------------------------------------------------------------
# AGENT 1A -- TILED VISION DETECTOR  (image seen here and ONLY here)
# ---------------------------------------------------------------------------
def agent_1a_detect(tiles):
    system_prompt = (
        "You are scanning one quadrant of an engineering drawing. "
        "Your PRIMARY job is to extract EVERY dimension -- do not skip any.\n\n"

        "DIAMETER SYMBOL -- MANDATORY CHARACTER RULE:\n"
        "  The ONLY permitted character for a diameter symbol is \u2300 (\u2300).\n"
        "  \u2314 is a geometry shape and is FORBIDDEN -- if you catch yourself about to write \u2314,"
        " write \u2300 instead.\n"
        "  When uncertain whether a circle-with-line symbol is a diameter: write \u2300.\n\n"

        "FCF / DATUM GROUPING -- STRICT SPATIAL RULE:\n"
        "  Attach an FCF or Datum to a dimension ONLY when a visible leader line or callout "
        "arrow DIRECTLY connects that FCF box to THAT specific dimension annotation.\n"
        "  An Nx GROUP (e.g. 2X \u230018, 4X 19,05) almost NEVER owns its own FCF.\n"
        "  If an FCF box sits nearby an Nx group AND a separate single feature of the same "
        "nominal value, the FCF belongs to the SINGLE feature, NOT the Nx group.\n"
        "  EXAMPLE -- correct output when \u230018 +-0,3 appears three ways:\n"
        "    [DIM] value: \u230018 +0,3/-0,3 | [FCF] symbol: position, tol: \u23000,2 M, datums: F  <- single feature, HAS leader to FCF\n"
        "    [DIM] value: 2X \u230018 +0,3/-0,3                                                       <- Nx group, NO FCF\n"
        "    [DIM] value: 2X \u230018 +0,3/-0,3                                                       <- second Nx group, NO FCF\n"
        "  When in doubt, output the FCF as a separate standalone [FCF] line rather than "
        "attaching it to the wrong dimension.\n\n"

        "FCF OUTPUT FORMAT -- ALWAYS COMPLETE:\n"
        "  When you attach an FCF to a dimension, ALWAYS include ALL three parts:\n"
        "    [FCF] symbol: <name>, tol: <value>, datums: <letters>\n"
        "  NEVER output a partial FCF like '[FCF] datum: C' or '[FCF] datums: E' alone.\n"
        "  If you can see the FCF box but the tolerance value is unclear, write tol: ?.\n"
        "  If you see a datum callout letter with no FCF box, output it as a separate "
        "[DATUM] <letter> line instead.\n\n"

        "Nx GROUPS -- COMPLETENESS RULE:\n"
        "  Report EVERY Nx-multiplied annotation you can see in this tile.\n"
        "  Do NOT skip a 2X or 4X group just because you already reported one with the same nominal.\n\n"

        "OTHER RULES:\n"
        "  - Output one semantic block per line.\n"
        "  - Reproduce values EXACTLY as printed (commas, signs, symbols).\n"
        "  - Scan all the way to the very edges of the tile -- do not stop early.\n"
        "  - Report partially visible values at tile edges (mark confidence low)."
    )

    combined_parts = []
    for tile in tiles:
        label = tile['label']
        print(f"  -> Agent 1A scanning tile: {label} ...")
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text",
                         "text": (f"This is the {label} quadrant of an engineering drawing.\n"
                                  "Extract EVERY dimension you can see. For each one, check if a "
                                  "Feature Control Frame (FCF) or Datum letter is physically next to "
                                  "or below it -- if so, combine onto one line. "
                                  "Standalone dimensions (no FCF) must still be reported. "
                                  "Scan all the way to the bottom edges of this tile.\n"
                                  "IMPORTANT -- Nx GROUPS: Report EVERY occurrence of an Nx-multiplied "
                                  "group (e.g. 4X, 2X) that you can see in this tile, even if you have "
                                  "already reported a group with the same nominal value. Different Nx "
                                  "groups at different locations are DIFFERENT features. Also note whether "
                                  "each Nx group has its own FCF/datum or not -- do not share a single "
                                  "FCF across multiple groups unless the leader line explicitly connects it to all of them.")},
                        {"type": "image_url",
                         "image_url": {
                             "url":    f"data:image/png;base64,{tile['b64']}",
                             "detail": "high",
                         }}
                    ]
                }
            ],
            max_tokens=2000,
        )
        tile_text = response.choices[0].message.content.strip()
        combined_parts.append(f"=== TILE: {label} ===\n{tile_text}")
        print(f"     {label}: {len(tile_text.splitlines())} line(s) extracted")

    combined = "\n\n".join(combined_parts)
    print(f"\n  Agent 1A total raw lines: {sum(len(p.splitlines()) for p in combined_parts)}")
    return combined


# ---------------------------------------------------------------------------
# AGENT 1B -- JSON EXTRACTOR  (text only -- no image)
# ---------------------------------------------------------------------------
def agent_1b_extract(combined_raw_text):
    print("-> Agent 1B (Extractor) structuring raw text into JSON ...")

    system_prompt = """You are a precision JSON extractor for engineering drawing data.

You receive SEMANTIC BLOCK lines from 4 quadrant tiles of a single drawing.
Lines follow this format (FCF and QTY parts are optional):
  [DIM] value: <...> | [FCF] symbol: <...>, tol: <...>, datums: <...> | [QTY] <N>X

RULE 1A - SINGLE FEATURES = MERGE (overlap artefact)  *** CRITICAL ***
If a feature has NO multiplier prefix (e.g. dia25, dia10.5, 500, 290.4, 50, 12.2 x 15)
and you see it appear multiple times across different tiles with the SAME nominal value,
SAME tolerances and SAME datum letter(s) -- it is an overlap artefact.
YOU MUST MERGE THEM into a single JSON row. Keep the MOST COMPLETE FCF data.

RULE 1A PARTIAL-FCF EXTENSION -- MERGE DESPITE INCOMPLETE FCF IN ONE TILE  *** CRITICAL ***
Agent 1A (the vision model) sometimes outputs a partial FCF for a feature in one tile
(e.g. only "[FCF] datum: C" with no symbol or tol) but a complete FCF in another tile
("[FCF] symbol: position, tol: 0.2, datums: C") for the SAME physical feature.
These are the SAME feature seen twice in the tile overlap -- MERGE THEM.
When merging, USE the most complete version (the one with symbol + tol + datums).
  EXAMPLE: "dia10.5 +0.27/0 | [FCF] datum: C"  (Top-Left tile, partial)
       and "dia10.5 +0.27/0 | [FCF] symbol: position, tol: 0.2, datums: C"  (Bottom-Left tile)
  -> ONE row: Diameter_10.5_C, nominal=10.5, upper_tol=0.27, lower_tol=0,
              datum_refs='C', gdt_symbol='position'
  WRONG: Diameter_10.5_1 (no gdt_symbol) + Diameter_10.5_3 (with position) for the same datum C hole.
  RIGHT: one merged row using the complete FCF data.

RULE 1B - Nx MULTIPLIERS = KEEP SEPARATE (distinct physical groups)  *** CRITICAL ***
If a feature DOES have a multiplier (e.g. 4X 19.05 +-0.5 or 2X dia18 +-0.3) and
you see that Nx-group repeated in the raw text -- whether in different tiles OR in the
SAME tile -- these are PHYSICALLY DISTINCT hole/feature groups on the part.
DO NOT MERGE THEM under any circumstances. Keep EVERY occurrence as its own JSON row.
Rule 1A (merge overlap artefacts) does NOT apply to any line that carries an Nx prefix.
An Nx-prefixed line is ALWAYS a distinct feature group regardless of how many tiles it appears in.

  EXAMPLE A -- identical 2X groups in two adjacent tiles:
    "2X dia18 +-0.3"  (Top-Left tile, no FCF)   -> Diameter_18.0_1, count=2
    "2X dia18 +-0.3"  (Top-Right tile, no FCF)  -> Diameter_18.0_2, count=2
  These are two groups on opposite sides of the part that are both visible in the overlap zone.
  WRONG: decide they are the same group and merge into one row with count=2.
  RIGHT: two separate rows, each count=2.

  EXAMPLE B -- three 4X groups across tiles:
    "4X 19.05 +-0.5" (Top-Right, first)   -> Length_19.05_1, count=4
    "4X 19.05 +-0.5" (Top-Right, second)  -> Length_19.05_2, count=4
    "4X 19.05 +-0.5" (Bottom-Right)       -> Length_19.05_3, count=4
  RIGHT: three separate rows.

RULE 1B EXTENSION -- MIXED MULTIPLIERS (same nominal, different Nx)  *** CRITICAL ***
If the SAME nominal value appears with DIFFERENT multipliers or different FCF/datum
contexts, these are always completely different physical features.
  EXAMPLE: dia18 appearing three ways on one drawing:
    1X dia18 +-0.3 (with FCF datum F)         -> Diameter_18.0_1, count=1, datum=F, gdt=position
    2X dia18 +-0.3 (Top-Left, no FCF)         -> Diameter_18.0_2, count=2
    2X dia18 +-0.3 (Top-Right, no FCF)        -> Diameter_18.0_3, count=2
  WRONG: collapse to one or two rows.  RIGHT: three rows.

RULE 1B EXTENSION 2 -- STRIP INCORRECTLY ATTACHED FCF FROM Nx GROUPS  *** CRITICAL ***
Agent 1A (the vision model) sometimes makes errors attaching FCFs to Nx-groups when the
FCF physically belongs to a nearby single feature. Apply this correction in extraction:
  - If you see an Nx-group line like "2X dia18 +-0.3 | [FCF] datums: F" in the raw text,
    AND the raw text also shows a standalone "dia18" or "1X dia18" anywhere else,
    REMOVE the FCF from the Nx-group row and confirm the FCF belongs to the single feature.
  - An Nx group SHOULD have an FCF only when NO standalone version of the same nominal exists.
  - When stripping: set datum_refs='' and gdt_symbol='' for the Nx-group row.

DECISION CHECKLIST -- apply in order:
  1. Does the raw text show this nominal+tolerance with DIFFERENT multipliers (e.g. 1X vs 2X)?
     YES -> EACH multiplier variant is a separate row. (Rule 1B Extension)
  2. Does the dimension have ANY Nx prefix (even the same N)?   YES -> KEEP SEPARATE (Rule 1B)
  3. Does it appear with different datums or FCF in different tiles? YES -> KEEP SEPARATE
  4. All conditions fail (standalone value, same tols, same/no datums)? -> MERGE (Rule 1A)

RULE 2 - SLOT / COMBINED DIMENSIONS  *** CRITICAL ***
If a single text line contains TWO toleranced values joined by "X" or "x"
(e.g. "12,2 +0,2/0 X 15 +0,5/0"), this is a SLOT DIMENSION.
Split it and create a SEPARATE JSON row for EACH value:
  Row 1: feature_name: Slot_Width_12.2,  nominal: 12.2, upper_tol: 0.2, lower_tol: 0.0
  Row 2: feature_name: Slot_Length_15.0, nominal: 15.0, upper_tol: 0.5, lower_tol: 0.0
NEVER collapse both numbers into one row. NEVER drop the second value.

RULE 3 - EUROPEAN DECIMAL FORMAT  *** CRITICAL ***
This drawing uses European decimal COMMAS, not points.
  "+0,2" -> 0.2    "+-0,05" -> +-0.05    "12,2" -> 12.2
Convert every comma to a decimal point before writing float values.
NEVER round +0,2 to 0.0. A comma is a decimal separator here, not a thousands separator.

GROUPING RULES (FCF pairing)
  - Each [DIM] line -> ONE json entry.
    The [FCF] data on the SAME line -> 'gdt_symbol' and 'datum_refs' of THAT entry.
    Do NOT create a separate entry for a FCF already paired with a [DIM].
  - A standalone [FCF] line (no paired [DIM]) -> its own entry (nominal=0, tols=0).
  - [QTY] Nx on a line -> set count=N on that entry.
  - FCF 'datums' -> 'datum_refs'.   FCF 'symbol' -> 'gdt_symbol'.

FEATURE NAMING RULES
  - Diameter symbol present in raw text -> name it Diameter_[nominal].
  - No diameter symbol -> name it Length_[nominal] or Dimension_[nominal] or Slot_Width/Slot_Length.
  - Do NOT call a linear length a Diameter.
  - Same nominal appears more than once -> append _1, _2, _3 to keep names unique.

GENERAL EXTRACTION RULES
  - Extract EVERYTHING - do not filter anything out.
  - Include GD&T-only entries (upper_tol=0, lower_tol=0) when gdt_symbol is set.
  - lower_tol <= 0.  upper_tol >= 0.
  - confidence: 1.0=clearly readable, 0.7=partially cut off at edge, 0.5=uncertain.

OUTPUT: valid JSON only - no markdown fences, no extra keys outside the schema."""

    prompt = f"""RAW EXTRACTION TEXT (from 4 quadrant tiles of one engineering drawing):
{combined_raw_text}

Apply ALL rules strictly:
  - Rule 1A: MERGE single features (no Nx prefix) that appear in multiple tiles -- overlap artefacts.
  - Rule 1B: KEEP SEPARATE every Nx-prefixed group (e.g. 4X, 2X) seen anywhere in the raw text --
    even if the same nominal appears as 1X+FCF in one place and 2X without FCF elsewhere, those are
    completely different physical features. Each occurrence = its own row.
  - Rule 2: Split every "A x B" slot/combined dimension into TWO separate rows.
  - Rule 3: Convert all European commas to decimal points (+0,2 -> 0.2, NEVER 0.0).
  - Merge each [FCF] into its paired [DIM] row; standalone [FCF] lines get their own row.

Return JSON:
{{
  "dimensions": [
    {{
      "feature_name":       "Diameter_X / Length_X / Slot_Width_X / Slot_Length_X (unique)",
      "nominal":            "<float, commas converted to decimal points>",
      "upper_tol":          "<float >= 0, commas converted>",
      "lower_tol":          "<float <= 0, commas converted>",
      "count":              "<int, default 1>",
      "datum_refs":         "datum letter(s) or empty string",
      "gdt_symbol":         "symbol name or empty string",
      "applies_to_surface": "surface description or empty string",
      "confidence":         "<0.0 to 1.0>",
      "raw_text_seen":      "verbatim text copied from raw input",
      "status":             "EXTRACTED"
    }}
  ],
  "extraction_notes": "brief summary of what was found, any splits or duplicate-instance decisions"
}}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ]
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# AGENT 1C -- QA AUDITOR  (text only -- no image)
# ---------------------------------------------------------------------------
def agent_1c_audit(combined_raw_text, extracted_dims):
    print("-> Agent 1C (QA Auditor) cross-checking raw text vs JSON ...")

    n1b = len(extracted_dims)

    system_prompt = f"""You are a strict QA Auditor for engineering drawing data extraction.

You receive:
  (A) Raw text extracted from drawing tiles — GROUND TRUTH.
  (B) A structured JSON list of {n1b} dimension entries from the extractor.

CRITICAL RULES — READ BEFORE DOING ANYTHING ELSE:
  *** YOUR OUTPUT LIST MUST CONTAIN AT LEAST {n1b} ENTRIES ***
  *** NEVER REMOVE, MERGE, DEDUPLICATE, OR DROP ANY ENTRY FROM (B) ***
  *** NEVER SHORTEN THE LIST — ONLY ADD OR CORRECT ***
  *** IF YOU SEE A DIMENSION IN (A) NOT IN (B) → ADD IT ***
  *** EVERY ENTRY WITH A NON-ZERO TOLERANCE MUST BE PRESERVED ***

AUDIT STEPS:
  1. Output EVERY entry from (B) exactly as-is (you may fix errors but NEVER delete).
  2. Scan raw text (A) for any dimension not already covered in (B).
     Match by nominal value + tolerance. Minor naming differences are OK.
  3. For each gap found → append a new entry:
       status='AUDITOR_ADDED', audit_note explains what was found.
  4. If a (B) entry has a wrong tolerance sign or value → correct it:
       status='AUDITOR_CORRECTED', preserve the original in audit_note.
  5. Do NOT filter zero-tolerance items — the Python layer handles that.
  6. Final count must be >= {n1b}. If your list is shorter, you dropped something — fix it."""

    prompt = f"""AGENT 1A RAW EXTRACTION TEXT (ground truth):
{combined_raw_text}

AGENT 1B STRUCTURED JSON ({n1b} entries — you must keep ALL of them):
{json.dumps(extracted_dims, indent=2)}

Return the complete updated list. It MUST contain all {n1b} original entries
plus any additions. NEVER drop or merge any entry.

Return JSON:
{{
  "dimensions": [ "...complete list of ALL entries..." ],
  "audit_summary": "what was added/corrected, or 'No gaps found'",
  "final_notes": "any important observations for the engineer"
}}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ]
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# DETERMINISTIC PYTHON FILTER
# ---------------------------------------------------------------------------
def filter_toleranced_only(dims):
    filtered = [
        d for d in dims
        if (
            abs(d.get('upper_tol') or 0.0) > 0.001
            or abs(d.get('lower_tol') or 0.0) > 0.001
            or str(d.get('gdt_symbol') or '').strip().lower()
               not in ('', 'datum', 'reference', 'ref')
        )
        # Exclude pure datum-reference entries — they carry no manufacturable tolerance
        and not str(d.get('feature_name') or '').startswith('Datum_')
        and str(d.get('gdt_symbol') or '').strip().lower() != 'datum'
    ]
    dropped = len(dims) - len(filtered)
    if dropped:
        print(f"  [Python filter] Dropped {dropped} row(s) with no tolerance and no GD&T symbol, "
              f"{len(filtered)} remain.")
    return filtered


# ---------------------------------------------------------------------------
# HITL LOGICAL GATEKEEPER  —  Graphical Tkinter UI
# ---------------------------------------------------------------------------
# Designed to run from a background thread (as launched by master_app.py).
# Uses threading.Event so the worker thread blocks until the engineer closes
# the window.  The actual Tk widgets are created/destroyed on the main thread
# via widget.after() to stay thread-safe.
# ---------------------------------------------------------------------------

_TREE_COLS = (
    ("#",           35,  "center"),
    ("Feature Name", 200, "w"),
    ("Nominal",      80,  "center"),
    ("+Tol",         70,  "center"),
    ("-Tol",         70,  "center"),
    ("Count",        50,  "center"),
    ("Datums",       90,  "center"),
    ("GD&T",         100, "center"),
    ("Status",       120, "w"),
)


def logical_layer_hitl_review(final_json):
    """
    Graphical HITL gatekeeper.  Blocks the calling thread until the engineer
    makes a decision.  Returns the (possibly modified) dims list or None.
    """
    dims = list(final_json.get('final_dimensions', []))

    if not dims:
        print("\n[HITL] No toleranced dimensions to review — skipping gatekeeper.")
        return None

    print("\n[HITL] Opening graphical review window…")

    result   = {"value": None}   # shared across threads
    done_evt = threading.Event()

    # ------------------------------------------------------------------
    # All widget code runs on the main (Tk) thread via .after()
    # ------------------------------------------------------------------
    def _build_ui():
        # ── Root / Toplevel ─────────────────────────────────────────────
        parent = _MASTER_ROOT
        if parent is not None:
            win = tk.Toplevel(parent)
        else:
            win = tk.Tk()

        win.title("HITL Review — Perception Layer Gatekeeper")
        win.configure(bg="#161b22")
        win.resizable(True, True)

        # Centre on screen
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = min(1100, sw - 60), min(720, sh - 60)
        win.geometry(f"{ww}x{wh}+{(sw - ww)//2}+{(sh - wh)//2}")
        win.grab_set()   # modal

        # ── Colour palette ───────────────────────────────────────────────
        BG       = "#161b22"
        CARD     = "#21262d"
        HDR      = "#0d1117"
        BORDER   = "#30363d"
        ACCENT   = "#1f6feb"
        RED      = "#b91c1c"
        GREEN    = "#166534"
        ORANGE   = "#92400e"
        FG       = "#e2e8f0"
        FG_MUTED = "#8b949e"

        # ── Header bar ────────────────────────────────────────────────────
        hdr_frame = tk.Frame(win, bg=HDR, height=52)
        hdr_frame.pack(fill="x")
        hdr_frame.pack_propagate(False)
        tk.Label(
            hdr_frame,
            text="  🔍  HITL GATEKEEPER  —  Perception Layer Review",
            font=("Segoe UI", 14, "bold"),
            bg=HDR, fg="#58a6ff", anchor="w"
        ).pack(side="left", fill="y", padx=10)
        notes_txt = final_json.get('final_notes', '')
        if notes_txt:
            tk.Label(
                hdr_frame,
                text=f"Notes: {notes_txt[:120]}",
                font=("Segoe UI", 9),
                bg=HDR, fg=FG_MUTED, anchor="e"
            ).pack(side="right", padx=14, fill="y")

        # ── Summary strip ─────────────────────────────────────────────────
        stats_frame = tk.Frame(win, bg=CARD, height=32)
        stats_frame.pack(fill="x")
        stats_frame.pack_propagate(False)
        stats_lbl = tk.Label(
            stats_frame, text="",
            font=("Consolas", 10),
            bg=CARD, fg=FG_MUTED, anchor="w"
        )
        stats_lbl.pack(side="left", padx=14, fill="y")

        def _refresh_stats():
            total_inst = sum(int(d.get('count', 1)) for d in dims)
            stats_lbl.config(
                text=f"  Unique features: {len(dims)}   │   DB rows after expansion: {total_inst}"
            )

        # ── Treeview ──────────────────────────────────────────────────────
        tree_frame = tk.Frame(win, bg=BORDER, bd=1, relief="flat")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "HITL.Treeview",
            background="#0d1117", foreground=FG,
            fieldbackground="#0d1117",
            rowheight=24,
            font=("Consolas", 10),
        )
        style.configure(
            "HITL.Treeview.Heading",
            background=CARD, foreground="#58a6ff",
            font=("Segoe UI", 10, "bold"),
            relief="flat"
        )
        style.map("HITL.Treeview", background=[("selected", ACCENT)])

        col_ids = [c[0] for c in _TREE_COLS]
        tree = ttk.Treeview(
            tree_frame, columns=col_ids, show="headings",
            selectmode="browse", style="HITL.Treeview"
        )
        for cid, width, anchor in _TREE_COLS:
            tree.heading(cid, text=cid)
            tree.column(cid, width=width, minwidth=40, anchor=anchor, stretch=(cid == "Feature Name"))

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        def _populate():
            for item in tree.get_children():
                tree.delete(item)
            for i, d in enumerate(dims, 1):
                tree.insert("", "end", iid=str(i), values=(
                    i,
                    d.get('feature_name', ''),
                    d.get('nominal', ''),
                    f"+{d.get('upper_tol', '')}",
                    str(d.get('lower_tol', '')),
                    str(int(d.get('count', 1))),
                    d.get('datum_refs', '') or '--',
                    d.get('gdt_symbol', '') or '--',
                    d.get('status', ''),
                ))
            _refresh_stats()

        _populate()

        # ── Button bar frames (primary / modify) ─────────────────────────
        btn_area = tk.Frame(win, bg=BG)
        btn_area.pack(fill="x", padx=10, pady=10)

        primary_bar = tk.Frame(btn_area, bg=BG)
        modify_bar  = tk.Frame(btn_area, bg=BG)

        def _show_primary():
            modify_bar.pack_forget()
            primary_bar.pack(fill="x")

        def _show_modify():
            primary_bar.pack_forget()
            modify_bar.pack(fill="x")

        # ── Primary buttons ────────────────────────────────────────────────
        _BTN = dict(font=("Segoe UI", 12, "bold"), relief="flat",
                    cursor="hand2", padx=20, pady=10)

        def _accept():
            result["value"] = list(dims)
            print(f"[HITL] Engineer APPROVED {len(dims)} dimension(s). Saving to DB…")
            win.grab_release()
            win.destroy()
            done_evt.set()

        def _reject():
            result["value"] = None
            print("[HITL] Engineer REJECTED — database save aborted.")
            win.grab_release()
            win.destroy()
            done_evt.set()

        tk.Button(
            primary_bar, text="✔  Accept All  →  Save to DB",
            bg="#166534", fg="white", activebackground="#15803d",
            command=_accept, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        tk.Button(
            primary_bar, text="⚠  Modify",
            bg=ORANGE, fg="white", activebackground="#b45309",
            command=_show_modify, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=6)

        tk.Button(
            primary_bar, text="✖  Reject All",
            bg=RED, fg="white", activebackground="#dc2626",
            command=_reject, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        _show_primary()

        # ── Modify sub-menu buttons ────────────────────────────────────────
        def _add_row():
            popup = tk.Toplevel(win)
            popup.title("Add Row")
            popup.configure(bg=CARD)
            popup.resizable(True, False)
            popup.grab_set()
            pw, ph = 480, 400
            popup.geometry(f"{pw}x{ph}+{(sw-pw)//2}+{(sh-ph)//2}")
            popup.minsize(420, 380)

            tk.Label(popup, text="Add Dimension Row",
                     font=("Segoe UI", 12, "bold"),
                     bg=CARD, fg="#58a6ff").pack(pady=(14, 8))

            # field key must exactly match what _save_new reads
            fields   = [("Feature Name", ""), ("Nominal", ""), ("+ Tolerance", ""),
                        ("- Tolerance", ""), ("Count", "1"),
                        ("Datums", ""), ("GD&T Symbol", "")]
            entries  = {}
            frm = tk.Frame(popup, bg=CARD)
            frm.pack(padx=20, pady=4, fill="x")
            for lbl_txt, default in fields:
                row_f = tk.Frame(frm, bg=CARD)
                row_f.pack(fill="x", pady=3)
                tk.Label(row_f, text=f"{lbl_txt}:",
                         width=18, anchor="w",
                         font=("Segoe UI", 10), bg=CARD, fg=FG).pack(side="left")
                var = tk.StringVar(value=default)
                e   = tk.Entry(row_f, textvariable=var,
                               bg="#0d1117", fg=FG, insertbackground=FG,
                               relief="flat", bd=4, font=("Consolas", 10))
                e.pack(side="left", fill="x", expand=True)
                entries[lbl_txt] = var

            err_lbl = tk.Label(popup, text="", bg=CARD, fg="#f87171",
                               font=("Segoe UI", 9))
            err_lbl.pack(pady=(4, 0))

            def _save_new():
                try:
                    utol = float(entries["+ Tolerance"].get())
                    ltol = float(entries["- Tolerance"].get())
                    if abs(utol) <= 0.001 and abs(ltol) <= 0.001:
                        err_lbl.config(text="Both tolerances are 0 — row would be filtered out.")
                        return
                    nd = {
                        'feature_name': entries["Feature Name"].get().strip(),
                        'nominal':      float(entries["Nominal"].get()),
                        'upper_tol':    utol,
                        'lower_tol':    ltol,
                        'count':        max(1, int(entries["Count"].get())),
                        'datum_refs':   entries["Datums"].get().strip() or None,
                        'gdt_symbol':   entries["GD&T Symbol"].get().strip() or None,
                        'confidence':   1.0,
                        'raw_text_seen': 'MANUALLY ADDED',
                        'status':       'HUMAN_ADDED',
                    }
                    if not nd['feature_name']:
                        err_lbl.config(text="Feature Name is required.")
                        return
                    dims.append(nd)
                    _populate()
                    print(f"[HITL] Added row: {nd['feature_name']}")
                    popup.destroy()
                except ValueError as ve:
                    err_lbl.config(text=f"Invalid value: {ve}")

            tk.Button(popup, text="  ✔  Save Row  ",
                      bg=ACCENT, fg="white", font=("Segoe UI", 11, "bold"),
                      relief="flat", cursor="hand2", pady=10,
                      command=_save_new).pack(pady=8)

        def _delete_row():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning(
                    "No Selection",
                    "Click the row you want to delete in the table first.",
                    parent=win
                )
                return
            iid     = sel[0]
            row_num = int(iid) - 1
            if 0 <= row_num < len(dims):
                name = dims[row_num].get('feature_name', '?')
                if messagebox.askyesno(
                    "Confirm Delete",
                    f"Delete row {int(iid)}:  '{name}'?",
                    parent=win
                ):
                    dims.pop(row_num)
                    _populate()
                    print(f"[HITL] Deleted row: {name}")
            else:
                messagebox.showerror("Error", "Row index out of range.", parent=win)

        def _modify_row():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning(
                    "No Selection",
                    "Click the row you want to modify in the table first.",
                    parent=win
                )
                return
            iid     = sel[0]
            row_num = int(iid) - 1
            if not (0 <= row_num < len(dims)):
                messagebox.showerror("Error", "Row index out of range.", parent=win)
                return

            d = dims[row_num]   # reference to existing dict

            popup = tk.Toplevel(win)
            popup.title(f"Modify Row {int(iid)}")
            popup.configure(bg=CARD)
            popup.resizable(True, False)
            popup.grab_set()
            pw, ph = 480, 420
            popup.geometry(f"{pw}x{ph}+{(sw-pw)//2}+{(sh-ph)//2}")
            popup.minsize(420, 400)

            tk.Label(popup, text=f"Modify Dimension Row {int(iid)}",
                     font=("Segoe UI", 12, "bold"),
                     bg=CARD, fg="#58a6ff").pack(pady=(14, 8))

            # Pre-fill with current values
            fields = [
                ("Feature Name",  str(d.get('feature_name', ''))),
                ("Nominal",       str(d.get('nominal', ''))),
                ("+ Tolerance",   str(d.get('upper_tol', ''))),
                ("- Tolerance",   str(d.get('lower_tol', ''))),
                ("Count",         str(int(d.get('count', 1)))),
                ("Datums",        str(d.get('datum_refs', '') or '')),
                ("GD&T Symbol",   str(d.get('gdt_symbol', '') or '')),
            ]
            entries = {}
            frm = tk.Frame(popup, bg=CARD)
            frm.pack(padx=20, pady=4, fill="x")
            for lbl_txt, current_val in fields:
                row_f = tk.Frame(frm, bg=CARD)
                row_f.pack(fill="x", pady=3)
                tk.Label(row_f, text=f"{lbl_txt}:",
                         width=18, anchor="w",
                         font=("Segoe UI", 10), bg=CARD, fg=FG).pack(side="left")
                var = tk.StringVar(value=current_val)
                e   = tk.Entry(row_f, textvariable=var,
                               bg="#0d1117", fg=FG, insertbackground=FG,
                               relief="flat", bd=4, font=("Consolas", 10))
                e.pack(side="left", fill="x", expand=True)
                entries[lbl_txt] = var

            err_lbl = tk.Label(popup, text="", bg=CARD, fg="#f87171",
                               font=("Segoe UI", 9))
            err_lbl.pack(pady=(4, 0))

            def _save_edit():
                try:
                    utol = float(entries["+ Tolerance"].get())
                    ltol = float(entries["- Tolerance"].get())
                    if abs(utol) <= 0.001 and abs(ltol) <= 0.001:
                        err_lbl.config(text="Both tolerances are 0 — row would be filtered out.")
                        return
                    feat = entries["Feature Name"].get().strip()
                    if not feat:
                        err_lbl.config(text="Feature Name is required.")
                        return
                    # Update the dict in-place so the dims list reflects the change
                    d['feature_name'] = feat
                    d['nominal']      = float(entries["Nominal"].get())
                    d['upper_tol']    = utol
                    d['lower_tol']    = ltol
                    d['count']        = max(1, int(entries["Count"].get()))
                    d['datum_refs']   = entries["Datums"].get().strip() or None
                    d['gdt_symbol']   = entries["GD&T Symbol"].get().strip() or None
                    if d.get('status') not in ('HUMAN_ADDED',):
                        d['status']   = 'HUMAN_MODIFIED'
                    _populate()
                    print(f"[HITL] Modified row {int(iid)}: {feat}")
                    popup.destroy()
                except ValueError as ve:
                    err_lbl.config(text=f"Invalid value: {ve}")

            tk.Button(popup, text="  ✔  Update Row  ",
                      bg="#1558d6", fg="white", font=("Segoe UI", 11, "bold"),
                      relief="flat", cursor="hand2", pady=10,
                      command=_save_edit).pack(pady=8)

        tk.Button(
            modify_bar, text="＋  Add Row",
            bg=ACCENT, fg="white", activebackground="#388bfd",
            command=_add_row, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))

        tk.Button(
            modify_bar, text="✎  Modify Selected",
            bg="#0e6a9e", fg="white", activebackground="#1280c0",
            command=_modify_row, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=4)

        tk.Button(
            modify_bar, text="－  Delete Selected",
            bg="#7c2d12", fg="white", activebackground=RED,
            command=_delete_row, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=4)

        tk.Button(
            modify_bar, text="◀  Back",
            bg="#374151", fg="white", activebackground="#4b5563",
            command=_show_primary, **_BTN
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        # ── Footer status ─────────────────────────────────────────────────
        tk.Label(
            win,
            text="  Select a row before using Delete. Close window = Reject.",
            font=("Segoe UI", 9), bg=HDR, fg=FG_MUTED, anchor="w"
        ).pack(fill="x", side="bottom")

        # Closing the window without a decision = Reject
        def _on_close():
            result["value"] = None
            print("[HITL] Window closed — treating as Reject.")
            win.grab_release()
            win.destroy()
            done_evt.set()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        win.focus_force()

    # Schedule UI creation on main Tk thread, then block worker thread
    if _MASTER_ROOT is not None:
        _MASTER_ROOT.after(0, _build_ui)
    else:
        # Fallback: no master root — build directly (standalone mode)
        _build_ui()

    done_evt.wait()   # worker thread waits here until engineer decides
    return result["value"]


# ---------------------------------------------------------------------------
# DATABASE SAVE
# ---------------------------------------------------------------------------
def save_to_relational_db(approved_dims, db_conn, drawing_id, project_tag=''):
    print("-> Saving engineer-approved data to SQLite ...")
    cursor = db_conn.cursor()

    if not approved_dims:
        print("[!] No dimensions to save.")
        return 0

    expanded = []
    for item in approved_dims:
        count = max(int(item.get('count', 1)), 1)
        if count == 1:
            expanded.append(item)
        else:
            base = item['feature_name']
            print(f"  -> Expanding '{base}' x {count}")
            for n in range(1, count + 1):
                copy = dict(item)
                copy['feature_name'] = f"{base}_{n}of{count}"
                copy['raw_text_seen'] = (
                    f"{item.get('raw_text_seen', '')} [instance {n}/{count}]")
                expanded.append(copy)

    seen = {}
    for item in expanded:
        b = item['feature_name']
        if b in seen:
            seen[b] += 1
            item['feature_name'] = f"{b}_{seen[b]}"
        else:
            seen[b] = 1

    inserted = 0
    for item in expanded:
        conf = item.get('confidence', 1.0)
        if conf == 0:
            print(f"[!] Skipping '{item.get('feature_name')}' -- zero confidence")
            continue
        try:
            cursor.execute('''
                INSERT INTO semantic_tolerances
                    (drawing_id, project_tag, feature_name, nominal_value,
                     upper_tolerance, lower_tolerance, verification_status,
                     confidence_score, raw_text_seen, datum_refs,
                     gdt_symbol, applies_to_surface)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id, feature_name) DO UPDATE SET
                    project_tag          = excluded.project_tag,
                    nominal_value        = excluded.nominal_value,
                    upper_tolerance      = excluded.upper_tolerance,
                    lower_tolerance      = excluded.lower_tolerance,
                    verification_status  = CASE
                        WHEN verification_status IN
                             ('Verified','Rejected - Exceeds Max Limit')
                        THEN verification_status
                        ELSE 'Pending'
                    END,
                    confidence_score     = excluded.confidence_score,
                    raw_text_seen        = excluded.raw_text_seen,
                    datum_refs           = excluded.datum_refs,
                    gdt_symbol           = excluded.gdt_symbol,
                    applies_to_surface   = excluded.applies_to_surface
            ''', (
                drawing_id, project_tag,
                item['feature_name'], item['nominal'],
                item['upper_tol'],    item['lower_tol'],
                'Pending', conf,
                item.get('raw_text_seen', ''),
                item.get('datum_refs',    ''),
                item.get('gdt_symbol',    ''),
                item.get('applies_to_surface', ''),
            ))
            inserted += 1
        except Exception as e:
            print(f"[ERR] DB insert failed for '{item.get('feature_name')}': {e}")

    db_conn.commit()
    print(f"[OK] Locked {inserted} dimension(s) into DB (drawing_id={drawing_id}).")
    return inserted


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------
def run_perception_layer(file_path):
    print("\n" + "=" * 62)
    print("=== SCANIA CPS -- TILED VISION PERCEPTION PIPELINE ===")
    print("=== 1A(vision x4) -> 1B(text) -> 1C(text) -> Python filter -> HITL ===")
    print("=" * 62)

    # -- 0. DB init
    db_conn = setup_database()

    # -- 1. Register drawing
    drawing_id, project_tag = register_drawing(db_conn, file_path)

    # -- 2. Load & pre-process document
    print("\n" + "=" * 62)
    print("  DOCUMENT PRE-PROCESSING")
    print("=" * 62)
    pil_image = preprocess_document(file_path)

    # -- 3. Tile the image
    print("\n" + "=" * 62)
    print("  IMAGE TILING  (4 overlapping quadrants, ~18% overlap)")
    print("=" * 62)
    tiles = generate_image_tiles(pil_image)

    # -- 4. Agent 1A -- vision scan (4 tiles)
    print("\n" + "=" * 62)
    print("  AGENT 1A -- TILED VISION DETECTOR  (image accessed here only)")
    print("=" * 62)
    combined_raw_text = agent_1a_detect(tiles)

    print("\n--- Agent 1A Combined Raw Text (all 4 tiles) ---")
    preview = combined_raw_text[:3000]
    print(preview + (" ...[truncated]" if len(combined_raw_text) > 3000 else ""))

    # -- 5. Agent 1B -- text-only JSON extraction
    print("\n" + "=" * 62)
    print("  AGENT 1B -- TEXT-ONLY JSON EXTRACTION")
    print("=" * 62)
    extracted_json = agent_1b_extract(combined_raw_text)
    extracted_dims = extracted_json.get('dimensions', [])
    print(f"Agent 1B extracted: {len(extracted_dims)} item(s)")
    for d in extracted_dims:
        print(f"  [*] {d.get('feature_name')}: nominal={d.get('nominal')}  "
              f"+{d.get('upper_tol')}/{d.get('lower_tol')}  "
              f"(conf: {d.get('confidence', 'N/A')})")
    if extracted_json.get('extraction_notes'):
        print(f"Notes: {extracted_json['extraction_notes']}")

    if not extracted_dims:
        print("\n[!] Agent 1B found no dimensions.")
        db_conn.cursor().execute(
            "UPDATE drawings SET status='FAILED' WHERE drawing_id=?", (drawing_id,))
        db_conn.commit()
        db_conn.close()
        return {"final_dimensions": [], "reason": "No dimensions found"}

    # -- 6. Agent 1C -- text-only QA audit
    print("\n" + "=" * 62)
    print("  AGENT 1C -- TEXT-ONLY QA AUDITOR")
    print("=" * 62)
    audited_json = agent_1c_audit(combined_raw_text, extracted_dims)
    audited_dims = audited_json.get('dimensions', [])

    # ── Safety union merge: 1B items can NEVER be dropped by 1C ──────────────
    # Build a lookup of what 1C returned by feature_name.
    # Any 1B item not present in 1C's output is re-inserted (with its original
    # data) so that GPT truncation / deduplication never silently loses dims.
    c_names = {str(d.get('feature_name', '')).strip().lower() for d in audited_dims}
    rescued = []
    for d in extracted_dims:
        fname = str(d.get('feature_name', '')).strip().lower()
        if fname not in c_names:
            rescued.append(dict(d, status='RESCUED_FROM_1B',
                                audit_note='Agent 1C did not include this; re-inserted from 1B output.'))
            c_names.add(fname)
    if rescued:
        print(f"  [MERGE] Agent 1C dropped {len(rescued)} item(s) — re-inserted from 1B:")
        for r in rescued:
            print(f"    rescued: {r.get('feature_name')}")
        audited_dims = audited_dims + rescued

    added = [d for d in audited_dims if d.get('status') == 'AUDITOR_ADDED']
    fixed = [d for d in audited_dims if d.get('status') == 'AUDITOR_CORRECTED']
    print(f"Agent 1C: {len(audited_dims)} total -- {len(added)} added, {len(fixed)} corrected")
    for d in added:
        print(f"  [+] {d.get('feature_name')}: {d.get('raw_text_seen', '')}")
    for d in fixed:
        print(f"  [FIX] {d.get('feature_name')} corrected")
    if audited_json.get('audit_summary'):
        print(f"Audit summary: {audited_json['audit_summary']}")

    # -- 7. Deterministic Python filter
    print("\n" + "=" * 62)
    print("  PYTHON FILTER -- ZERO-TOLERANCE REMOVAL")
    print("=" * 62)
    final_dims = filter_toleranced_only(audited_dims)

    if not final_dims:
        print("\n[!] No toleranced dimensions remain after filtering.")
        db_conn.cursor().execute(
            "UPDATE drawings SET status='FAILED' WHERE drawing_id=?", (drawing_id,))
        db_conn.commit()
        db_conn.close()
        return {"final_dimensions": [], "reason": "All dimensions had zero tolerance"}

    final_json = {
        'final_dimensions': final_dims,
        'final_notes':      audited_json.get('final_notes', ''),
    }

    # -- 8. HITL Gatekeeper
    approved_dims = logical_layer_hitl_review(final_json)

    if approved_dims is None:
        db_conn.cursor().execute(
            "UPDATE drawings SET status='REJECTED_BY_ENGINEER' WHERE drawing_id=?",
            (drawing_id,))
        db_conn.commit()
        db_conn.close()
        print("\n[ABORT] Pipeline terminated by engineer.")
        return {"final_dimensions": [], "reason": "Rejected by engineer"}

    # -- 9. Save to DB
    save_to_relational_db(approved_dims, db_conn, drawing_id, project_tag)

    # -- 10. Finalise + create project VIEW
    finalise_drawing(db_conn, drawing_id, project_tag)

    # -- 11. DB summary
    print("\n" + "=" * 62)
    print("--- DATABASE STATE ---")
    cursor = db_conn.cursor()
    cursor.execute('SELECT * FROM drawings ORDER BY drawing_id')
    drows = cursor.fetchall()
    print(f"\n{'DWG_ID':<8} {'Project Tag':<24} {'File Name':<28} {'Dims':<6} {'Status'}")
    print("-" * 85)
    for d in drows:
        print(f"{d[0]:<8} {d[3]:<24} {d[1]:<28} {d[5]:<6} {d[6]}")

    for d in drows:
        cursor.execute(
            'SELECT id, feature_name, nominal_value, upper_tolerance, '
            'lower_tolerance, verification_status, confidence_score '
            'FROM semantic_tolerances WHERE drawing_id = ? ORDER BY id',
            (d[0],)
        )
        sdims = cursor.fetchall()
        if sdims:
            print(f"\n  +- [{d[3]}]  drawing_id={d[0]}")
            print(f"  |  {'ID':<5} {'Feature':<28} {'Nom':<10} "
                  f"{'Upper':<8} {'Lower':<8} {'Status':<16} Conf")
            print(f"  |  " + "-" * 85)
            for row in sdims:
                print(f"  |  {row[0]:<5} {row[1]:<28} {row[2]:<10} "
                      f"{row[3]:<8} {row[4]:<8} {row[5]:<16} {row[6]}")
            print(f"  +- {len(sdims)} dimension(s)")

    db_conn.close()
    print("\n" + "=" * 62 + "\n")
    final_json['final_dimensions'] = approved_dims
    return final_json


# ---------------------------------------------------------------------------
# ENTRY POINT -- folder scan + file picker (accepts images AND PDFs)
# ---------------------------------------------------------------------------
def _select_drawing():
    import tkinter as tk
    from tkinter import filedialog

    script_dir = os.path.dirname(os.path.abspath(__file__))

    exts = ('*.png', '*.jpg', '*.jpeg', '*.bmp', '*.tif', '*.tiff', '*.pdf')
    found = sorted(set(
        p for ext in exts for p in glob.glob(os.path.join(script_dir, ext))
    ))

    already_done = set()
    try:
        _db  = sqlite3.connect(DB_PATH)
        _cur = _db.cursor()
        _cur.execute("SELECT file_path FROM drawings WHERE status='COMPLETED'")
        already_done = {row[0] for row in _cur.fetchall()}
        _db.close()
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("  SELECT DRAWING TO PROCESS")
    print("=" * 60)
    if found:
        print(f"  Folder: {script_dir}\n")
        for i, path in enumerate(found, 1):
            marker = "  [OK] already processed" if path in already_done else ""
            print(f"  [{i}]  {os.path.basename(path)}{marker}")
    else:
        print("  (no image or PDF files found in LAYER1 folder)")

    print("\n  [B]  Browse for a file elsewhere...")
    print("  [Q]  Quit")
    print("-" * 60)

    while True:
        choice = input("  Your choice: ").strip().upper()

        if choice == 'Q':
            return None

        if choice == 'B':
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askopenfilename(
                title="Select engineering drawing",
                filetypes=[
                    ("Drawings", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.pdf"),
                    ("PDF",      "*.pdf"),
                    ("Images",   "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                    ("All",      "*.*"),
                ]
            )
            root.destroy()
            if path:
                return path
            print("  No file selected -- try again.")
            continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(found):
                return found[idx]
            print(f"  Invalid -- enter 1-{len(found)}, B, or Q.")
            continue

        print("  Invalid input -- enter a number, B, or Q.")


if __name__ == "__main__":
    file_path = _select_drawing()
    if file_path:
        run_perception_layer(file_path)
    else:
        print("\nNo drawing selected. Exiting.\n")