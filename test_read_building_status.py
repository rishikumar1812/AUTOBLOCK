"""
test_read_building_status.py  —  Main PC diagnostic ONLY
Read-only. Does not click, does not edit Data.ini.

One-pass screen calibration report for InLine_Pro's Auto-Status
Windows screen. Finds the exact pixel positions of every control
inline_automation.py depends on, in a SINGLE run:

  STEP 1 — Connect to InLine_Pro
  STEP 2 — Building 1-10 / Function 1-4 label + value positions
           (Front + Rear racks)          -> building_check.* config
  STEP 3 — STOP / SETUP / START / OK / Yes button positions + state

Run while InLine_Pro is open, on the Auto-Status Windows tab:
    python test_read_building_status.py
    (or the built .exe — see test_read_building_status.spec)

Output -> C:\\MainPC\\logs\\test_read_output.txt + console
"""

import os
import sys
import traceback
from datetime import datetime

OUTPUT_DIR = "C:\\MainPC\\logs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "test_read_output.txt")

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

try:
    from pywinauto import Application
except ImportError:
    print("ERROR: pywinauto not installed. Run: pip install pywinauto")
    sys.exit(1)


def log(msg, f):
    print(msg)
    f.write(msg + "\n")


def sep(f, c="=", w=92):
    log(c * w, f)


def safe_text(ctrl) -> str:
    try:
        val = ctrl.window_text()
        return str(val).strip() if val is not None else ""
    except Exception:
        return ""


def safe_value(ctrl) -> str:
    try:
        val = ctrl.get_value()
        return str(val).strip() if val is not None else ""
    except Exception:
        return ""


def safe_rect(ctrl):
    try:
        return ctrl.rectangle()
    except Exception:
        return None


def safe_enabled(ctrl):
    try:
        return ctrl.is_enabled()
    except Exception:
        return "?"


def safe_control_type(ctrl) -> str:
    try:
        return ctrl.element_info.control_type
    except Exception:
        return "?"


def safe_class_name(ctrl) -> str:
    try:
        return ctrl.class_name()
    except Exception:
        return "?"


def find_value_for_label(all_ctrls, label_ctrl, tol=5):
    """
    Given a label control (e.g. 'Building 6'), find the closest Edit
    control on the same row (top within tol px). Picks the CLOSEST
    by horizontal distance rather than assuming a fixed left
    position — this script is what CALIBRATES that fixed position
    for building_check.*, so it can't assume it already knows it.
    """
    r = safe_rect(label_ctrl)
    if r is None:
        return None
    candidates = []
    for c in all_ctrls:
        cr = safe_rect(c)
        if cr is None:
            continue
        if abs(cr.top - r.top) <= tol and safe_control_type(c) == "Edit":
            candidates.append((abs(cr.left - r.left), c))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def main():
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        log(f"Screen Calibration Report — {datetime.now()}", f)
        log("", f)

        # =====================================================
        # STEP 1 — Connect
        # =====================================================
        sep(f)
        log("STEP 1 — Connect to InLine_Pro", f)
        sep(f)
        try:
            app = Application(backend="uia").connect(title_re=".*InLine.*", timeout=10)
            win = app.window(title_re=".*InLine.*")
            log(f"  Connected: '{win.window_text()}'", f)
        except Exception as e:
            log(f"  ERROR: could not connect — {e}", f)
            log(traceback.format_exc(), f)
            print(f"\nFAILED — see {OUTPUT_FILE}")
            return

        log("  Caching window.descendants() once for this run...", f)
        all_ctrls = win.descendants()
        log(f"  Total descendants found: {len(all_ctrls)}", f)
        log("", f)

        # =====================================================
        # STEP 2 — Building 1-10 / Function 1-4 positions, one pass
        # =====================================================
        sep(f)
        log("STEP 2 — Building / Function label + value positions", f)
        log("Used to fill in config.json's building_check.* left/tolerance values.", f)
        sep(f)

        row_fmt = "  {:<12} {:<6} {:<10} {:<9} | {:<9} {:<8} {:<8} {:<18}"
        header = row_fmt.format(
            "Title", "Found", "LabelLeft", "LabelTop",
            "ValLeft", "ValTop", "ValType", "ValText")

        for group_label, count in (("Building", 10), ("Function", 4)):
            for rack in ("Front", "Rear"):
                log("", f)
                log(f"  -- {group_label} 1-{count} ({rack} Rack, by visual position) --", f)
                log(header, f)
                log("  " + "-" * (len(header) - 2), f)
                for n in range(1, count + 1):
                    title = f"{group_label} {n}"
                    title_normalized = title.replace(" ", "")
                    # 2 real matches exist window-wide (front+rear share the
                    # same title text) — list every one found, with its own
                    # position, rather than guessing which side is which.
                    # Space-tolerant: Building 10/20 render with an extra
                    # space inside the number on this screen (e.g.
                    # 'Building 1 0'), unlike Building 1-9 — comparing with
                    # all internal whitespace stripped handles both without
                    # changing anything for the already-working 1-9 case.
                    matches = [c for c in all_ctrls
                               if safe_text(c).replace(" ", "") == title_normalized]
                    if not matches:
                        log(row_fmt.format(title, "0", "-", "-", "-", "-", "-", "-"), f)
                        continue
                    for m in matches:
                        r = safe_rect(m)
                        left = r.left if r else "?"
                        top = r.top if r else "?"
                        val_ctrl = find_value_for_label(all_ctrls, m)
                        if val_ctrl is not None:
                            vr = safe_rect(val_ctrl)
                            vleft = vr.left if vr else "?"
                            vtop = vr.top if vr else "?"
                            vtype = safe_control_type(val_ctrl)
                            vtext = (safe_value(val_ctrl) or safe_text(val_ctrl))[:18]
                        else:
                            vleft = vtop = vtype = vtext = "<none>"
                        log(row_fmt.format(
                            title, str(len(matches)), str(left), str(top),
                            str(vleft), str(vtop), str(vtype), str(vtext)), f)

        log("", f)
        log("  HOW TO READ THIS: for each title, the two rows are the Front", f)
        log("  and Rear rack instances — whichever has the SMALLER LabelLeft", f)
        log("  is Front, the larger is Rear (matches building_check.front_*", f)
        log("  vs rear_* in config.json). Compare ValText against what the", f)
        log("  screen shows right now to confirm you've got the right row.", f)

        # =====================================================
        # STEP 3 — Automation buttons
        # =====================================================
        log("", f)
        sep(f)
        log("STEP 3 — Automation button positions + enabled state", f)
        log("Sanity-check for the buttons inline_automation.py clicks.", f)
        sep(f)
        btn_fmt = "  {:<10} {:<6} {:<12} {:<6} {:<6} {:<8}"
        log(btn_fmt.format("Button", "Found", "ControlType", "Left", "Top", "Enabled"), f)
        log("  " + "-" * 52, f)
        for name in ("STOP", "SETUP", "START", "OK", "Yes"):
            matches = [c for c in all_ctrls
                       if safe_text(c) == name and safe_control_type(c) == "Button"]
            if not matches:
                log(btn_fmt.format(name, "0", "-", "-", "-", "-"), f)
                continue
            for m in matches:
                r = safe_rect(m)
                left = r.left if r else "?"
                top = r.top if r else "?"
                log(btn_fmt.format(name, str(len(matches)), safe_control_type(m),
                                    str(left), str(top), str(safe_enabled(m))), f)

        log("", f)
        sep(f)
        log("REPORT COMPLETE", f)
        sep(f)

    print(f"\nDone. Open {OUTPUT_FILE} for full results.")
    print("STEP 2 -> config.json building_check.front_label_left / front_value_left / etc.")
    print("STEP 3 -> confirms STOP/SETUP/START/OK/Yes are all found as expected")


if __name__ == "__main__":
    main()
