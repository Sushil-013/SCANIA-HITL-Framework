"""
generate_report.py
──────────────────
Reads the actuation_results table from eats_validation.db and produces a
self-contained HTML report that engineers can open in any browser.

Usage
─────
  # Standalone (generates report for the most recent run):
  python generate_report.py

  # Called from A4_The Actuator.py after saving the DB:
  from generate_report import generate_html_report
  path = generate_html_report()   # returns path to output .html file
"""

import base64
import datetime
import os
import sqlite3
import sys

# PyInstaller EXE: save DB and reports next to the .exe, not in the temp extraction dir.
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH     = os.path.join(_BASE_DIR, "eats_validation.db")
REPORTS_DIR = os.path.join(_BASE_DIR, "reports")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _img_to_b64(path: str) -> str | None:
    """Read an image file and return a data-URI string, or None on failure."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "bmp": "image/bmp"}.get(ext, "image/png")
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:
        return None


def _decision_badge(comment: str) -> str:
    """Return a colored HTML badge for the HITL decision embedded in comment."""
    c = (comment or "").upper()
    if "APPROVE" in c:
        return '<span class="badge approve">APPROVE</span>'
    if "REJECT" in c:
        return '<span class="badge reject">REJECT</span>'
    if "OVERRIDE" in c:
        return '<span class="badge override">OVERRIDE</span>'
    return '<span class="badge auto">AUTO</span>'


def _status_icon(status: str) -> str:
    s = (status or "").upper()
    if "CLASH" in s or "FAIL" in s:
        return "❌"
    if "CLEAR" in s or "PASS" in s:
        return "✅"
    return "—"


# ─────────────────────────────────────────────────────────────────────────────
# Main generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_html_report(db_path: str = DB_PATH,
                         output_dir: str = REPORTS_DIR) -> str:
    """
    Generate a self-contained HTML report from actuation_results.
    Returns the path of the saved .html file.
    """
    os.makedirs(output_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    cur.execute("SELECT * FROM actuation_results ORDER BY id")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("[REPORT] actuation_results is empty — no report generated.")
        return ""

    # ── Meta ─────────────────────────────────────────────────────────────────
    first        = rows[0]
    assembly     = first["assembly_name"] or "N/A"
    # For the filename: use assembly name (stable, unambiguous)
    report_tag   = assembly
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    total        = len(rows)
    clashes      = sum(1 for r in rows if "CLASH" in (r["type"] or "").upper()
                                       or "FAIL"  in (r["status"] or "").upper())
    clears       = total - clashes

    # ── Per-publication summary ───────────────────────────────────────────────
    pubs: dict[str, dict] = {}
    for r in rows:
        p = r["publication_name"] or "?"
        if p not in pubs:
            # Capture the drawing_name for this publication (first row wins)
            try:
                dwg = r["drawing_name"] or r["project_tag"] or "—"
            except Exception:
                dwg = r["project_tag"] or "—"
            pubs[p] = {"MMC": None, "LMC": None, "drawing": dwg}
        bd = (r["boundary"] or "").upper()
        if bd in ("MMC", "LMC"):
            # Worst status for this boundary
            cur_status = pubs[p][bd]
            new_status = r["status"] or ""
            if cur_status is None or "CLASH" in new_status.upper():
                pubs[p][bd] = new_status

    def _pub_cell(s):
        if s is None:
            return '<td class="na">—</td>'
        if "CLASH" in s.upper():
            return f'<td class="fail">❌ FAIL</td>'
        return f'<td class="pass">✅ PASS</td>'

    summary_rows_html = ""
    for pub, bd in pubs.items():
        summary_rows_html += f"""
          <tr>
            <td>{pub}</td>
            <td><span class="drawing-tag">{bd['drawing']}</span></td>
            {_pub_cell(bd["MMC"])}
            {_pub_cell(bd["LMC"])}
          </tr>"""

    # ── Detail rows ───────────────────────────────────────────────────────────
    detail_rows_html = ""
    for r in rows:
        img_b64    = _img_to_b64(r["image_path"])
        img_html   = ""
        if img_b64:
            row_id = r["id"]
            img_html = f"""
              <tr class="img-row" id="img-{row_id}" style="display:none;">
                <td colspan="10" style="padding:12px;background:#f8f8f8;text-align:center;">
                  <img src="{img_b64}"
                       style="max-width:100%;border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.2);"
                       alt="4-View clash montage"/>
                </td>
              </tr>"""
            toggle_js = f'onclick="toggleImg({row_id})" style="cursor:pointer;" title="Click to show/hide clash images"'
        else:
            toggle_js = ""

        status_icon  = _status_icon(r["status"] or "")
        decision_html = _decision_badge(r["comment"] or "")
        val_str      = f'{abs(float(r["value"])):.4f} mm' if r["value"] is not None else "—"
        clash_type   = r["type"] or "—"
        row_cls      = "row-clash" if "CLASH" in (r["type"] or "").upper() else "row-clear"

        detail_rows_html += f"""
          <tr class="{row_cls}" {toggle_js}>
            <td>{r["id"]}</td>
            <td><span class="drawing-tag">{r["drawing_name"] if "drawing_name" in r.keys() else (r["project_tag"] or "—")}</span></td>
            <td>{r["publication_name"] or "—"}</td>
            <td><span class="boundary-badge {(r["boundary"] or '').lower()}">{r["boundary"] or "—"}</span></td>
            <td>{r["product1"] or "—"}</td>
            <td>{r["product2"] or "—"}</td>
            <td>{status_icon} {clash_type}</td>
            <td>{val_str}</td>
            <td>{decision_html}</td>
            <td style="font-size:11px;color:#777;">{r["run_at"] or "—"}</td>
          </tr>{img_html}"""

    # ── HTML template ─────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>EATS Validation Report — {assembly}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Segoe UI", Arial, sans-serif;
      background: #F0F3F8;
      color: #0D1B3E;
      padding: 24px;
    }}

    /* ── Header ── */
    .header {{
      background: linear-gradient(135deg, #002E35 0%, #004852 60%, #006B7A 100%);
      color: #fff;
      padding: 28px 32px;
      border-radius: 10px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      border-bottom: 4px solid #FDB913;
    }}
    .header h1 {{ font-size: 22px; margin-bottom: 6px; letter-spacing: .3px; }}
    .header .meta {{ font-size: 13px; opacity: .85; line-height: 1.8; }}
    .header .stamp {{
      text-align: right;
      font-size: 12px;
      opacity: .80;
      white-space: nowrap;
    }}
    .header .traton-badge {{
      display: inline-block;
      background: #FDB913;
      color: #002E35;
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 1.5px;
      padding: 2px 8px;
      border-radius: 3px;
      margin-bottom: 8px;
      text-transform: uppercase;
    }}

    /* ── KPI strip ── */
    .kpi-strip {{
      display: flex;
      gap: 16px;
      margin-bottom: 24px;
    }}
    .kpi {{
      flex: 1;
      background: #fff;
      border-radius: 8px;
      padding: 16px 20px;
      text-align: center;
      box-shadow: 0 1px 6px rgba(0,72,82,.12);
      border-top: 3px solid #004852;
    }}
    .kpi .num {{ font-size: 32px; font-weight: 700; color: #004852; }}
    .kpi .lbl {{ font-size: 12px; color: #5A6A85; margin-top: 4px; font-weight: 500; }}
    .kpi.red  .num {{ color: #B91C1C; }}
    .kpi.green .num {{ color: #166534; }}
    .kpi.blue  .num {{ color: #004852; }}
    .kpi.gold  .num {{ color: #92400E; }}

    /* ── Card ── */
    .card {{
      background: #fff;
      border-radius: 8px;
      padding: 0;
      margin-bottom: 24px;
      box-shadow: 0 1px 6px rgba(0,72,82,.12);
      overflow: hidden;
      border: 1px solid #C0D8DB;
    }}
    .card-header {{
      background: linear-gradient(90deg, #004852 0%, #006B7A 100%);
      color: #fff;
      padding: 12px 20px;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: .4px;
      border-left: 4px solid #FDB913;
    }}

    /* ── Tables ── */
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th {{
      background: #E0EEF0;
      padding: 10px 14px;
      text-align: left;
      font-weight: 700;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .5px;
      color: #004852;
      border-bottom: 2px solid #9FC8CD;
    }}
    td {{
      padding: 10px 14px;
      border-bottom: 1px solid #C0D8DB;
      vertical-align: middle;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr.row-clash {{ background: #FFF5F5; }}
    tr.row-clear {{ background: #F5FBF7; }}
    tr.row-clash:hover, tr.row-clear:hover {{
      background: #D9EEF0;
      transition: background .15s;
    }}

    /* ── Summary cells ── */
    td.fail {{ color: #B91C1C; font-weight: 700; }}
    td.pass {{ color: #166534; font-weight: 700; }}
    td.na   {{ color: #9CA3AF; }}

    /* ── Badges ── */
    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .5px;
    }}
    .badge.approve  {{ background: #DCFCE7; color: #166534; }}
    .badge.reject   {{ background: #FEE2E2; color: #B91C1C; }}
    .badge.override {{ background: #FEF9C3; color: #92400E; }}
    .badge.auto     {{ background: #D9EEF0; color: #004852; }}
    .boundary-badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
    }}
    .boundary-badge.mmc {{ background: #CCE8EB; color: #004852; }}
    .boundary-badge.lmc {{ background: #FEF3C7; color: #92400E; }}

    .drawing-tag {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      background: #D9EEF0;
      color: #004852;
    }}
    /* ── Footer ── */
    .footer {{
      text-align: center;
      font-size: 11px;
      color: #8896B3;
      margin-top: 24px;
      padding: 12px 0;
      border-top: 1px solid #9FC8CD;
    }}
    .footer strong {{ color: #004852; }}

    /* ── Click-to-expand hint ── */
    .hint {{
      font-size: 12px;
      color: #5A6A85;
      padding: 8px 20px 12px;
      font-style: italic;
      background: #F2FAFB;
      border-bottom: 1px solid #C0D8DB;
    }}
  </style>
  <script>
    function toggleImg(id) {{
      var row = document.getElementById('img-' + id);
      if (!row) return;
      row.style.display = (row.style.display === 'none') ? 'table-row' : 'none';
    }}
  </script>
</head>
<body>

<!-- ── Header ── -->
<div class="header">
  <div>
    <div class="traton-badge">TRATON Group &nbsp;·&nbsp; Scania CV AB</div>
    <h1>&#9881;&nbsp; EATS — DMU Clash Validation Report</h1>
    <div class="meta">
      <span>&#127981;&nbsp; Assembly&nbsp;:&nbsp; <strong>{assembly}</strong></span>
    </div>
  </div>
  <div class="stamp">
    Generated<br/><strong>{generated_at}</strong>
  </div>
</div>

<!-- ── KPI strip ── -->
<div class="kpi-strip">
  <div class="kpi blue">
    <div class="num">{total}</div>
    <div class="lbl">Total Results</div>
  </div>
  <div class="kpi red">
    <div class="num">{clashes}</div>
    <div class="lbl">Clashes</div>
  </div>
  <div class="kpi green">
    <div class="num">{clears}</div>
    <div class="lbl">Clearances</div>
  </div>
  <div class="kpi blue">
    <div class="num">{len(pubs)}</div>
    <div class="lbl">Publications Tested</div>
  </div>
</div>

<!-- ── Publication summary ── -->
<div class="card">
  <div class="card-header">Publication Summary</div>
  <table>
    <thead>
      <tr>
        <th>Publication Name</th>
        <th>Source Drawing</th>
        <th>MMC Result</th>
        <th>LMC Result</th>
      </tr>
    </thead>
    <tbody>{summary_rows_html}
    </tbody>
  </table>
</div>

<!-- ── Detailed results ── -->
<div class="card">
  <div class="card-header">Detailed Conflict Results</div>
  <div class="hint">&#128247; Click a clash row to show / hide the 4-view montage image.</div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Source Drawing</th>
        <th>Publication</th>
        <th>Boundary</th>
        <th>Product A</th>
        <th>Product B</th>
        <th>Type</th>
        <th>Penetration</th>
        <th>HITL Decision</th>
        <th>Timestamp</th>
      </tr>
    </thead>
    <tbody>{detail_rows_html}
    </tbody>
  </table>
</div>

<div class="footer">
  <strong>EATS Validation Framework</strong> &nbsp;·&nbsp; TRATON Group / Scania CV AB &nbsp;·&nbsp; Auto-generated &nbsp;·&nbsp; {generated_at}
</div>

</body>
</html>
"""

    # ── Save ─────────────────────────────────────────────────────────────────
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"EATS_Report_{report_tag.replace(' ', '_')}_{ts}.html"
    out_path = os.path.join(output_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n📄  HTML report saved → {out_path}")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = generate_html_report()
    if p:
        import webbrowser
        webbrowser.open(p)
