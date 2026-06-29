"""
test_read_building_status_v2.py  —  Main PC diagnostic ONLY
Standalone, READ-ONLY. Does not click, does not touch Data.ini.

v2 — fixes v1's wrong assumptions:
  - v1 assumed "Front Rack" container's control_type is literally "Pane".
    It is NOT (confirmed by your last test — lookup timed out).
  - v1 assumed status text (Wait/Down/Pass) lives in "Edit" controls.
    Your last run found 0 Edit controls near the Building labels at all.

v2 makes NO assumptions about control_type. Instead it:
  1. Finds every control with title "Front Rack" or "Rear Rack" and
     prints its REAL control_type + class_name (whatever it actually is).
  2. Finds every control with title "Building 6" (there will be 2 — one
     Front, one Rear) and prints their rect + real control_type/class.
  3. For EACH "Building 6" instance, scans ALL descendants of the window
     and lists every control (any type) sitting in the same row (top
     within +/-5px) ordered left-to-right, printing type/class/text for
     each. The status word should be one of these neighbors.
  4. Same again for "Building 1" as a second data point (different
     row, helps confirm the pattern is consistent).

Run while InLine_Pro is open AND while you can see the live screen:
    python test_read_building_status_v2.py

Output -> test_read_output_v2.txt (and console)
"""

import os
import sys
import traceback
from datetime import datetime

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_read_output_v2.txt")

try:
    from pywinauto import Application
except ImportError:
    print("ERROR: pywinauto not installed. Run:  pip install pywinauto")
    sys.exit(1)


def log(msg, f):
    print(msg)
    f.write(msg + "\n")


def sep(f, c="=", w=90):
    log(c * w, f)


def describe(ctrl):
    """Return a dict of everything we can safely read about a control."""
    out = {}
    try:
        out["control_type"] = ctrl.element_info.control_type
    except Exception as e:
        out["control_type"] = f"<err: {e}>"
    try:
        out["class_name"] = ctrl.class_name()
    except Exception as e:
        out["class_name"] = f"<err: {e}>"
    try:
        out["window_text"] = ctrl.window_text()
    except Exception as e:
        out["window_text"] = f"<err: {e}>"
    try:
        out["rect"] = ctrl.rectangle()
    except Exception as e:
        out["rect"] = f"<err: {e}>"
    try:
        out["auto_id"] = ctrl.element_info.automation_id
    except Exception:
        out["auto_id"] = ""
    try:
        out["get_value"] = ctrl.get_value()
    except Exception:
        out["get_value"] = "<n/a>"
    return out


def main():
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        log(f"Building Status Read Test v2 — {datetime.now()}", f)
        log("", f)

        # ---------------------------------------------------
        sep(f)
        log("STEP 1 — Connect", f)
        sep(f)
        try:
            app = Application(backend="uia").connect(title_re=".*InLine.*", timeout=10)
            win = app.window(title_re=".*InLine.*")
            log(f"  Connected: '{win.window_text()}'", f)
        except Exception as e:
            log(f"  ERROR connecting: {e}", f)
            log(traceback.format_exc(), f)
            return

        # Cache descendants once — this can be a few hundred controls,
        # walking it repeatedly is slow and InLine_Pro is a live app
        # (positions could shift slightly between calls otherwise).
        log("  Caching window.descendants() once for this run...", f)
        all_ctrls = win.descendants()
        log(f"  Total descendants found: {len(all_ctrls)}", f)

        # ---------------------------------------------------
        sep(f)
        log("STEP 2 — Real control_type/class_name for 'Front Rack' / 'Rear Rack'", f)
        sep(f)
        for wanted in ("Front Rack", "Rear Rack"):
            matches = [c for c in all_ctrls if (c.window_text() or "").strip() == wanted]
            log(f"  '{wanted}': {len(matches)} match(es)", f)
            for m in matches:
                d = describe(m)
                log(f"    control_type={d['control_type']}  class_name={d['class_name']}  "
                    f"rect={d['rect']}  auto_id={d['auto_id']!r}", f)
        log("", f)

        # ---------------------------------------------------
        sep(f)
        log("STEP 3 — All 'Building 6' instances + their row neighbors", f)
        sep(f)
        b6_matches = [c for c in all_ctrls if (c.window_text() or "").strip() == "Building 6"]
        log(f"  Found {len(b6_matches)} controls titled 'Building 6'", f)

        for idx, b6 in enumerate(b6_matches):
            d = describe(b6)
            log("", f)
            log(f"  --- 'Building 6' instance #{idx+1} ---", f)
            log(f"    control_type={d['control_type']}  class_name={d['class_name']}  rect={d['rect']}", f)

            try:
                b6_rect = b6.rectangle()
            except Exception as e:
                log(f"    could not get rectangle: {e}", f)
                continue

            log(f"    Scanning ALL descendants for same row (top within +/-5px of {b6_rect.top})...", f)
            row_ctrls = []
            for c in all_ctrls:
                try:
                    r = c.rectangle()
                    if abs(r.top - b6_rect.top) <= 5:
                        row_ctrls.append(c)
                except Exception:
                    continue

            # sort left -> right so output reads in visual order
            row_ctrls_sorted = sorted(row_ctrls, key=lambda c: c.rectangle().left)
            log(f"    {len(row_ctrls_sorted)} control(s) found in this row:", f)
            log(f"    {'Left':<6} {'Type':<12} {'Class':<14} {'Text':<20} {'GetValue':<20}", f)
            log(f"    {'-'*6} {'-'*12} {'-'*14} {'-'*20} {'-'*20}", f)
            for c in row_ctrls_sorted:
                cd = describe(c)
                log(f"    {cd['rect'].left if hasattr(cd['rect'],'left') else '?':<6} "
                    f"{str(cd['control_type']):<12} {str(cd['class_name']):<14} "
                    f"{str(cd['window_text'])[:18]:<20} {str(cd['get_value'])[:18]:<20}", f)

        # ---------------------------------------------------
        sep(f)
        log("STEP 4 — Same check for 'Building 1' (second data point)", f)
        sep(f)
        b1_matches = [c for c in all_ctrls if (c.window_text() or "").strip() == "Building 1"]
        log(f"  Found {len(b1_matches)} controls titled 'Building 1'", f)

        for idx, b1 in enumerate(b1_matches):
            d = describe(b1)
            log("", f)
            log(f"  --- 'Building 1' instance #{idx+1} ---", f)
            log(f"    control_type={d['control_type']}  class_name={d['class_name']}  rect={d['rect']}", f)
            try:
                b1_rect = b1.rectangle()
            except Exception as e:
                log(f"    could not get rectangle: {e}", f)
                continue

            row_ctrls = []
            for c in all_ctrls:
                try:
                    r = c.rectangle()
                    if abs(r.top - b1_rect.top) <= 5:
                        row_ctrls.append(c)
                except Exception:
                    continue
            row_ctrls_sorted = sorted(row_ctrls, key=lambda c: c.rectangle().left)
            log(f"    {len(row_ctrls_sorted)} control(s) found in this row:", f)
            log(f"    {'Left':<6} {'Type':<12} {'Class':<14} {'Text':<20} {'GetValue':<20}", f)
            log(f"    {'-'*6} {'-'*12} {'-'*14} {'-'*20} {'-'*20}", f)
            for c in row_ctrls_sorted:
                cd = describe(c)
                log(f"    {cd['rect'].left if hasattr(cd['rect'],'left') else '?':<6} "
                    f"{str(cd['control_type']):<12} {str(cd['class_name']):<14} "
                    f"{str(cd['window_text'])[:18]:<20} {str(cd['get_value'])[:18]:<20}", f)

        sep(f)
        log("TEST COMPLETE", f)
        sep(f)

    print(f"\nDone. Open {OUTPUT_FILE}")
    print("Look at STEP 3 / STEP 4 row tables — find the control whose")
    print("Text or GetValue column shows 'Wait' / 'Down' / 'Not Use' / 'PASS'")
    print("right now, matching what you see live on screen for that Building.")


if __name__ == "__main__":
    main()
