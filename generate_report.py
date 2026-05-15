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

DB_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eats_validation.db")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


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
    project_tag  = first["project_tag"]  or "N/A"
    assembly     = first["assembly_name"] or "N/A"
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
            pubs[p] = {"MMC": None, "LMC": None}
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
            <td><strong>{r["project_tag"] or "—"}</strong></td>
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
  <title>EATS Validation Report — {project_tag}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Segoe UI", Arial, sans-serif;
      background: #f0f2f5;
      color: #1a1a2e;
      padding: 24px;
    }}

    /* ── Header ── */
    .header {{
      background: linear-gradient(135deg, #1a2744 0%, #2c3e6b 100%);
      color: #fff;
      padding: 28px 32px;
      border-radius: 10px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
    }}
    .header h1 {{ font-size: 22px; margin-bottom: 6px; }}
    .header .meta {{ font-size: 13px; opacity: .8; line-height: 1.7; }}
    .header .stamp {{
      text-align: right;
      font-size: 12px;
      opacity: .75;
      white-space: nowrap;
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
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
    }}
    .kpi .num {{ font-size: 32px; font-weight: 700; }}
    .kpi .lbl {{ font-size: 12px; color: #666; margin-top: 4px; }}
    .kpi.red  .num {{ color: #c0392b; }}
    .kpi.green .num {{ color: #27ae60; }}
    .kpi.blue  .num {{ color: #2980b9; }}

    /* ── Card ── */
    .card {{
      background: #fff;
      border-radius: 8px;
      padding: 0;
      margin-bottom: 24px;
      box-shadow: 0 1px 4px rgba(0,0,0,.08);
      overflow: hidden;
    }}
    .card-header {{
      background: #2c3e6b;
      color: #fff;
      padding: 12px 20px;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: .4px;
    }}

    /* ── Tables ── */
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th {{
      background: #eef1f7;
      padding: 10px 14px;
      text-align: left;
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .5px;
      color: #444;
      border-bottom: 2px solid #d0d7e8;
    }}
    td {{
      padding: 10px 14px;
      border-bottom: 1px solid #f0f2f5;
      vertical-align: middle;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr.row-clash {{ background: #fff8f8; }}
    tr.row-clear {{ background: #f8fff8; }}
    tr.row-clash:hover, tr.row-clear:hover {{
      background: #e8f0fe;
      transition: background .15s;
    }}

    /* ── Summary cells ── */
    td.fail {{ color: #c0392b; font-weight: 600; }}
    td.pass {{ color: #27ae60; font-weight: 600; }}
    td.na   {{ color: #999; }}

    /* ── Badges ── */
    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .5px;
    }}
    .badge.approve  {{ background: #d5f5e3; color: #1e8449; }}
    .badge.reject   {{ background: #fde8e8; color: #c0392b; }}
    .badge.override {{ background: #fef9e7; color: #b7770d; }}
    .badge.auto     {{ background: #eaf3fb; color: #2471a3; }}
    .boundary-badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
    }}
    .boundary-badge.mmc {{ background: #d6eaf8; color: #1a5276; }}
    .boundary-badge.lmc {{ background: #fdebd0; color: #784212; }}

    /* ── Footer ── */
    .footer {{
      text-align: center;
      font-size: 11px;
      color: #aaa;
      margin-top: 24px;
    }}

    /* ── Click-to-expand hint ── */
    .hint {{
      font-size: 12px;
      color: #888;
      padding: 8px 20px 12px;
      font-style: italic;
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
    <h1>&#9881;  EATS — DMU Validation Report</h1>
    <div class="meta">
      <span>&#128196; Project&nbsp;&nbsp;:&nbsp; <strong>{project_tag}</strong></span><br/>
      <span>&#127981; Assembly&nbsp;:&nbsp; <strong>{assembly}</strong></span>
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
        <th>Project Tag</th>
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
  EATS Validation Framework &nbsp;|&nbsp; Auto-generated report &nbsp;|&nbsp; {generated_at}
</div>

</body>
</html>
"""

    # ── Save ─────────────────────────────────────────────────────────────────
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"EATS_Report_{project_tag.replace(' ', '_')}_{ts}.html"
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
