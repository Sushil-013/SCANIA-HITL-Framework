import sqlite3
import sys
import traceback
import win32com.client
import time
import os
import re
import datetime
import tempfile
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageTk, ImageFont

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eats_validation.db')


# ──────────────────────────────────────────────────────────────────────────────
# CATIA PARAMETER RESOLUTION
# ──────────────────────────────────────────────────────────────────────────────

def _get_param(product, pub_name):
    """Try to resolve a published parameter from a product node."""
    try:
        ref      = product.ReferenceProduct
        part_doc = ref.Parent
        params   = part_doc.Part.Parameters
        try:
            return params.GetItem(pub_name)
        except Exception:
            pass
        for k in range(1, params.Count + 1):
            try:
                p = params.Item(k)
                n = str(p.Name)
                if n == pub_name or n.endswith('\\' + pub_name) or n.endswith('/' + pub_name):
                    return p
            except Exception:
                pass
    except Exception:
        pass
    try:
        pubs = product.Publications
        for k in range(1, pubs.Count + 1):
            try:
                pub = pubs.Item(k)
                if str(pub.Name) == pub_name:
                    ve = pub.ValuatedElement
                    _  = ve.Name   # verify it is accessible
                    return ve
            except Exception:
                pass
    except Exception:
        pass
    return None


def _find_product_with_pub(root_product, pub_name):
    """Recursively walk the product tree to find the node that owns pub_name."""
    try:
        pubs = root_product.Publications
        for k in range(1, pubs.Count + 1):
            try:
                if str(pubs.Item(k).Name) == pub_name:
                    param = _get_param(root_product, pub_name)
                    if param:
                        return root_product, param
            except Exception:
                pass
    except Exception:
        pass
    try:
        for j in range(1, root_product.Products.Count + 1):
            try:
                child = root_product.Products.Item(j)
                node, param = _find_product_with_pub(child, pub_name)
                if param:
                    return node, param
            except Exception:
                pass
    except Exception:
        pass
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
# DMU SPACE ANALYSIS — CLASH DETECTION
# ──────────────────────────────────────────────────────────────────────────────

def _run_clash_analysis(root_product, target_part_name):
    """
    Create a temporary 'Between all' Clash object, compute it, then FILTER
    the results to only those involving target_part_name ('Selection against all'
    behaviour).  Background static clashes between unrelated parts are discarded.

    The temporary clash object is NOT deleted here — caller owns cleanup via
    _cleanup_clash(clashes, new_clash) AFTER any human inspection pause.

    Returns: (results, clashes_collection, new_clash_object)
        results — list of dicts (only conflicts that involve target_part_name):
            {
              type, clearance, is_clash, detail,
              first_product, second_product,
              p1_name, p2_name,
              status, comment, location
            }

    ComputationType: 1 = catClashAndClearance
    Conflict.Type:   0 = Clash, 1 = Clearance, 2 = Contact
    """
    results   = []
    new_clash = None

    # ── Step 1: Obtain the Clashes collection ────────────────────────────
    try:
        clashes = root_product.GetTechnologicalObject("Clashes")
        print(f"    [DMU] Clashes collection obtained. Existing groups: {clashes.Count}")
    except Exception as e:
        print(f"    [DMU] ❌ Cannot get 'Clashes' technological object: {e}")
        print( "    [DMU]    Ensure the document is a CATProduct with SPA licence active.")
        return [], None, None

    # ── Step 2: Create a fresh temporary clash object ────────────────────
    try:
        new_clash = clashes.Add()
        print(f"    [DMU] Temporary clash object created.")
    except Exception as e:
        print(f"    [DMU] ❌ clashes.Add() failed: {e}")
        return [], clashes, None

    # ── Step 3: Set computation type ─────────────────────────────────────
    try:
        new_clash.ComputationType = 1   # catClashAndClearance
        print(f"    [DMU] ComputationType = 1 (Clash + Clearance).")
    except Exception as e:
        print(f"    [DMU] ⚠  Could not set ComputationType (non-fatal): {e}")

    # ── Step 4: Compute ───────────────────────────────────────────────────
    try:
        new_clash.Compute()
        print(f"    [DMU] Compute() finished.")
    except Exception as e:
        print(f"    [DMU] ❌ Compute() failed: {e}")
        return [], clashes, new_clash

    # ── Step 5: Read Conflicts ────────────────────────────────────────────
    try:
        conflicts = new_clash.Conflicts
        total     = conflicts.Count
        print(f"    [DMU] {total} total conflict(s) before target filter "
              f"(target='{target_part_name}').")
    except Exception as e:
        print(f"    [DMU] ❌ Cannot read Conflicts collection: {e}")
        return [], clashes, new_clash

    type_map = {0: "Clash", 1: "Clearance", 2: "Contact"}

    for r in range(1, total + 1):
        try:
            res = conflicts.Item(r)

            # ─ Product names ─────────────────────────────────────────
            fp = sp = None
            try:
                fp = res.FirstProduct
                p1 = fp.Name
            except Exception:
                p1 = "?"
            try:
                sp = res.SecondProduct
                p2 = sp.Name
            except Exception:
                p2 = "?"

            # ─ TARGETED FILTER — skip if target not involved ────────────
            if target_part_name and \
               target_part_name not in p1 and \
               target_part_name not in p2:
                continue   # background clash — not relevant to this parameter

            # ─ Type ──────────────────────────────────────────────
            try:
                ctype_raw = int(res.Type)
                ctype     = type_map.get(ctype_raw, f"Unknown({ctype_raw})")
            except Exception:
                ctype = "Unknown"

            # ─ Distance (value) ──────────────────────────────────────
            dist = None
            for attr in ("MinDistance", "Value", "Distance"):
                try:
                    dist = float(getattr(res, attr))
                    break
                except Exception:
                    continue
            if dist is None:
                dist = 0.0

            # ─ Status ─────────────────────────────────────────────
            try:
                status = str(res.Status)
            except Exception:
                status = "Computed"

            # ─ Comment ────────────────────────────────────────────
            try:
                comment = str(res.Comment)
            except Exception:
                comment = "Automated MMC/LMC check"

            # ─ Location (clash point coordinates) ───────────────────
            # Try every known V5 SPA property name for clash coordinates
            location = "N/A"
            for coord_attr in ("Coordinates", "Point", "ClashPoint",
                               "Coordinate", "Location", "ClashLocation"):
                try:
                    coords = getattr(res, coord_attr)
                    location = f"({float(coords[0]):.3f}, {float(coords[1]):.3f}, {float(coords[2]):.3f})"
                    break
                except Exception:
                    continue

            # PASS = Clearance/Contact (dist >= 0) • FAIL = Clash (dist < 0)
            is_clash = (ctype == "Clash") or (dist < 0)
            detail   = f"{ctype} | dist={dist:.4f} mm | {p1} ↔ {p2}"

            results.append({
                "type"          : ctype,
                "clearance"     : dist,
                "is_clash"      : is_clash,
                "detail"        : detail,
                "first_product" : fp,
                "second_product": sp,
                "p1_name"       : p1,
                "p2_name"       : p2,
                "status"        : status,
                "comment"       : comment,
                "location"      : location,
            })

        except Exception as item_e:
            print(f"    [DMU] ⚠  Could not read conflict item {r}: {item_e}")

    print(f"    [DMU] {len(results)} conflict(s) involve target '{target_part_name}' "
          f"({total - len(results)} background clash(es) filtered out).")

    # NOTE: no cleanup here — caller owns the lifecycle of new_clash
    return results, clashes, new_clash


def _cleanup_clash(clashes, new_clash):
    """Delete the temporary clash object from the CATIA spec tree."""
    if clashes is None or new_clash is None:
        return
    try:
        clashes.Remove(new_clash.Name)
        print(f"    [DMU] Temporary clash object removed from spec tree.")
    except Exception as del_e:
        try:
            new_clash.Delete()
            print(f"    [DMU] Temporary clash object deleted via .Delete().")
        except Exception as del_e2:
            print(f"    [DMU] ⚠  Could not remove temporary clash object "
                  f"({del_e} / {del_e2}). Remove it manually from the tree.")


def _parse_location(location_str):
    """Parse a location string like '(x, y, z)' into [x, y, z] floats, or None."""
    try:
        if location_str and location_str != 'N/A':
            nums = [float(v.strip('() ')) for v in location_str.split(',')]
            if len(nums) == 3:
                return nums
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# SVG — CAPTURE + ANNOTATE CLASH SCREENSHOT
# ──────────────────────────────────────────────────────────────────────────────
# CATIA VISIBILITY HELPERS
# ──────────────────────────────────────────────────────────────────────────────

_CAT_SHOW   = 0   # catVisPropertyShowAttr
_CAT_NOSHOW = 1   # catVisPropertyNoShowAttr


def _get_all_products(root_product):
    """Return a flat list of every descendant Product node."""
    result = []
    try:
        count = root_product.Products.Count
        for i in range(1, count + 1):
            try:
                child = root_product.Products.Item(i)
                result.append(child)
                result.extend(_get_all_products(child))
            except Exception:
                pass
    except Exception:
        pass
    return result


def _set_vis(active_doc, products, show_value):
    """Batch-set visibility for a list of products via Selection API."""
    if not products:
        return
    try:
        sel = active_doc.Selection
        sel.Clear()
        for p in products:
            try: sel.Add(p)
            except Exception: pass
        sel.VisProperties.SetShow(show_value)
        sel.Clear()
    except Exception as e:
        print(f"    [VIS] _set_vis failed: {e}")


def _isolate_pair(active_doc, root_product, fp, sp):
    """Hide every leaf part except fp and sp; show fp, sp, and all folders."""
    try:
        all_prods = _get_all_products(root_product)
        to_hide, to_show = [], [root_product]
        fp_name = fp.Name if fp else "___NONE___"
        sp_name = sp.Name if sp else "___NONE___"
        for p in all_prods:
            try:
                if p.Name in (fp_name, sp_name):
                    to_show.append(p)
                elif p.Products.Count == 0:
                    to_hide.append(p)
                else:
                    to_show.append(p)
            except Exception:
                pass
        _set_vis(active_doc, to_hide,  _CAT_NOSHOW)
        _set_vis(active_doc, to_show,  _CAT_SHOW)
        print(f"    [VIS] Isolated: {len(to_show)} shown, {len(to_hide)} hidden.")
    except Exception as e:
        print(f"    [VIS] _isolate_pair failed: {e}")


def _global_unhide(catia, active_doc, root_product):
    """Restore full assembly visibility and force CATIA to repaint."""
    try:
        all_prods = _get_all_products(root_product)
        all_prods.append(root_product)
        _set_vis(active_doc, all_prods, _CAT_SHOW)
        print("    [VIS] Global unhide complete.")
    except Exception as e:
        print(f"    [VIS] Global unhide failed: {e}")
    # Force viewer repaint so geometry reappears immediately
    try:
        catia.ActiveWindow.ActiveViewer.Update()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────

def _capture_and_annotate_clash(catia, active_doc, clash_result, clash_idx=0):
    """
    Single CATIA screenshot approach — eliminates CATIA camera control dependency:
      1. Reframe on both clashing parts → capture ONE screenshot.
      2. Save exact camera state (origin, sight, up, sight_dist).
      3. Project 3D clash coordinates → pixel (px, py) in that screenshot using
         orthographic projection (CATIA V5 default).
      4. Pillow crops a tight window around (px, py) for the ZOOMED CLASH VIEW.
         This guarantees the clash point is exactly at the centre of the crop.
      5. Draw clash marker at the crop centre — always correct by construction.
    Returns montage PNG path.
    """
    import math as _math

    tmp_dir  = tempfile.gettempdir()
    out_path = os.path.join(tmp_dir, f'_catia_clash_montage_{clash_idx}.png')

    try:
        viewer = catia.ActiveWindow.ActiveViewer
    except Exception as e:
        print(f"    [SVG] Cannot access viewer: {e}")
        return _make_fallback_image(clash_result, out_path)

    fp     = clash_result.get("first_product")
    sp     = clash_result.get("second_product")
    coords = _parse_location(clash_result.get("location", "N/A"))

    # ── Save original viewpoint ───────────────────────────────────────────────
    saved_origin = saved_sight = saved_up = saved_sight_dist = None
    try:
        vp               = viewer.Viewpoint3D
        saved_origin     = list(vp.GetOrigin())
        saved_sight      = list(vp.GetSightDirection())
        saved_up         = list(vp.GetUpDirection())
        saved_sight_dist = float(vp.SightDistance)
    except Exception:
        pass

    # ── ONE screenshot: reframe on both clashing parts ────────────────────────
    def _catia_capture(viewer, idx, label):
        tmp = tempfile.gettempdir()
        for fmt_id, ext in ((3, '.png'), (0, '.bmp')):
            p = os.path.join(tmp, f'_catia_clash_{label}_{idx}{ext}')
            try:
                viewer.CaptureToFile(fmt_id, p)
                time.sleep(0.6)
                if os.path.isfile(p) and os.path.getsize(p) > 2000:
                    return p, True
            except Exception:
                pass
        return os.path.join(tmp, f'_catia_clash_{label}_{idx}.bmp'), False

    ov_captured = False
    path_ov = os.path.join(tmp_dir, f'_catia_clash_ov_{clash_idx}.png')
    # Capture camera state AFTER reframe (used for projection)
    cam_origin = cam_sight = cam_up = cam_dist = None
    try:
        sel = active_doc.Selection
        sel.Clear()
        if fp: sel.Add(fp)
        if sp: sel.Add(sp)
        viewer.Reframe()
        sel.Clear()
        viewer.Update()
        time.sleep(0.5)
        # Save camera state right after Reframe — this is what the screenshot sees
        try:
            vp2        = viewer.Viewpoint3D
            cam_origin = list(vp2.GetOrigin())
            cam_sight  = list(vp2.GetSightDirection())
            cam_up     = list(vp2.GetUpDirection())
            cam_dist   = float(vp2.SightDistance)
        except Exception:
            pass
        path_ov, ov_captured = _catia_capture(viewer, clash_idx, 'ov')
        if ov_captured:
            print(f"    [SVG] Context shot saved: {path_ov}")
    except Exception as e:
        print(f"    [SVG] Context shot failed: {e}")

    # ── Restore original viewpoint ────────────────────────────────────────────
    if saved_origin is not None:
        try:
            vp = viewer.Viewpoint3D
            vp.PutOrigin(saved_origin)
            vp.PutSightDirection(saved_sight)
            vp.PutUpDirection(saved_up)
            vp.SightDistance = saved_sight_dist
            viewer.Update()
        except Exception:
            pass

    if not ov_captured:
        print("    [SVG] Screenshot failed — generating fallback image.")
        return _make_fallback_image(clash_result, out_path)

    # ── Pillow processing ─────────────────────────────────────────────────────
    try:
        raw = Image.open(path_ov).convert("RGB")
        RW, RH = raw.size

        # Panel size constants
        CTX_W, CTX_H = 380, 280    # context thumbnail
        DET_W, DET_H = 980, 640    # detail panel
        SEP          = 8
        LABEL_H      = 46

        # ── Orthographic projection helpers ───────────────────────────────────
        def _vdot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
        def _vnorm(v):
            m = _math.sqrt(sum(x*x for x in v))
            return [x/m for x in v] if m > 0 else list(v)
        def _vcross(a, b):
            return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

        def _project(p3d, origin, sight, up, dist, iw, ih):
            """3D world point → (px, py) pixel in screenshot."""
            s = _vnorm(sight); u = _vnorm(up); r = _vnorm(_vcross(s, u))
            dv = [p3d[i]-origin[i] for i in range(3)]
            xc, yc = _vdot(dv, r), _vdot(dv, u)
            sc = min(iw, ih) / dist if dist and dist > 0 else 1.0
            return int(round(iw/2 + xc*sc)), int(round(ih/2 - yc*sc))

        # ── Project clash coords → pixel in raw screenshot ────────────────────
        clash_px = clash_py = None
        if coords and cam_origin:
            try:
                clash_px, clash_py = _project(coords, cam_origin, cam_sight,
                                               cam_up, cam_dist, RW, RH)
                # Clamp to image bounds
                clash_px = max(0, min(clash_px, RW - 1))
                clash_py = max(0, min(clash_py, RH - 1))
                print(f"    [SVG] Clash projected to pixel ({clash_px}, {clash_py}) in {RW}×{RH}")
            except Exception as pe:
                print(f"    [SVG] Projection failed: {pe}")

        # ── Context thumbnail — strip spec tree (left 25%) ────────────────────
        ctx_raw = raw.crop((RW // 4, 0, RW, RH))
        ctx_img = ctx_raw.resize((CTX_W, CTX_H), Image.Resampling.LANCZOS)

        # ── Detail zoom — crop around clash pixel; fall back to centre ─────────
        # Crop a 40% × 40% window of the raw image centred on the clash pixel.
        # Because we crop around the clash point, the marker is ALWAYS at crop centre.
        CROP_F = 0.40   # fraction of raw image to crop (40% × 40%)
        hw = int(RW * CROP_F / 2)
        hh = int(RH * CROP_F / 2)

        if clash_px is not None:
            cx, cy = clash_px, clash_py
        else:
            cx, cy = RW // 2, RH // 2   # centre fallback

        # Clamp crop box to image boundaries
        l = max(0, cx - hw);  r_  = min(RW, cx + hw)
        t = max(0, cy - hh);  b   = min(RH, cy + hh)
        # Adjust cx/cy so marker stays at crop centre after clamping
        actual_cx = (l + r_) // 2
        actual_cy = (t + b)  // 2

        zoom_crop = raw.crop((l, t, r_, b))
        det_img   = zoom_crop.resize((DET_W, DET_H), Image.Resampling.LANCZOS)

        # ── Build canvas ──────────────────────────────────────────────────────
        CANVAS_W = CTX_W + SEP + DET_W
        CANVAS_H = LABEL_H + DET_H

        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (255, 255, 255))
        canvas.paste(ctx_img, (0, LABEL_H + (DET_H - CTX_H) // 2))
        canvas.paste(det_img, (CTX_W + SEP, LABEL_H))

        draw = ImageDraw.Draw(canvas)
        draw.rectangle((CTX_W, 0, CTX_W + SEP, CANVAS_H), fill=(160, 175, 210))

        # Fonts
        try:
            f_label  = ImageFont.truetype("arialbd.ttf", 22)
            f_marker = ImageFont.truetype("arialbd.ttf", 18)
            f_msub   = ImageFont.truetype("arial.ttf",   14)
        except Exception:
            f_label = f_marker = f_msub = ImageFont.load_default()

        # ── Clash marker — always at the centre of the detail crop ────────────
        val_mm = float(clash_result.get("clearance", 0.0))
        ctype  = str(clash_result.get("type", "CLASH")).upper()

        # Marker is at the exact centre of the detail panel (= crop centre = clash point)
        mx = CTX_W + SEP + DET_W // 2
        my = LABEL_H + DET_H // 2

        R1, R2, R3   = 14, 32, 52
        CLR_RED      = (220, 30, 30)
        CLR_WHITE    = (255, 255, 255)
        CLR_ORANGE   = (255, 140, 0)

        draw.ellipse((mx-R3, my-R3, mx+R3, my+R3), outline=CLR_ORANGE, width=3)
        draw.ellipse((mx-R2, my-R2, mx+R2, my+R2), outline=CLR_RED,    width=3)
        ARM = 72
        draw.line((mx-ARM, my, mx-R1-2, my), fill=CLR_RED, width=2)
        draw.line((mx+R1+2, my, mx+ARM, my), fill=CLR_RED, width=2)
        draw.line((mx, my-ARM, mx, my-R1-2), fill=CLR_RED, width=2)
        draw.line((mx, my+R1+2, mx, my+ARM), fill=CLR_RED, width=2)
        draw.ellipse((mx-R1, my-R1, mx+R1, my+R1), fill=CLR_RED, outline=CLR_WHITE, width=2)

        # Label box
        lbl_x = mx + R3 + 10;  lbl_y = my - 10
        box_w, box_h = 270, 52
        if lbl_x + box_w > CANVAS_W:
            lbl_x = mx - R3 - box_w - 10
        draw.rectangle((lbl_x, lbl_y, lbl_x+box_w, lbl_y+box_h),
                       fill=(20, 20, 30), outline=CLR_RED, width=2)
        draw.line((mx+R1, my, lbl_x, lbl_y+box_h//2), fill=CLR_RED, width=2)
        draw.text((lbl_x+10, lbl_y+4),  f"⚠ {ctype}",
                  fill=(255, 90, 90),   font=f_marker)
        draw.text((lbl_x+10, lbl_y+28), f"Penetration: {abs(val_mm):.4f} mm",
                  fill=(255, 220, 180), font=f_msub)

        # ── Header bars ───────────────────────────────────────────────────────
        draw.rectangle((0, 0, CTX_W, LABEL_H), fill=(245, 247, 252))
        draw.rectangle((0, LABEL_H-4, CTX_W, LABEL_H), fill=(30, 80, 180))
        draw.text((12, 10), "🔍  CONTEXT VIEW", fill=(20, 50, 130), font=f_label)

        draw.rectangle((CTX_W+SEP, 0, CANVAS_W, LABEL_H), fill=(255, 246, 246))
        draw.rectangle((CTX_W+SEP, LABEL_H-4, CANVAS_W, LABEL_H), fill=(190, 30, 30))
        draw.text((CTX_W+SEP+14, 10), "🔎  ZOOMED CLASH VIEW",
                  fill=(150, 15, 15), font=f_label)

        canvas.save(out_path)
        print(f"    [SVG] Montage saved: {out_path}")

        try:
            if os.path.exists(path_ov): os.remove(path_ov)
        except Exception:
            pass

        return out_path

    except Exception as e:
        import traceback
        print(f"    [SVG] Pillow montage failed (non-fatal): {e}")
        traceback.print_exc()
        return path_ov if ov_captured else _make_fallback_image(clash_result, out_path)


def _make_fallback_image(clash_result, out_path):
    """Generate a light-grey placeholder image when CATIA capture is unavailable."""
    try:
        img  = Image.new("RGB", (800, 500), (240, 240, 240))
        draw = ImageDraw.Draw(img)
        draw.text((24, 24),  "CATIA capture unavailable", fill=(180, 0, 0))
        draw.text((24, 64),  f"Type : {clash_result.get('type','?')}",  fill=(40, 40, 40))
        draw.text((24, 94),  f"Depth: {clash_result.get('clearance',0):.4f} mm", fill=(40, 40, 40))
        draw.text((24, 124), f"{clash_result.get('p1_name','?')} \u2194 {clash_result.get('p2_name','?')}",
                  fill=(40, 40, 40))
        img.save(out_path)
    except Exception:
        pass
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# SVG — HITL TKINTER POPUP  (light theme, one clash at a time)
# ──────────────────────────────────────────────────────────────────────────────

# Injected by master_app before running the actuator.
# When set to a tk/ctk Frame, HITL UI is embedded in that frame instead of
# opening a separate tk.Tk() window.
_HITL_FRAME = None

# Set by master_app before calling run_agent_3_actuation() when the active CATIA
# document name doesn't exactly match any assembly in the DB.  Value should be
# an integer assembly_id.  When None the old terminal input() fallback is used.
_ASSEMBLY_CHOICE = None


def _build_hitl_content(container, decision, on_decide, image_path,
                        clash_result, pub_name, boundary, clash_idx,
                        total_clashes, embedded=False):
    """
    Build all HITL widgets into *container* (any tk Frame or Tk root).
    Calls on_decide() when the user presses a decision button.
    """
    BG_ROOT   = "#f4f6f9"
    BG_CARD   = "#ffffff"
    BG_HEADER = "#1a2744"
    BG_SUBHDR = "#2c3e6b"
    BG_IMG    = "#ffffff"
    FG_WHITE  = "#ffffff"
    FG_DARK   = "#1a1a2e"
    FG_MUTED  = "#6c757d"
    ACCENT    = "#e84040"
    SEP       = "#dee2e6"

    try:
        f_h1   = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        f_h2   = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        f_body = tkfont.Font(family="Segoe UI", size=10)
        f_val  = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        f_btn  = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        f_sm   = tkfont.Font(family="Segoe UI", size=8)
    except Exception:
        f_h1 = f_h2 = f_body = f_val = f_btn = f_sm = None

    if not embedded:
        container.configure(bg=BG_ROOT)

    # ── TOP HEADER ────────────────────────────────────────────────────────
    hdr = tk.Frame(container, bg=BG_HEADER)
    hdr.pack(fill="x", side="top")

    tk.Label(hdr, text="  ⚠  DMU CLASH DETECTION REVIEW",
             bg=BG_HEADER, fg=FG_WHITE, font=f_h1,
             pady=10, anchor="w").pack(side="left", padx=(6, 0))

    badge_f = tk.Frame(hdr, bg=BG_HEADER)
    badge_f.pack(side="right", padx=12, pady=6)
    tk.Label(badge_f, text=f"  {boundary}  ",
             bg=ACCENT, fg=FG_WHITE, font=f_h2,
             padx=8, pady=3).pack(side="right", padx=(4, 0))
    tk.Label(badge_f, text=f"  {pub_name}  ",
             bg=BG_SUBHDR, fg=FG_WHITE, font=f_body,
             padx=10, pady=5).pack(side="right", padx=(0, 4))
    tk.Label(badge_f,
             text=f"  Conflict  {clash_idx + 1} / {total_clashes}  ",
             bg="#3d5a99", fg=FG_WHITE, font=f_sm,
             padx=6, pady=5).pack(side="right", padx=(0, 4))

    # ── FOOTER STATUS BAR — packed BEFORE content so it is not squeezed out ──
    tk.Label(
        container,
        text=(f"  CATIA is live — two parts isolated for inspection  "
              f"│  {pub_name} @ {boundary}  "
              f"│  Conflict {clash_idx + 1} of {total_clashes}"),
        bg=BG_HEADER, fg="#adb5bd",
        font=f_sm, pady=5, anchor="w", padx=14
    ).pack(fill="x", side="bottom")

    # ── DECISION BUTTONS — packed BEFORE content (side="bottom") ─────────
    tk.Frame(container, bg=SEP, height=1).pack(fill="x", side="bottom")

    btn_bar = tk.Frame(container, bg=BG_ROOT, pady=12)
    btn_bar.pack(fill="x", side="bottom", padx=16)

    def _decide(val):
        decision["value"] = val
        on_decide()

    btn_defs = [
        ("✔  Approve Clash",            "approve",  "#1e8449", FG_WHITE,
         "Interference is genuine and expected."),
        ("✖  Reject  (False Positive)",  "reject",   "#c0392b", FG_WHITE,
         "Mark as false positive — no real interference."),
        ("▲  Override",                  "override", "#d68910", FG_WHITE,
         "Accept with reservation — flag for review."),
    ]
    for txt, val, bg, fg, tip in btn_defs:
        col = tk.Frame(btn_bar, bg=BG_ROOT)
        col.pack(side="left", expand=True, fill="x", padx=6)
        tk.Button(
            col, text=txt,
            bg=bg, fg=fg,
            activebackground=fg, activeforeground=bg,
            font=f_btn, relief="flat", cursor="hand2",
            pady=12,
            command=lambda v=val: _decide(v)
        ).pack(fill="x")
        tk.Label(col, text=tip, bg=BG_ROOT, fg=FG_MUTED,
                 font=f_sm, anchor="center").pack()

    # ── MAIN CONTENT ROW — packed LAST so it fills remaining space ────────
    content = tk.Frame(container, bg=BG_ROOT)
    content.pack(fill="both", expand=True, padx=12, pady=(10, 4))

    # RIGHT panel (packed first — rigid wall)
    right = tk.Frame(content, bg=BG_CARD, width=360)
    right.pack(side="right", fill="y", padx=(12, 0))
    right.pack_propagate(False)

    tk.Frame(right, bg=ACCENT, height=4).pack(fill="x")
    tk.Label(right, text="  CLASH INFORMATION",
             bg=BG_SUBHDR, fg=FG_WHITE, font=f_h2,
             anchor="w", pady=6).pack(fill="x")

    r = clash_result
    val_mm = float(r.get("clearance", 0.0))

    def _row(label, value, vc=FG_DARK):
        row = tk.Frame(right, bg=BG_CARD)
        row.pack(fill="x", padx=14, pady=(5, 0))
        tk.Label(row, text=label, bg=BG_CARD, fg=FG_MUTED,
                 font=f_sm, anchor="w", width=13).pack(side="left")
        tk.Label(row, text=value, bg=BG_CARD, fg=vc,
                 font=f_body, anchor="w", wraplength=210,
                 justify="left").pack(side="left", fill="x", expand=True)

    def _hsep():
        tk.Frame(right, bg=SEP, height=1).pack(fill="x", padx=14, pady=5)

    df = tk.Frame(right, bg=BG_CARD)
    df.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(df, text="Penetration Depth",
             bg=BG_CARD, fg=FG_MUTED, font=f_sm, anchor="w").pack(anchor="w")
    depth_str = f"{val_mm:.4f} mm"
    tk.Label(df, text=depth_str,
             bg=BG_CARD, fg=ACCENT, font=f_val, anchor="w").pack(anchor="w")

    _hsep()
    _row("Type",    r.get("type",    "?").upper())
    _row("Status",  r.get("status",  "?"))
    _hsep()
    _row("Part A",  r.get("p1_name", "?"))
    _row("Part B",  r.get("p2_name", "?"))
    _hsep()
    _row("Coords",  r.get("location", "N/A"), FG_MUTED)
    _hsep()
    _row("Boundary", boundary)
    _row("Publication", pub_name)

    tk.Frame(right, bg=SEP, height=1).pack(fill="x", pady=(10, 0))
    tk.Label(right, text="  INSTRUCTIONS",
             bg=BG_SUBHDR, fg=FG_WHITE, font=f_h2,
             anchor="w", pady=5).pack(fill="x")
    tk.Label(right,
             text=("Parts are isolated in CATIA.\n"
                   "Rotate and inspect the live model\n"
                   "on your other monitor, then choose\n"
                   "a decision below."),
             bg=BG_CARD, fg=FG_DARK, font=f_body,
             anchor="w", justify="left",
             padx=14, pady=10, wraplength=320).pack(fill="x")

    # LEFT panel — screenshot
    left = tk.Frame(content, bg=BG_IMG)
    left.pack(side="left", fill="both", expand=True)

    img_lbl = tk.Label(left, bg=BG_IMG, text="Loading screenshot…",
                       fg="#6c757d", anchor="center")
    img_lbl.pack(fill="both", expand=True, padx=3, pady=3)

    _pil = [None]
    _ref = [None]

    def _render(event=None):
        if _pil[0] is None:
            return
        fw = max(100, left.winfo_width()  - 6)
        fh = max(100, left.winfo_height() - 6)
        fit = _pil[0].copy()
        fit.thumbnail((fw, fh), Image.Resampling.LANCZOS)
        _ref[0] = ImageTk.PhotoImage(fit)
        img_lbl.configure(image=_ref[0], text="")

    try:
        _pil[0] = Image.open(image_path).convert("RGB")
        container.update_idletasks()
        _render()
    except Exception as ie:
        img_lbl.configure(text=f"Image unavailable:\n{ie}", fg="#e74c3c")

    left.bind("<Configure>", _render)


def _show_hitl_ui(image_path, clash_result, pub_name, boundary, clash_idx, total_clashes):
    """
    Blocking HITL review.
    • If _HITL_FRAME is set (injected by master_app), builds UI inside that frame
      and blocks the calling thread via threading.Event until a decision is made.
    • Otherwise falls back to a standalone tk.Tk() window.
    Returns 'approve' | 'reject' | 'override'.
    """
    import threading as _threading
    decision = {"value": "approve"}

    if _HITL_FRAME is not None:
        # ── Embedded mode ─────────────────────────────────────────────────
        _done = _threading.Event()

        def _build():
            for w in _HITL_FRAME.winfo_children():
                w.destroy()
            _build_hitl_content(
                _HITL_FRAME, decision, _done.set,
                image_path, clash_result, pub_name, boundary,
                clash_idx, total_clashes, embedded=True
            )

        _HITL_FRAME.after(0, _build)
        _done.wait()
        # Clear the frame so it is blank for the next clash (or when done)
        _HITL_FRAME.after(0, lambda: [w.destroy() for w in _HITL_FRAME.winfo_children()])
        return decision["value"]

    else:
        # ── Standalone window mode ────────────────────────────────────────
        root = tk.Tk()
        root.title(
            f"DMU Clash Review  —  {pub_name}  [{boundary}]  "
            f"({clash_idx + 1} / {total_clashes})"
        )
        root.configure(bg="#f4f6f9")
        root.resizable(True, True)
        root.geometry("1280x740")
        root.minsize(900, 560)

        _build_hitl_content(
            root, decision, root.destroy,
            image_path, clash_result, pub_name, boundary,
            clash_idx, total_clashes, embedded=False
        )
        root.protocol("WM_DELETE_WINDOW", lambda: (
            decision.__setitem__("value", "approve"), root.destroy()
        ))
        root.lift()
        root.focus_force()
        root.mainloop()
        return decision["value"]


def _summarise_clash_results(clash_results, boundary_label):
    """Print formatted DMU results and return (has_clash, summary_str)."""
    if clash_results is None or len(clash_results) == 0:
        # Errors were already printed inside _run_clash_analysis.
        print(f"    [DMU {boundary_label}] No conflict results returned (see DMU errors above).")
        return False, "SPA_UNAVAILABLE"

    has_clash = any(r["is_clash"] for r in clash_results)
    print(f"    [DMU {boundary_label}] {len(clash_results)} conflict result(s):")
    for r in clash_results:
        icon = "❌ CLASH" if r["is_clash"] else "✅ CLEAR"
        print(f"      {icon}  {r['detail']}")

    if has_clash:
        clashes_only = [r for r in clash_results if r["is_clash"]]
        worst = min(clashes_only, key=lambda x: x["clearance"])
        summary = (
            f"FAIL — {len(clashes_only)} clash(es) | "
            f"worst penetration={worst['clearance']:.4f} mm"
        )
    else:
        min_clearance = min(r["clearance"] for r in clash_results)
        summary = f"PASS — no clashes | min_clearance={min_clearance:.4f} mm"

    return has_clash, summary


# ──────────────────────────────────────────────────────────────────────────────
# SINGLE-PARAMETER ACTUATION WITH DMU VALIDATION
# ──────────────────────────────────────────────────────────────────────────────

def _actuate_parameter(catia, active_doc, root_product,
                        pub_name, part_path, feature_name, nominal, upper_tol, lower_tol):
    """
    Push pub_name to MMC then LMC, run targeted DMU clash detection at each
    extreme, pause for human inspection on a clash, then unconditionally revert
    to nominal.

    Returns (upper_summary, lower_summary, mmc_rows, lmc_rows) where
    *_rows are the filtered per-conflict dicts for DB saving.
    """
    # Derive target from part_path — last two dot-delimited tokens (e.g. '3289049.1')
    parts = part_path.strip().split('.')
    target_part_name = '.'.join(parts[-2:]) if len(parts) >= 2 else part_path
    print(f"  Target part filter: '{target_part_name}'")

    _, param = _find_product_with_pub(root_product, pub_name)

    if param is None:
        print(f"  ⚠  Parameter '{pub_name}' not found in product tree.")
        return "NOT FOUND", "NOT FOUND", [], []

    try:
        original_val = float(param.Value)
    except Exception:
        original_val = nominal
    print(f"  Current CATIA value: {original_val} mm")

    upper_limit   = nominal + upper_tol
    lower_limit   = nominal + lower_tol
    upper_summary = "SKIPPED"
    lower_summary = "SKIPPED"
    mmc_db_rows   = []
    lmc_db_rows   = []

    # ── try/finally guarantees reversion to nominal no matter what ────────
    # Clash handles are keyed so the finally safety-net can clean any that
    # weren't already deleted in the normal pass/fail branches.
    cleanup_pairs = []   # list of (clashes_col, new_clash) to clean in finally

    try:
        # ── MMC (Maximum Material Condition) ─────────────────────────────
        print(f"\n  ┌─ [MMC] Setting '{pub_name}' → {upper_limit} mm ...")
        param.Value = upper_limit
        root_product.Update()
        time.sleep(1.5)

        mmc_results, mmc_clashes_col, mmc_new_clash = _run_clash_analysis(
            root_product, target_part_name)
        cleanup_pairs.append([mmc_clashes_col, mmc_new_clash])  # register for safety-net
        sys.stdout.flush()  # ensure all DMU prints are visible before summary

        has_mmc_clash, upper_summary = _summarise_clash_results(mmc_results, "MMC")
        print(f"  └─ MMC result: {upper_summary}")
        mmc_db_rows = mmc_results

        if has_mmc_clash:
            clashes_only = [r for r in mmc_results if r.get("is_clash")]
            total = len(clashes_only)
            reviewed = []
            for idx, clash_r in enumerate(clashes_only):
                fp = clash_r.get("first_product")
                sp = clash_r.get("second_product")
                print(f"  [SVG] MMC clash {idx+1}/{total}: isolating {clash_r.get('p1_name','?')} ↔ {clash_r.get('p2_name','?')}")
                _isolate_pair(active_doc, root_product, fp, sp)
                img_path = _capture_and_annotate_clash(catia, active_doc, clash_r, clash_idx=idx)
                dec = _show_hitl_ui(img_path, clash_r, pub_name, "MMC", idx, total)
                print(f"  [HITL] MMC clash {idx+1}/{total} decision: {dec.upper()}")
                reviewed.append(dict(clash_r,
                                     comment=f"HITL:{dec}",
                                     keep=("Yes" if dec == "approve" else "No")))
            # All MMC clashes reviewed — now restore visibility
            _global_unhide(catia, active_doc, root_product)
            # Merge reviewed rows back with non-clash rows
            non_clash = [r for r in mmc_results if not r.get("is_clash")]
            mmc_db_rows = reviewed + non_clash
        # Cleanup AFTER possible input() pause (or immediately on PASS)
        _cleanup_clash(mmc_clashes_col, mmc_new_clash)
        cleanup_pairs[-1][1] = None  # mark as done so finally skips it

        if not has_mmc_clash:
            time.sleep(0.5)

        # ── LMC (Least Material Condition) ────────────────────────────────
        print(f"\n  ┌─ [LMC] Setting '{pub_name}' → {lower_limit} mm ...")
        param.Value = lower_limit
        root_product.Update()
        time.sleep(1.5)

        lmc_results, lmc_clashes_col, lmc_new_clash = _run_clash_analysis(
            root_product, target_part_name)
        cleanup_pairs.append([lmc_clashes_col, lmc_new_clash])
        sys.stdout.flush()

        has_lmc_clash, lower_summary = _summarise_clash_results(lmc_results, "LMC")
        print(f"  └─ LMC result: {lower_summary}")
        lmc_db_rows = lmc_results

        if has_lmc_clash:
            clashes_only = [r for r in lmc_results if r.get("is_clash")]
            total = len(clashes_only)
            reviewed = []
            for idx, clash_r in enumerate(clashes_only):
                fp = clash_r.get("first_product")
                sp = clash_r.get("second_product")
                print(f"  [SVG] LMC clash {idx+1}/{total}: isolating {clash_r.get('p1_name','?')} ↔ {clash_r.get('p2_name','?')}")
                _isolate_pair(active_doc, root_product, fp, sp)
                img_path = _capture_and_annotate_clash(catia, active_doc, clash_r, clash_idx=idx)
                dec = _show_hitl_ui(img_path, clash_r, pub_name, "LMC", idx, total)
                print(f"  [HITL] LMC clash {idx+1}/{total} decision: {dec.upper()}")
                reviewed.append(dict(clash_r,
                                     comment=f"HITL:{dec}",
                                     keep=("Yes" if dec == "approve" else "No")))
            # All LMC clashes reviewed — now restore visibility
            _global_unhide(catia, active_doc, root_product)
            non_clash = [r for r in lmc_results if not r.get("is_clash")]
            lmc_db_rows = reviewed + non_clash
        _cleanup_clash(lmc_clashes_col, lmc_new_clash)
        cleanup_pairs[-1][1] = None

        if not has_lmc_clash:
            time.sleep(0.5)

    finally:
        # Safety net: remove any clash objects not yet deleted
        # (e.g. if the user hit Ctrl+C during input())
        for col, obj in cleanup_pairs:
            if obj is not None:
                _cleanup_clash(col, obj)

        # Safety net: always restore full visibility (in case an exception
        # fired while parts were isolated for HITL inspection)
        try:
            _global_unhide(catia, active_doc, root_product)
        except Exception:
            pass

        # ── GUARANTEED REVERSION TO NOMINAL ──────────────────────────────
        print(f"\n  ↩  Reverting '{pub_name}' to nominal {nominal} mm ...")
        try:
            param.Value = nominal
            root_product.Update()
            time.sleep(1.0)
            print(f"  ✅ Reverted to nominal successfully.")
        except Exception as revert_err:
            print(f"  ❌ REVERT FAILED: {revert_err}")
            print(f"     *** MANUAL RESET REQUIRED: set '{pub_name}' = {nominal} mm ***")

    return upper_summary, lower_summary, mmc_db_rows, lmc_db_rows


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def run_agent_3_actuation():
    print("\n" + "=" * 60)
    print("=== AGENT 3: CAD ACTUATION & DMU CLASH VALIDATION ===")
    print("=" * 60)

    # ── Connect to CATIA first — need active doc name for assembly filter ─
    try:
        catia           = win32com.client.Dispatch("CATIA.Application")
        active_doc      = catia.ActiveDocument
        root_product    = active_doc.Product
        active_doc_name = active_doc.Name
        print(f"Connected to CATIA. Active document: {active_doc_name}\n")
    except Exception as e:
        print(f"Cannot connect to CATIA: {e}")
        return

    # ── Resolve assembly_id from active document name ─────────────────────
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute(
            'SELECT assembly_id, assembly_name FROM catia_assemblies WHERE assembly_name = ?',
            (active_doc_name,)
        )
        asm_row = cursor.fetchone()
    except Exception as e:
        print(f"DB assembly lookup failed: {e}")
        conn.close()
        return

    if asm_row:
        target_assembly_id   = asm_row[0]
        target_assembly_name = asm_row[1]
        print(f"✅ Matched active document → assembly_id={target_assembly_id} "
              f"('{target_assembly_name}')\n")
    else:
        # ── Fallback: list assemblies that have mappings and let user choose
        print(f"⚠  No exact assembly match found for '{active_doc_name}'.")
        print("   Assemblies with mappings in the database:\n")
        try:
            cursor.execute('''
                SELECT DISTINCT ca.assembly_id, ca.assembly_name
                FROM   human_mapping m
                JOIN   catia_assemblies ca ON ca.assembly_id = m.assembly_id
                ORDER  BY ca.assembly_id
            ''')
            available = cursor.fetchall()
        except Exception:
            available = []

        if not available:
            print("   No mappings exist yet.  Run the UI Bridge first.")
            conn.close()
            return

        for a_id, a_name in available:
            print(f"   [{a_id}]  {a_name}")

        try:
            if _ASSEMBLY_CHOICE is not None:
                # Injected by master_app — no terminal prompt needed
                chosen = str(_ASSEMBLY_CHOICE)
                print(f"\n   [Master UI] Assembly pre-selected: {chosen}")
            else:
                chosen = input("\n   Enter assembly ID to run (or ENTER to cancel): ").strip()
        except EOFError:
            chosen = ""

        if not chosen:
            print("Cancelled.")
            conn.close()
            return

        try:
            target_assembly_id = int(chosen)
        except ValueError:
            print("Invalid ID. Aborting.")
            conn.close()
            return

        target_assembly_name = next(
            (a[1] for a in available if a[0] == target_assembly_id), "Unknown"
        )
        print(f"\n▶  Running for assembly_id={target_assembly_id} "
              f"('{target_assembly_name}')\n")

    # ── Load mappings filtered to the resolved assembly ───────────────────
    try:
        cursor.execute('''
            SELECT
                cp.publication_name,
                cp.part_path,
                st.feature_name,
                st.nominal_value,
                st.upper_tolerance,
                st.lower_tolerance
            FROM  human_mapping       m
            JOIN  semantic_tolerances st ON st.id  = m.vlm_id
            JOIN  catia_parameters    cp ON cp.id  = m.cad_id
            WHERE m.assembly_id = ?
        ''', (target_assembly_id,))
        mappings = cursor.fetchall()
    except Exception as e:
        print(f"DB query failed: {e}")
        conn.close()
        return
    conn.close()

    if not mappings:
        print(f"No mappings found for assembly_id={target_assembly_id}. "
              "Run the UI Bridge first.")
        return

    print(f"Found {len(mappings)} mapping(s) to actuate for '{target_assembly_name}'.\n")

    results = []

    # ── Process each mapping ──────────────────────────────────────────────
    for pub_name, part_path, feature_name, nominal, upper_tol, lower_tol in mappings:
        # Re-acquire root_product each iteration: a COM Update() on a prior
        # parameter can leave the old reference stale in win32com.
        try:
            root_product = active_doc.Product
        except Exception as refresh_err:
            print(f"  ⚠  Could not refresh root_product: {refresh_err}")

        print("\n" + "─" * 60)
        print(f"Actuating: '{pub_name}'")
        print(f"  2D feature  : {feature_name}")
        print(f"  Part path   : {part_path}")
        print(f"  Nominal={nominal} mm   MMC={nominal + upper_tol} mm   LMC={nominal + lower_tol} mm")

        try:
            upper_summary, lower_summary, mmc_rows, lmc_rows = _actuate_parameter(
                catia, active_doc, root_product,
                pub_name, part_path, feature_name, nominal, upper_tol, lower_tol
            )
        except Exception as act_err:
            print(f"  ❌ Actuation error for '{pub_name}': {act_err}")
            traceback.print_exc()
            upper_summary = f"ERROR: {act_err}"
            lower_summary = f"ERROR: {act_err}"
            mmc_rows, lmc_rows = [], []

        results.append((pub_name, upper_summary, lower_summary, mmc_rows, lmc_rows))

    print("\n\n" + "=" * 60)
    print("=== AGENT 3 SUMMARY — DMU CLASH RESULTS ===")
    print(f"{'Publication':<28}  {'MMC Result':<30}  {'LMC Result'}")
    print("─" * 95)
    for pub, u, l, _mr, _lr in results:
        u_icon = "❌" if "FAIL" in u else ("⚠" if u in ("SKIPPED", "NOT FOUND", "SPA_UNAVAILABLE") else "✅")
        l_icon = "❌" if "FAIL" in l else ("⚠" if l in ("SKIPPED", "NOT FOUND", "SPA_UNAVAILABLE") else "✅")
        print(f"{pub:<28}  {u_icon} {u:<28}  {l_icon} {l}")

    # ── Persist per-conflict rows to DB (CATIA report schema) ──────────────
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()

        # Drop and recreate so schema is always current
        cur.execute('DROP TABLE IF EXISTS actuation_results')
        cur.execute('''
            CREATE TABLE actuation_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                project_tag      TEXT,
                assembly_name    TEXT,
                publication_name TEXT,
                boundary         TEXT,
                product1         TEXT,
                product2         TEXT,
                type             TEXT,
                value            REAL,
                status           TEXT,
                info             TEXT,
                keep             TEXT,
                comment          TEXT,
                location         TEXT,
                run_at           TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')

        # Resolve project_tag from the drawing linked to this assembly's mappings
        try:
            cur.execute('''
                SELECT COALESCE(NULLIF(d.project_tag,''), d.file_name)
                FROM   drawings d
                JOIN   semantic_tolerances st ON st.drawing_id = d.drawing_id
                JOIN   human_mapping m ON m.vlm_id = st.id
                WHERE  m.assembly_id = ?
                LIMIT  1
            ''', (target_assembly_id,))
            tag_row = cur.fetchone()
            run_project_tag = tag_row[0] if tag_row else target_assembly_name
        except Exception:
            run_project_tag = target_assembly_name

        row_count = 0
        for pub, u_sum, l_sum, mmc_rows, lmc_rows in results:
            for boundary, rows in (("MMC", mmc_rows), ("LMC", lmc_rows)):
                for r in rows:
                    cur.execute(
                        '''
                        INSERT INTO actuation_results
                            (project_tag, assembly_name, publication_name, boundary,
                             product1, product2, type, value, status, info,
                             keep, comment, location)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''',
                        (
                            run_project_tag,
                            target_assembly_name,
                            pub,
                            boundary,
                            r.get("p1_name", "?"),
                            r.get("p2_name", "?"),
                            r.get("type", "Unknown"),
                            r.get("clearance", 0.0),
                            r.get("status", "Computed"),
                            "Agent Validation",
                            r.get("keep", "Yes"),
                            r.get("comment", "Automated MMC/LMC check"),
                            r.get("location", "N/A"),
                        )
                    )
                    row_count += 1

        conn.commit()
        conn.close()
        print(f"\n✅ {row_count} conflict row(s) saved to actuation_results table.")
    except Exception as e:
        print(f"\n⚠  Could not save results to DB: {e}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_agent_3_actuation()

