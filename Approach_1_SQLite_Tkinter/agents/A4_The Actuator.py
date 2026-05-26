import sqlite3
import sys
import traceback
import pythoncom
import win32com.client
import time
import os
import re
import datetime
import tempfile
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageTk, ImageFont, ImageFilter

# Persistent path: next to the .exe when frozen, otherwise project root.
if getattr(sys, 'frozen', False):
    DB_PATH = os.path.join(os.path.dirname(sys.executable), 'eats_validation.db')
else:
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
            # We store both a display string AND raw float tuple (clash_coords)
            # so the camera can aim precisely at the clash zone.
            # Strategy: three escalating attempts, most reliable first.
            location     = "N/A"
            clash_coords = None   # (x, y, z) float tuple — used for camera aim

            # Force COM dispatch on the conflict object for better method access.
            try:
                res_d = win32com.client.Dispatch(res)
            except Exception:
                res_d = res

            # Attempt 1: pycatia Conflict wrapper (Sajadh’s exact method — most reliable)
            # pycatia exposes get_first_point_coordinates() / get_second_point_coordinates()
            # which handle the COM OUT-parameter calling convention correctly.
            try:
                from pycatia.space_analyses_interfaces.conflict import Conflict as _CatConflict
                _wrapped = _CatConflict(getattr(res, "com_object", res))
                fp_raw = tuple(float(v) for v in _wrapped.get_first_point_coordinates())
                sp_raw = tuple(float(v) for v in _wrapped.get_second_point_coordinates())
                clash_coords = tuple((fp_raw[i] + sp_raw[i]) / 2.0 for i in range(3))
                location = f"({clash_coords[0]:.3f}, {clash_coords[1]:.3f}, {clash_coords[2]:.3f})"
                print(f"    [DMU] clash_coords via pycatia Conflict: {clash_coords}")
            except Exception:
                pass

            # Attempt 2: dispatched COM attributes / method calls
            if clash_coords is None:
                try:
                    res_d = win32com.client.Dispatch(res)
                except Exception:
                    res_d = res
                for method_pair in (("GetFirstPoint", "GetSecondPoint"),
                                    ("FirstPoint",    "SecondPoint"),
                                    ("first_point",   "second_point")):
                    try:
                        _a = getattr(res_d, method_pair[0])
                        _b = getattr(res_d, method_pair[1])
                        fp_raw = tuple(float(v) for v in (_a() if callable(_a) else _a))
                        sp_raw = tuple(float(v) for v in (_b() if callable(_b) else _b))
                        clash_coords = tuple((fp_raw[i] + sp_raw[i]) / 2.0 for i in range(3))
                        location = f"({clash_coords[0]:.3f}, {clash_coords[1]:.3f}, {clash_coords[2]:.3f})"
                        print(f"    [DMU] clash_coords from {method_pair[0]}: {clash_coords}")
                        break
                    except Exception:
                        continue

            if clash_coords is None:
                print(f"    [DMU] ⚠  No 3D clash coordinates for conflict {r} — "
                      f"marker will use highlight scan.")

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
                "clash_coords"  : clash_coords,   # raw (x,y,z) or None
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
# VECTOR MATH HELPERS  (ported from Sajadh’s layer3_tolerance_dmu_agent.py)
# ──────────────────────────────────────────────────────────────────────────────
import math as _math

def _vsub(a, b):  return tuple(a[i]-b[i] for i in range(3))
def _vadd(a, b):  return tuple(a[i]+b[i] for i in range(3))
def _vscale(v, s): return tuple(c*s for c in v)
def _vdot(a, b):  return sum(a[i]*b[i] for i in range(3))
def _vcross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _vnorm(v):
    m = _math.sqrt(_vdot(v, v))
    return tuple(c/m for c in v) if m > 1e-12 else None
def _vrot(v, axis, deg):
    ax = _vnorm(axis)
    if ax is None: return v
    r = _math.radians(deg)
    c, s = _math.cos(r), _math.sin(r)
    d = _vdot(ax, v)
    return _vadd(_vadd(_vscale(v, c), _vscale(_vcross(ax, v), s)), _vscale(ax, d*(1-c)))

# ──────────────────────────────────────────────────────────────────────────────
# VIEWPOINT HELPERS  (uses pycatia ViewPoint3D — ported from Sajadh)
# ──────────────────────────────────────────────────────────────────────────────
def _vp_get(viewer):
    """Return a pycatia ViewPoint3D wrapping viewer’s Viewpoint3D, or None."""
    try:
        from pycatia.in_interfaces.viewpoint_3d import ViewPoint3D as _VP3
        raw = viewer.Viewpoint3D
        return _VP3(raw)
    except Exception:
        return None

def _vp_save(viewer):
    """Capture full viewpoint state as a plain dict.  Returns None on failure."""
    vp = _vp_get(viewer)
    if vp is None:
        return None
    try:
        return {
            "origin":         tuple(vp.get_origin()),
            "sight":          tuple(vp.get_sight_direction()),
            "up":             tuple(vp.get_up_direction()),
            "focus_distance": float(vp.focus_distance),
            "projection_mode": int(vp.projection_mode),
            "zoom":           float(getattr(vp, 'zoom',        1.0) or 1.0),
            "field_of_view":  float(getattr(vp, 'field_of_view', 0.0) or 0.0),
        }
    except Exception as e:
        print(f"    [CAM] _vp_save failed: {e}")
        return None

def _vp_restore(viewer, state):
    """Restore viewpoint from a dict saved by _vp_save."""
    if not state:
        return
    vp = _vp_get(viewer)
    if vp is None:
        return
    try:
        vp.put_origin(state["origin"])
        vp.put_sight_direction(state["sight"])
        vp.put_up_direction(state["up"])
        vp.focus_distance = state["focus_distance"]
        pm = state["projection_mode"]
        if pm == 0:   # conic/perspective
            try: vp.field_of_view = state["field_of_view"]
            except Exception: pass
        else:         # cylindric/parallel
            try: vp.zoom = state["zoom"]
            except Exception: pass
    except Exception as e:
        print(f"    [CAM] _vp_restore failed: {e}")

def _project_world_to_image(world_point, view_state, image_size):
    """
    Project a 3D world point to 2D pixel coordinates within an image of
    image_size=(width, height), using the camera state from _vp_save().
    Ported verbatim from Sajadh's project_world_point_to_image().
    Returns (px, py) floats or None on failure.
    """
    if world_point is None or view_state is None:
        return None
    width, height = image_size
    if width <= 0 or height <= 0:
        return None
    sight = _vnorm(view_state.get("sight"))
    up    = _vnorm(view_state.get("up"))
    if sight is None or up is None:
        return None
    right = _vnorm(_vcross(sight, up))
    if right is None:
        return None
    relative = _vsub(world_point, view_state.get("origin"))
    x_cam = _vdot(relative, right)
    y_cam = _vdot(relative, up)
    z_cam = _vdot(relative, sight)
    if z_cam <= 1e-6:
        return None
    aspect = float(width) / float(max(1, height))
    pm = int(view_state.get("projection_mode", 0))
    if pm == 0:  # conic / perspective
        fov   = float(view_state.get("field_of_view", 0.0) or 0.0)
        tan_v = _math.tan(_math.radians(fov))
        tan_h = tan_v * aspect
        if abs(tan_v) <= 1e-9 or abs(tan_h) <= 1e-9:
            return None
        x_norm = 0.5 + (x_cam / (z_cam * tan_h)) * 0.5
        y_norm = 0.5 - (y_cam / (z_cam * tan_v)) * 0.5
    else:  # cylindric / parallel / orthographic
        zoom  = float(view_state.get("zoom", 1.0) or 1.0)
        fd    = float(view_state.get("focus_distance", 1.0) or 1.0)
        half_v = fd / max(zoom, 1e-9)
        half_h = half_v * aspect
        if half_v <= 1e-9 or half_h <= 1e-9:
            return None
        x_norm = 0.5 + (x_cam / half_h) * 0.5
        y_norm = 0.5 - (y_cam / half_v) * 0.5
    if not (_math.isfinite(x_norm) and _math.isfinite(y_norm)):
        return None
    return (x_norm * width, y_norm * height)


def _vp_focus(viewer, midpoint):
    """
    Move the camera origin so that *midpoint* appears at the centre of the
    screen.  Also zooms in (tightens field-of-view or increases zoom factor)
    so the clash fills roughly half the viewport.
    Ported from Sajadh’s focus_viewpoint_on_record().
    """
    if midpoint is None:
        return False
    vp = _vp_get(viewer)
    if vp is None:
        return False
    try:
        sight = _vnorm(tuple(vp.get_sight_direction()))
        if sight is None:
            return False
        fd = float(vp.focus_distance)
        # Origin = midpoint - sight * focus_distance  =>  midpoint is at screen centre
        vp.put_origin(_vsub(midpoint, _vscale(sight, fd)))
        pm = int(vp.projection_mode)
        if pm == 0:   # conic / perspective
            try: vp.field_of_view = max(2.0, float(vp.field_of_view) * 0.55)
            except Exception: pass
        else:         # cylindric / parallel
            try:
                z = float(vp.zoom)
                vp.zoom = max(z * 4.0, z + 0.002)
            except Exception: pass
        return True
    except Exception as e:
        print(f"    [CAM] _vp_focus failed: {e}")
        return False

def _vp_orbit(viewer, base_state, midpoint, yaw_deg, pitch_deg):
    """
    Rotate the camera around *midpoint* by yaw (around up) then pitch
    (around right), keeping midpoint at the screen centre.
    Ported from Sajadh’s apply_view_variant().
    Returns True on success.
    """
    vp = _vp_get(viewer)
    if vp is None:
        return False
    try:
        sight = _vnorm(base_state["sight"])
        up    = _vnorm(base_state["up"])
        if sight is None or up is None:
            return False
        right = _vnorm(_vcross(sight, up))
        if yaw_deg:
            sight = _vnorm(_vrot(sight, up,    yaw_deg)) or sight
            right = _vnorm(_vcross(sight, up))  or right
        if pitch_deg:
            sight = _vnorm(_vrot(sight, right,  pitch_deg)) or sight
            up    = _vnorm(_vrot(up,    right,  pitch_deg)) or up
        sight = _vnorm(sight) or base_state["sight"]
        up    = _vnorm(up)    or base_state["up"]
        vp.put_sight_direction(sight)
        vp.put_up_direction(up)
        fd = base_state["focus_distance"]
        origin = _vsub(midpoint, _vscale(sight, fd)) if midpoint is not None else base_state["origin"]
        vp.put_origin(origin)
        vp.focus_distance = fd
        pm = base_state["projection_mode"]
        if pm == 0:
            try: vp.field_of_view = base_state["field_of_view"]
            except Exception: pass
        else:
            try: vp.zoom = base_state["zoom"]
            except Exception: pass
        return True
    except Exception as e:
        print(f"    [CAM] _vp_orbit failed: {e}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# GEOMETRY FINDER  -- locates where the actual 3D parts are in the screenshot
def _find_geometry_center(pil_image):
    """
    Find the centroid (cx, cy) of 3D geometry pixels in a CATIA screenshot.
    1. Scan for clash-highlight colours (red/orange/yellow) first.
    2. Fallback: centroid of all non-background, non-white pixels.
    Background grey approx (147,147,147), tolerance 133-161.
    Returns ((cx, cy), source) or (None, None).
    """
    try:
        img = pil_image.convert("RGB")
        w, h = img.size
        pix  = img.load()
        sl = int(w * 0.18); sr = w
        st = int(h * 0.06); sb = int(h * 0.92)
        # Pass 1: highlight colours
        hx_sum = hy_sum = h_count = 0
        for y in range(st, sb):
            for x in range(sl, sr):
                R, G, B = pix[x, y][:3]
                is_red    = R >= 180 and G <= 90  and B <= 90  and R >= G + 60 and R >= B + 60
                is_orange = (R >= 140 and G >= 70  and B <= 120 and R >= B + 50
                             and not (abs(R-G) < 15 and abs(R-B) < 15))
                is_yellow = (R >= 170 and G >= 130 and B <= 140
                             and not (abs(R-G) < 15 and abs(R-B) < 15))
                if is_red or is_orange or is_yellow:
                    hx_sum += x; hy_sum += y; h_count += 1
        if h_count >= 10:
            cx = hx_sum // h_count; cy = hy_sum // h_count
            print(f"    [GEO] Highlight centre ({h_count} px): ({cx},{cy}).")
            return (cx, cy), "highlight"
        # Pass 2: geometry centroid (non-background, non-white pixels)
        BG_LO, BG_HI = 133, 161
        gx_sum = gy_sum = g_count = 0
        for y in range(st, sb, 2):
            for x in range(sl, sr, 2):
                R, G, B = pix[x, y][:3]
                is_bg    = BG_LO <= R <= BG_HI and BG_LO <= G <= BG_HI and BG_LO <= B <= BG_HI
                is_white = R >= 230 and G >= 230 and B >= 230
                if not is_bg and not is_white:
                    gx_sum += x; gy_sum += y; g_count += 1
        if g_count >= 50:
            cx = gx_sum // g_count; cy = gy_sum // g_count
            print(f"    [GEO] Geometry centroid ({g_count} sampled px): ({cx},{cy}).")
            return (cx, cy), "geometry"
        print(f"    [GEO] No geometry found in {w}x{h} -- viewport-centre fallback.")
        return None, None
    except Exception as e:
        print(f"    [GEO] Scanner error: {e}")
        return None, None


def _generate_delta_view(mmc_iso_path, lmc_iso_path, out_path, pub_name, upper_limit, lower_limit):
    """
    #8 MMC/LMC Delta View — side-by-side comparison of the two isometric clash
    captures.  Saved to *out_path* so the engineer can immediately see whether
    the clash worsens at max or min material condition.
    """
    try:
        CELL_W, CELL_H = 720, 540
        BORDER         = 6
        canvas = Image.new("RGB", (CELL_W * 2 + BORDER, CELL_H + 80), (22, 24, 34))
        try:
            f_h    = ImageFont.truetype("arialbd.ttf", 16)
            f_sub  = ImageFont.truetype("arial.ttf",   13)
        except Exception:
            f_h = f_sub = ImageFont.load_default()

        def _paste_panel(src_path, label, x_off, limit_val):
            try:
                img = Image.open(src_path).convert("RGB")
            except Exception:
                img = Image.new("RGB", (CELL_W, CELL_H), (60, 60, 80))
            img.thumbnail((CELL_W, CELL_H), Image.Resampling.LANCZOS)
            panel = Image.new("RGB", (CELL_W, CELL_H), (30, 30, 44))
            px = (CELL_W - img.width) // 2
            py = (CELL_H - img.height) // 2
            panel.paste(img, (px, py))
            # Label strip at bottom of panel
            rgba = panel.convert("RGBA")
            ov   = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
            dr   = ImageDraw.Draw(ov)
            dr.rounded_rectangle((10, CELL_H - 46, CELL_W - 10, CELL_H - 8),
                                  radius=8, fill=(18, 24, 36, 210),
                                  outline=(110, 160, 220, 200), width=2)
            dr.text((20, CELL_H - 40), label,            fill=(235, 245, 255, 255), font=f_h)
            dr.text((20, CELL_H - 20), f"{limit_val:.4f} mm", fill=(200, 215, 240, 200), font=f_sub)
            panel = Image.alpha_composite(rgba, ov).convert("RGB")
            canvas.paste(panel, (x_off, 0))

        _paste_panel(mmc_iso_path, "MMC  (Maximum Material)", 0,             upper_limit)
        _paste_panel(lmc_iso_path, "LMC  (Least Material)",   CELL_W + BORDER, lower_limit)

        # Title bar at the bottom
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, CELL_H, CELL_W * 2 + BORDER, CELL_H + 80), fill=(14, 16, 24))
        draw.text((16, CELL_H + 12), f"MMC vs LMC  —  {pub_name}",
                  fill=(200, 220, 255), font=f_h)
        draw.text((16, CELL_H + 36),
                  "Side-by-side isometric comparison of clash at both tolerance extremes.",
                  fill=(130, 150, 180), font=f_sub)

        canvas.save(out_path, "PNG")
        print(f"    [DELTA] MMC/LMC delta view saved: {out_path}")
        return out_path
    except Exception as e:
        print(f"    [DELTA] Delta view generation failed (non-fatal): {e}")
        return None


def _capture_and_annotate_clash(catia, active_doc, clash_result, clash_idx=0, save_dir=None):
    """
    4-View 2×2 Grid Montage — Isometric / Top / Front / Side.

    Pipeline
    ────────
    1. Save the user's original CATIA viewpoint so it can be restored at the end.
    2. Loop over four canonical view angles using Sajadh's yaw/pitch orbit approach
       (iso=32°/-24°, front=0°/0°, top=0°/-88°, right=-90°/0°) relative to the
       model's own front orientation — NOT hardcoded world-space vectors.
         • _vp_orbit()   → rotate to the canonical angle from the saved base state
         • viewer.Reframe() → fit the isolated clash pair in frame
         • _vp_focus()   → re-centre and zoom in on the clash midpoint
         • CaptureToFile → PNG → JPEG → BMP (first that Pillow can open)
    3. Open the 4 bitmaps in Pillow, crop the left 20 % (spec tree) off every one,
       resize each to a common cell size, and stitch into a 2×2 canvas.
    4. Overlay:
         • Semi-transparent dark label band on every quadrant  (view name)
         • One master Clash HUD badge (type, penetration, part names) in the
           top-right corner of the complete montage.
    5. Save the final .png, delete the 4 temp .bmp files, return the .png path.
    6. On any fatal error fall back to _make_fallback_image().
    """
    tmp_dir  = tempfile.gettempdir()
    # #6 Permanent save: write montage + individual views to save_dir when provided.
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        out_path = os.path.join(save_dir, f'clash_{clash_idx:03d}_montage.png')
    else:
        out_path = os.path.join(tmp_dir, f'_catia_clash_montage_{clash_idx}.png')

    # Four canonical views — (slug, yaw_deg, pitch_deg).
    # Yaw / pitch are relative to the model's own front orientation, exactly
    # as Sajadh does in layer3_tolerance_dmu_agent.py VIEW_SPECS.
    # The base_state is captured via _vp_save() after the first Reframe(); each
    # view is then produced by _vp_orbit(viewer, base_state, midpoint, yaw, pitch).
    VIEW_SPECS = [
        ("iso",   "ISOMETRIC VIEW",  32.0, -24.0),
        ("front", "FRONT VIEW",       0.0,   0.0),
        ("top",   "TOP VIEW",         0.0, -88.0),
        ("right", "RIGHT VIEW",     -90.0,   0.0),
    ]

    # ── Access viewer ─────────────────────────────────────────────────────────
    try:
        import win32com.client.dynamic as _windyn
        _raw_viewer = catia.ActiveWindow.ActiveViewer
        viewer      = _windyn.DumbDispatch(_raw_viewer)
    except Exception as e:
        print(f"    [CAM] Cannot access viewer: {e}")
        return _make_fallback_image(clash_result, out_path)

    fp     = clash_result.get("first_product")
    sp     = clash_result.get("second_product")
    coords = _parse_location(clash_result.get("location", "N/A"))

    # ── Set CATIA viewer background to white before captures ─────────────────
    saved_bg_color = None
    try:
        saved_bg_color = viewer.BackgroundColor
        viewer.BackgroundColor = 16777215  # 0xFFFFFF = white
        viewer.Update()
        print("    [CAM] Background set to white.")
    except Exception as e:
        print(f"    [CAM] Could not set background color (non-fatal): {e}")

    # ── Isolate the clashing pair so the scene is clean ───────────────────────
    try:
        root_product = active_doc.Product
        _isolate_pair(active_doc, root_product, fp, sp)
    except Exception as e:
        print(f"    [CAM] _isolate_pair failed (non-fatal): {e}")

    # ── Step 1: Add fp/sp to CATIA Selection before Reframe ─────────────────
    # Sajadh does this in capture_conflict_views() — select_products_for_capture()
    # adds the two products to Selection so that viewer.Reframe() fits exactly
    # those two objects (tight bounding box), not the whole assembly.
    try:
        sel = active_doc.Selection
        sel.Clear()
        if fp is not None:
            try: sel.Add(fp)
            except Exception: pass
        if sp is not None:
            try: sel.Add(sp)
            except Exception: pass
    except Exception:
        pass

    # ── Step 2: Reframe to fit the isolated pair ──────────────────────────────
    try:
        viewer.Reframe()
        viewer.Update()
        time.sleep(0.2)
        print("    [CAM] Reframe after isolate: OK.")
    except Exception as e:
        print(f"    [CAM] Reframe after isolate failed (non-fatal): {e}")

    # ── Step 3: Zoom in tight on the clash midpoint ───────────────────────────
    # This is Sajadh's focus_viewpoint_on_record() — slides origin to midpoint
    # AND tightens field-of-view (*0.55) or zoom (*4.0) so the clash fills the
    # screen.  We do this BEFORE saving base_state so that every orbit view
    # inherits the tight zoom level.
    clash_coords = clash_result.get("clash_coords")
    midpoint     = tuple(clash_coords) if clash_coords else None
    if midpoint is not None:
        if _vp_focus(viewer, midpoint):
            print(f"    [CAM] Tight zoom on clash midpoint {midpoint}: OK.")
            viewer.Update()
            time.sleep(0.15)
        else:
            print("    [CAM] _vp_focus failed (non-fatal) — base state will be at Reframe zoom.")

    # ── Step 4: Save base viewpoint from the TIGHT focused position ───────────
    # Sajadh saves base_record_view_state AFTER focus_viewpoint_on_record().
    # All four orbit views inherit this tight zoom, so every captured image
    # shows the clash region filling the frame — not a distant overview.
    base_state = _vp_save(viewer)
    if base_state:
        print("    [CAM] Base viewpoint (tight on clash) saved via _vp_save().")
    else:
        print("    [CAM] Could not save base viewpoint (non-fatal).")

    # ── Helper: orbit to one view and capture a screenshot ───────────────────
    def _take_bmp(slug, view_label, yaw_deg, pitch_deg, idx):
        """Orbit from the already-tight base_state (Sajadh's exact technique).

        No per-view Reframe or focus needed — base_state is already centred and
        zoomed in on the clash.  Just orbit to the desired angle and capture.
        """
        FORMAT_ATTEMPTS = [(3, '.png'), (1, '.jpg'), (0, '.bmp')]

        try:
            # Orbit camera to the desired yaw/pitch angle from the tight base.
            # apply_view_variant / _vp_orbit keeps midpoint at screen centre
            # throughout the rotation — every view stays locked on the clash.
            if base_state is not None:
                ok = _vp_orbit(viewer, base_state, midpoint, yaw_deg, pitch_deg)
                if not ok:
                    print(f"    [CAM] _vp_orbit failed for {view_label} (non-fatal).")
            else:
                print(f"    [CAM] No base_state — skipping orbit for {view_label}.")

            # #7 Context zoom: iso stays tight (100%); other views back off slightly
            # so the engineer can see WHERE on the part the clash zone sits, not
            # just the sub-mm contact spot.  We widen the field-of-view / reduce
            # zoom by 35% for the three orthographic views after the orbit.
            if view_label != "ISOMETRIC VIEW" and midpoint is not None:
                vp_ctx = _vp_get(viewer)
                if vp_ctx is not None:
                    try:
                        pm = int(vp_ctx.projection_mode)
                        if pm == 0:   # conic/perspective
                            vp_ctx.field_of_view = min(60.0, float(vp_ctx.field_of_view) / 0.65)
                        else:         # cylindric/parallel
                            vp_ctx.zoom = max(0.0001, float(vp_ctx.zoom) * 0.65)
                    except Exception:
                        pass

            viewer.Update()
            time.sleep(0.5)

            # Capture the viewpoint state at this exact camera position so the
            # montage builder can project the 3D clash point to 2D pixels.
            capture_view_state = _vp_save(viewer)

            # Capture — try PNG → JPEG → BMP until Pillow can open the result.
            for fmt_id, ext in FORMAT_ATTEMPTS:
                cap_path = os.path.join(tmp_dir, f'_catia_v4_{slug}_{idx}{ext}')
                try:
                    viewer.CaptureToFile(fmt_id, cap_path)
                    time.sleep(0.8)
                    if os.path.isfile(cap_path) and os.path.getsize(cap_path) > 2000:
                        from PIL import Image as _PIL_Image
                        try:
                            with _PIL_Image.open(cap_path) as _probe:
                                _probe.verify()
                            print(f"    [CAM] Captured {view_label} (fmt={fmt_id}): {cap_path}")
                            return cap_path, True, capture_view_state
                        except Exception:
                            pass
                except Exception:
                    pass

            print(f"    [CAM] All format attempts failed for {view_label}.")
            return os.path.join(tmp_dir, f'_catia_v4_{slug}_{idx}.bmp'), False, capture_view_state

        except Exception as e:
            print(f"    [CAM] Capture failed for {view_label}: {e}")
            return os.path.join(tmp_dir, f'_catia_v4_{slug}_{idx}.bmp'), False, None

    # ── Shoot all 4 views ─────────────────────────────────────────────────────
    captured_paths = []   # list of (view_label, path, ok, view_state)
    for slug, view_label, yaw_deg, pitch_deg in VIEW_SPECS:
        path, ok, view_state = _take_bmp(slug, view_label, yaw_deg, pitch_deg, clash_idx)
        captured_paths.append((view_label, path, ok, view_state))

    # Clear CATIA selection (was set to fp/sp before Reframe).
    try:
        active_doc.Selection.Clear()
    except Exception:
        pass

    # ── Restore original viewpoint via _vp_restore() ─────────────────────────
    if base_state is not None:
        _vp_restore(viewer, base_state)
        try:
            viewer.Update()
        except Exception:
            pass
        print("    [CAM] Original viewpoint restored via _vp_restore().")

    # ── Restore original background color ─────────────────────────────────────
    if saved_bg_color is not None:
        try:
            viewer.BackgroundColor = saved_bg_color
            viewer.Update()
        except Exception:
            pass

    # ── Check we have at least one usable capture ─────────────────────────────
    any_ok = any(ok for _, _, ok, _ in captured_paths)
    if not any_ok:
        print("    [CAM] All 4 captures failed — generating fallback image.")
        return _make_fallback_image(clash_result, out_path)

    # ── Pillow: smart crop + annotate + assemble 2×2 montage ─────────────────
    try:
        CELL_W   = 640    # width of each quadrant in the final montage
        CELL_H   = 480    # height of each quadrant
        BORDER   = 4      # gap between cells
        CANVAS_W = CELL_W * 2 + BORDER
        CANVAS_H = CELL_H * 2 + BORDER

        try:
            f_label = ImageFont.truetype("arialbd.ttf", 15)
            f_info  = ImageFont.truetype("arial.ttf",   13)
            f_small = ImageFont.truetype("arial.ttf",   11)
        except Exception:
            f_label = f_info = f_small = ImageFont.load_default()

        ctype   = clash_result.get("type", "Clash")
        dist    = clash_result.get("clearance", 0.0)
        p1_name = clash_result.get("p1_name", "?")
        p2_name = clash_result.get("p2_name", "?")
        marker_color_rgb = (220, 50, 47) if ctype == "Clash" else (203, 75, 22) if ctype == "Contact" else (38, 139, 210)

        VIEW_DISPLAY = {
            "ISOMETRIC VIEW": "View 1 – Isometric",
            "FRONT VIEW":     "View 2 – Front",
            "TOP VIEW":       "View 3 – Top",
            "RIGHT VIEW":     "View 4 – Right",
        }

        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (30, 30, 40))

        offsets = [
            (0,               0),
            (CELL_W + BORDER, 0),
            (0,               CELL_H + BORDER),
            (CELL_W + BORDER, CELL_H + BORDER),
        ]

        def _smart_crop(img):
            """
            Strip CATIA UI chrome to extract the pure 3D viewport region.
            CaptureToFile captures the whole CATIA window:
              - Spec-tree panel: left ~18% of width
              - Top toolbar:     top  ~6% of height
              - Bottom toolbar:  bottom ~8% of height
            The 3D viewport occupies the remaining area.  Its centre is
            where _vp_focus() aimed the camera = the clash midpoint.

            Returns (cropped_image, (left, top, right, bottom)).
            """
            w, h = img.size
            left   = int(w * 0.18)
            top    = int(h * 0.06)
            right  = w
            bottom = int(h * 0.92)
            cropped = img.crop((left, top, right, bottom))
            cw, ch = cropped.size
            if cw > 1200 or ch > 900:
                cropped.thumbnail((1200, 900), Image.Resampling.LANCZOS)
            return cropped, (left, top, right, bottom)

        def _annotate_cell(cell_img, view_label, marker_pos, show_clash_info=True):
            """Overlay Sajadh-style badges on a CELL image (RGBA compositing).
            • Top-left rounded-rectangle: view name
            • Top-left below: clash type + dist + part names info box
            • At marker_pos: ring circle + callout line to badge
            """
            img_rgba  = cell_img.convert("RGBA")
            overlay   = Image.new("RGBA", img_rgba.size, (0, 0, 0, 0))
            draw      = ImageDraw.Draw(overlay)
            mc        = marker_color_rgb + (255,)   # fully opaque tuple
            mc_fill   = marker_color_rgb + (230,)

            # View name badge (top-left)
            display_name = VIEW_DISPLAY.get(view_label, view_label)
            vbox = (12, 12, 230, 48)
            draw.rounded_rectangle(vbox, radius=8, fill=(18, 24, 36, 215), outline=(110, 160, 220, 255), width=2)
            draw.text((vbox[0] + 10, vbox[1] + 8), display_name, fill=(235, 245, 255, 255), font=f_label)

            # Clash info box (below view badge) — isometric only
            if show_clash_info:
                ibox = (12, 58, 380, 116)
                draw.rounded_rectangle(ibox, radius=8, fill=(18, 24, 36, 210), outline=mc_fill, width=2)
                draw.text((ibox[0] + 10, ibox[1] + 8),  f"{ctype.upper()}  {dist:+.4f} mm",
                          fill=(255, 245, 210, 255), font=f_info)
                label_pair = f"{p1_name[:22]}  ↔  {p2_name[:22]}"
                draw.text((ibox[0] + 10, ibox[1] + 30), label_pair, fill=(210, 220, 235, 255), font=f_small)

            # Clash zone ring + callout
            if marker_pos is not None:
                cx, cy = int(marker_pos[0]), int(marker_pos[1])
                r = max(18, min(40, int(min(cell_img.width, cell_img.height) * 0.05)))
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=mc_fill, width=5)
                # Callout box
                cb_w, cb_h = 200, 40
                if cx < cell_img.width * 0.58:
                    cb_left = min(cell_img.width - cb_w - 12, cx + r + 14)
                    anchor_x = cb_left
                else:
                    cb_left = max(12, cx - cb_w - r - 14)
                    anchor_x = cb_left + cb_w
                cb_top = max(124, min(cell_img.height - cb_h - 12, cy - cb_h // 2))
                cb_box = (cb_left, cb_top, cb_left + cb_w, cb_top + cb_h)
                draw.rounded_rectangle(cb_box, radius=8, fill=(18, 24, 36, 210), outline=mc_fill, width=2)
                draw.text((cb_box[0] + 10, cb_box[1] + 12),
                          f"{ctype.capitalize()} area", fill=(255, 245, 210, 255), font=f_small)
                draw.line((cx, cy, anchor_x, cb_top + cb_h // 2), fill=mc_fill, width=3)

            composed = Image.alpha_composite(img_rgba, overlay).convert("RGB")
            return composed

        for idx, (view_label, bmp_path, ok, cap_view_state) in enumerate(captured_paths):
            off_x, off_y = offsets[idx]
            CELL_BG = (40, 40, 55)

            marker_pos = None
            if ok and os.path.isfile(bmp_path):
                try:
                    raw = Image.open(bmp_path).convert("RGB")
                    # #2 UnsharpMask: crisp up geometry edges (Sajadh's technique).
                    raw = raw.filter(ImageFilter.UnsharpMask(radius=1.8, percent=140, threshold=2))
                    raw_w, raw_h = raw.size

                    # Smart crop: returns (cropped_image, (left, top, right, bottom)).
                    cropped, (crop_left, crop_top, crop_right, crop_bottom) = _smart_crop(raw)
                    cw, ch = cropped.size

                    # Scale cropped image to fit inside the cell.
                    scale   = min(CELL_W / max(1, cw), CELL_H / max(1, ch))
                    fit_w   = max(1, int(cw * scale))
                    fit_h   = max(1, int(ch * scale))
                    resized = cropped.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
                    cell    = Image.new("RGB", (CELL_W, CELL_H), CELL_BG)
                    paste_x = (CELL_W - fit_w) // 2
                    paste_y = (CELL_H - fit_h) // 2
                    cell.paste(resized, (paste_x, paste_y))

                    # ── Marker position (Sajadh's exact approach) ────────────
                    # Project the 3D clash midpoint to raw image pixel coords,
                    # then transform into the cropped+scaled cell space.
                    orig_cw = crop_right  - crop_left
                    orig_ch = crop_bottom - crop_top
                    thumb_scale_x = cw / max(1, orig_cw)
                    thumb_scale_y = ch / max(1, orig_ch)
                    proj = _project_world_to_image(midpoint, cap_view_state, (raw_w, raw_h))
                    if proj is not None:
                        px, py = proj
                        local_x = px - crop_left
                        local_y = py - crop_top
                        if 0 <= local_x <= orig_cw and 0 <= local_y <= orig_ch:
                            cell_x = paste_x + int(local_x * thumb_scale_x * scale)
                            cell_y = paste_y + int(local_y * thumb_scale_y * scale)
                            cell_x = max(0, min(CELL_W - 1, cell_x))
                            cell_y = max(0, min(CELL_H - 1, cell_y))
                            marker_pos = (cell_x, cell_y)
                            print(f"    [MON] Marker at ({cell_x},{cell_y}) proj=({int(px)},{int(py)}) for {view_label}.")
                        else:
                            print(f"    [MON] Projected point ({int(px)},{int(py)}) outside crop for {view_label} — no marker.")
                    else:
                        print(f"    [MON] Projection failed for {view_label} — no marker.")

                    # Annotate this cell — clash info badge only on isometric (idx 0).
                    cell = _annotate_cell(cell, view_label, marker_pos, show_clash_info=(idx == 0))
                    print(f"    [MON] Annotated {view_label}.")

                    # #6 Save individual annotated view to permanent directory.
                    if save_dir:
                        slug_map = {"ISOMETRIC VIEW": "iso", "FRONT VIEW": "front",
                                    "TOP VIEW": "top", "RIGHT VIEW": "right"}
                        view_slug = slug_map.get(view_label, f"view{idx}")
                        ind_path = os.path.join(save_dir, f'clash_{clash_idx:03d}_{view_slug}.png')
                        cell.save(ind_path, "PNG")
                        print(f"    [MON] Saved {view_label} → {ind_path}")

                except Exception as pe:
                    print(f"    [MON] Could not process {bmp_path}: {pe}")
                    cell = Image.new("RGB", (CELL_W, CELL_H), CELL_BG)
            else:
                cell = Image.new("RGB", (CELL_W, CELL_H), CELL_BG)

            canvas.paste(cell, (off_x, off_y))

        # ── Save final montage ────────────────────────────────────────────────
        canvas.save(out_path, "PNG")
        print(f"    [MON] 4-view montage saved: {out_path}")

        # ── Clean up temp BMP files ───────────────────────────────────────────
        for _, bmp_path, _, _ in captured_paths:
            try:
                if os.path.exists(bmp_path):
                    os.remove(bmp_path)
            except Exception:
                pass

        return out_path

    except Exception as e:
        import traceback as _tb
        print(f"    [MON] Montage assembly failed: {e}")
        _tb.print_exc()
        return _make_fallback_image(clash_result, out_path)


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
    BG_HEADER = "#004852"
    BG_SUBHDR = "#006678"
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
             bg="#006678", fg=FG_WHITE, font=f_sm,
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

    btn_bar = tk.Frame(container, bg=BG_ROOT, pady=8)
    btn_bar.pack(fill="x", side="bottom", padx=16)

    # #4 Notes field — engineer can annotate the decision with a free-text note.
    notes_var = tk.StringVar()
    notes_row = tk.Frame(container, bg=BG_ROOT)
    notes_row.pack(fill="x", side="bottom", padx=16, pady=(0, 4))
    tk.Label(notes_row, text="Engineer note (optional):",
             bg=BG_ROOT, fg=FG_MUTED, font=f_sm, anchor="w").pack(side="left", padx=(0, 8))
    tk.Entry(notes_row, textvariable=notes_var, font=f_body,
             bg="#ffffff", fg=FG_DARK, relief="solid", bd=1,
             width=55).pack(side="left", fill="x", expand=True)

    def _decide(val):
        decision["value"] = val
        decision["note"]  = notes_var.get().strip()
        on_decide()

    btn_defs = [
        ("✔  Approve Clash",            "approve",  "#1e8449", FG_WHITE,
         "Interference is genuine and expected.  [Enter]"),
        ("✖  Reject  (False Positive)",  "reject",   "#c0392b", FG_WHITE,
         "Mark as false positive — no real interference.  [Esc]"),
        ("▲  Override",                  "override", "#d68910", FG_WHITE,
         "Accept with reservation — flag for review.  [Space]"),
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

    # #5 Keyboard shortcuts — bind after all buttons exist so focus is clear.
    def _bind_keys(widget):
        widget.bind("<Return>",  lambda _e: _decide("approve"),  add="+")
        widget.bind("<Escape>",  lambda _e: _decide("reject"),   add="+")
        widget.bind("<space>",   lambda _e: _decide("override"), add="+")
    try:
        root_win = container.winfo_toplevel()
        _bind_keys(root_win)
    except Exception:
        _bind_keys(container)

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

    # #9 Severity colour coding: scale the depth badge colour by magnitude so
    # engineers get an instant triage signal before reading the number.
    abs_mm = abs(val_mm)
    if abs_mm < 0.05:
        depth_color = "#e67e22"   # orange — very light interference
    elif abs_mm < 0.5:
        depth_color = "#e84040"   # standard red
    else:
        depth_color = "#8b0000"   # dark/deep red — significant penetration

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
             bg=BG_CARD, fg=depth_color, font=f_val, anchor="w").pack(anchor="w")

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

    # #3 Click-to-zoom: clicking the image opens it full-resolution in a scrollable Toplevel.
    def _open_zoom(_event=None):
        if _pil[0] is None:
            return
        zoom_win = tk.Toplevel(container.winfo_toplevel())
        zoom_win.title(f"Full View — {pub_name}  [{boundary}]  Conflict {clash_idx + 1}")
        zoom_win.geometry("1320x900")
        zoom_win.minsize(600, 400)
        _zf = tk.Frame(zoom_win, bg="#002d35")
        _zf.pack(fill="both", expand=True)
        _zf.columnconfigure(0, weight=1)
        _zf.rowconfigure(0, weight=1)
        _zcanvas = tk.Canvas(_zf, bg="#002d35", highlightthickness=0)
        _zcanvas.grid(row=0, column=0, sticky="nsew")
        _ys = tk.Scrollbar(_zf, orient="vertical",   command=_zcanvas.yview)
        _xs = tk.Scrollbar(_zf, orient="horizontal", command=_zcanvas.xview)
        _ys.grid(row=0, column=1, sticky="ns")
        _xs.grid(row=1, column=0, sticky="ew")
        _zcanvas.configure(yscrollcommand=_ys.set, xscrollcommand=_xs.set)
        _zref = [None]
        _zscale = [1.0]
        def _zrender():
            w = max(1, int(_pil[0].width  * _zscale[0]))
            h = max(1, int(_pil[0].height * _zscale[0]))
            _zref[0] = ImageTk.PhotoImage(_pil[0].resize((w, h), Image.Resampling.LANCZOS))
            _zcanvas.delete("all")
            _zcanvas.create_image(0, 0, image=_zref[0], anchor="nw")
            _zcanvas.configure(scrollregion=(0, 0, w, h))
        def _zwheel(e):
            _zscale[0] = max(0.1, min(8.0, _zscale[0] * (1.1 if e.delta > 0 else 0.9)))
            _zrender()
        _zcanvas.bind("<MouseWheel>", _zwheel)
        tk.Label(zoom_win, text="Scroll to zoom  •  Double-click to fit",
                 bg="#002d35", fg="#6abdc5", font=f_sm).pack(side="bottom")
        zoom_win.after(50, _zrender)

    img_lbl.bind("<Button-1>", _open_zoom)
    img_lbl.config(cursor="hand2")
    tk.Label(left, text="Click image to zoom  •  Live CATIA model on other monitor",
             bg=BG_IMG, fg="#80c4cc", font=f_sm).pack(side="bottom", pady=(0, 4))


def _show_hitl_ui(image_path, clash_result, pub_name, boundary, clash_idx, total_clashes):
    """
    Blocking HITL review.
    • If _HITL_FRAME is set (injected by master_app), builds UI inside that frame
      and blocks the calling thread via threading.Event until a decision is made.
    • Otherwise falls back to a standalone tk.Tk() window.
    Returns (decision_str, note_str) where decision_str is 'approve' | 'reject' | 'override'.
    """
    import threading as _threading
    decision = {"value": "approve", "note": ""}

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
        return decision["value"], decision["note"]

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
        return decision["value"], decision["note"]


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

    # #6 Permanent save directory — Results/dmu_agent/{pub_name}/
    _safe_name = re.sub(r'[^\w\-]', '_', pub_name)
    save_base  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'Results', 'dmu_agent', _safe_name)
    mmc_save_dir = os.path.join(save_base, 'MMC')
    lmc_save_dir = os.path.join(save_base, 'LMC')

    # #8 Track first iso path from each boundary for delta view.
    mmc_iso_path = None
    lmc_iso_path = None

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
                img_path = _capture_and_annotate_clash(catia, active_doc, clash_r,
                                                       clash_idx=idx, save_dir=mmc_save_dir)
                if idx == 0 and img_path:
                    iso_candidate = os.path.join(mmc_save_dir, f'clash_{idx:03d}_iso.png')
                    mmc_iso_path = iso_candidate if os.path.isfile(iso_candidate) else img_path
                dec, note = _show_hitl_ui(img_path, clash_r, pub_name, "MMC", idx, total)
                print(f"  [HITL] MMC clash {idx+1}/{total} decision: {dec.upper()}" +
                      (f" | note: {note}" if note else ""))
                reviewed.append(dict(clash_r,
                                     comment=f"HITL:{dec}" + (f" | {note}" if note else ""),
                                     keep=("Yes" if dec == "approve" else "No"),
                                     image_path=img_path))
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
                img_path = _capture_and_annotate_clash(catia, active_doc, clash_r,
                                                       clash_idx=idx, save_dir=lmc_save_dir)
                if idx == 0 and img_path:
                    iso_candidate = os.path.join(lmc_save_dir, f'clash_{idx:03d}_iso.png')
                    lmc_iso_path = iso_candidate if os.path.isfile(iso_candidate) else img_path
                dec, note = _show_hitl_ui(img_path, clash_r, pub_name, "LMC", idx, total)
                print(f"  [HITL] LMC clash {idx+1}/{total} decision: {dec.upper()}" +
                      (f" | note: {note}" if note else ""))
                reviewed.append(dict(clash_r,
                                     comment=f"HITL:{dec}" + (f" | {note}" if note else ""),
                                     keep=("Yes" if dec == "approve" else "No"),
                                     image_path=img_path))
            # All LMC clashes reviewed — now restore visibility
            _global_unhide(catia, active_doc, root_product)
            non_clash = [r for r in lmc_results if not r.get("is_clash")]
            lmc_db_rows = reviewed + non_clash
        _cleanup_clash(lmc_clashes_col, lmc_new_clash)
        cleanup_pairs[-1][1] = None

        if not has_lmc_clash:
            time.sleep(0.5)

        # #8 MMC/LMC delta view — side-by-side isometric comparison.
        if mmc_iso_path or lmc_iso_path:
            delta_path = os.path.join(save_base, 'delta_MMC_vs_LMC.png')
            _generate_delta_view(
                mmc_iso_path or lmc_iso_path,
                lmc_iso_path or mmc_iso_path,
                delta_path, pub_name, upper_limit, lower_limit,
            )

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
        pythoncom.CoInitialize()   # Required when called from a non-COM-initialised thread (e.g. PyInstaller windowed EXE)
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
                st.lower_tolerance,
                COALESCE(NULLIF(d.project_tag,''), d.file_name) AS drawing_name
            FROM  human_mapping       m
            JOIN  semantic_tolerances st ON st.id        = m.vlm_id
            JOIN  catia_parameters    cp ON cp.id        = m.cad_id
            JOIN  drawings            d  ON d.drawing_id = st.drawing_id
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
    for pub_name, part_path, feature_name, nominal, upper_tol, lower_tol, drawing_name in mappings:
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
                drawing_name     TEXT,
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
                image_path       TEXT,
                run_at           TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')

        # project_tag resolved per-publication via the drawing that was mapped
        # Build a lookup: publication_name -> (project_tag, drawing_name)
        pub_drawing_map = {}
        for pub_name_m, _, _, _, _, _, dwg_name in mappings:
            pub_drawing_map[pub_name_m] = dwg_name
        # Fallback project_tag = assembly name (used only if pub not found)
        fallback_tag = target_assembly_name

        row_count = 0
        for pub, u_sum, l_sum, mmc_rows, lmc_rows in results:
            # Per-publication drawing tag
            pub_drawing  = pub_drawing_map.get(pub, fallback_tag)
            for boundary, rows in (("MMC", mmc_rows), ("LMC", lmc_rows)):
                for r in rows:
                    cur.execute(
                        '''
                        INSERT INTO actuation_results
                            (project_tag, drawing_name, assembly_name, publication_name, boundary,
                             product1, product2, type, value, status, info,
                             keep, comment, location, image_path)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''',
                        (
                            pub_drawing,
                            pub_drawing,
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
                            r.get("image_path", None),
                        )
                    )
                    row_count += 1

        conn.commit()
        conn.close()
        print(f"\n✅ {row_count} conflict row(s) saved to actuation_results table.")
    except Exception as e:
        print(f"\n⚠  Could not save results to DB: {e}")

    # ── Auto-generate HTML report ─────────────────────────────────────────────
    try:
        # Standard import — works correctly both in normal Python and PyInstaller EXE.
        import generate_report
        generate_report.generate_html_report()
    except Exception as _re:
        print(f"⚠  Report generation failed (non-fatal): {_re}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    run_agent_3_actuation()

