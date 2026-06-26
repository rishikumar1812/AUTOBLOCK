"""
test_read_building_status.py  —  Main PC diagnostic ONLY
Standalone script — does NOT touch inline_automation.py, Data.ini,
or click anything. Read-only check.

Goal: confirm pywinauto can read the live "Wait" / "Down" / "Not Use"
/ "Pass" text out of the Front Rack status controls.

Run while InLine_Pro is open, while you can also see the screen,
so you can compare the printed text against what you see live:

    python test_read_building_status.py

Output goes to console AND test_read_output.txt in the same folder.
"""

import os
import sys
import time
import traceback

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_read_output.txt")

try:
    from pywinauto import Application
except ImportError:
    print("ERROR: pywinauto not installed. Run:  pip install pywinauto")
    sys.exit(1)


def log(msg: str, f):
    print(msg)
    f.write(msg + "\n")


def separator(f, char="=", width=80):
    log(char * width, f)


def get_text_safely(ctrl) -> str:
    """
    Try every method pywinauto exposes for reading text.
    Edit controls usually answer to window_text(), but some legacy
    Win32 edits only answer correctly to get_value() (uia) or
    .texts() (win32) — try all three, report all three.
    """
    results = {}
    try:
        results["window_text()"] = ctrl.window_text()
    except Exception as e:
        results["window_text()"] = f"ERROR: {e}"

    try:
        results["get_value()"] = ctrl.get_value()
    except Exception as e:
        results["get_value()"] = f"<not available: {e}>"

    try:
        texts = ctrl.texts()
        results["texts()"] = texts
    except Exception as e:
        results["texts()"] = f"<not available: {e}>"

    return results


def main():
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        log(f"Building Status Read Test — {__import__('datetime').datetime.now()}", f)
        log("", f)

        # ---------------------------------------------------------
        # Step 1 — connect + find main window
        # ---------------------------------------------------------
        separator(f)
        log("STEP 1 — Connect to InLine_Pro", f)
        separator(f)
        try:
            app = Application(backend="uia").connect(title_re=".*InLine.*", timeout=10)
            win = app.window(title_re=".*InLine.*")
            log(f"  Connected: '{win.window_text()}'", f)
        except Exception as e:
            log(f"  ERROR: could not connect — {e}", f)
            log(traceback.format_exc(), f)
            return

        # ---------------------------------------------------------
        # Step 2 — find the Front Rack pane
        # ---------------------------------------------------------
        separator(f)
        log("STEP 2 — Find 'Front Rack' pane", f)
        separator(f)
        try:
            front_rack_pane = win.child_window(title="Front Rack", control_type="Pane")
            front_rack_pane.wait("exists", timeout=5)
            rect = front_rack_pane.rectangle()
            log(f"  Found 'Front Rack' pane — rect: {rect}", f)
        except Exception as e:
            log(f"  ERROR: could not find 'Front Rack' pane — {e}", f)
            log("  Falling back to whole-window search instead.", f)
            front_rack_pane = win

        # ---------------------------------------------------------
        # Step 3 — list ALL Edit controls + nearby Text labels,
        # sorted top-to-bottom, so label and value can be visually
        # paired by matching vertical position (top coordinate)
        # ---------------------------------------------------------
        separator(f)
        log("STEP 3 — All Text labels + Edit values inside Front Rack area", f)
        log("(Reading from the FULL window, then filtering by the Front", f)
        log(" Rack pane's x-range, since label/value may not be nested", f)
        log(" under the pane in the accessibility tree even though they", f)
        log(" are visually inside it.)", f)
        separator(f)

        try:
            fr_rect = front_rack_pane.rectangle()
            x_min, x_max = fr_rect.left, fr_rect.right
        except Exception:
            x_min, x_max = 0, 100000  # no filtering if pane lookup failed

        log(f"  Filtering controls with left-x between {x_min} and {x_max}", f)
        log("", f)

        labels = []   # (top, left, text)
        values = []   # (top, left, control, text_dict)

        for child in win.descendants():
            try:
                rect = child.rectangle()
                if not (x_min - 5 <= rect.left <= x_max + 5):
                    continue  # not in Front Rack's x-band

                ctrl_type = child.element_info.control_type or "?"
                title = (child.window_text() or "").strip()

                if ctrl_type == "Text" and title:
                    labels.append((rect.top, rect.left, title))
                elif ctrl_type == "Edit":
                    text_info = get_text_safely(child)
                    values.append((rect.top, rect.left, child, text_info))
            except Exception:
                continue

        labels.sort(key=lambda t: -t[0])   # sort by top descending (matches screen top->bottom since B-T axis can vary)
        values.sort(key=lambda t: -t[0])

        log(f"  Found {len(labels)} Text labels, {len(values)} Edit controls in this x-band", f)
        log("", f)

        log(f"  {'Top':<6} {'Label (Text ctrl)':<20} {'Top':<6} {'Edit window_text()':<25} {'Edit get_value()':<25}", f)
        log(f"  {'-'*6} {'-'*20} {'-'*6} {'-'*25} {'-'*25}", f)

        # naive pairing by matching 'top' coordinate within a tolerance
        used_values = set()
        for ltop, lleft, ltext in labels:
            match = None
            for i, (vtop, vleft, vctrl, vtext) in enumerate(values):
                if i in used_values:
                    continue
                if abs(vtop - ltop) <= 3:  # same row, small tolerance
                    match = (vtop, vleft, vctrl, vtext)
                    used_values.add(i)
                    break

            if match:
                vtop, vleft, vctrl, vtext = match
                wt = vtext.get("window_text()", "")
                gv = vtext.get("get_value()", "")
                log(f"  {ltop:<6} {ltext:<20} {vtop:<6} {str(wt):<25} {str(gv):<25}", f)
            else:
                log(f"  {ltop:<6} {ltext:<20} {'--':<6} {'<no matching Edit found>':<25}", f)

        # Show any leftover Edit controls that weren't matched to a label
        leftover = [v for i, v in enumerate(values) if i not in used_values]
        if leftover:
            log("", f)
            log(f"  {len(leftover)} Edit control(s) with NO matching label nearby:", f)
            for vtop, vleft, vctrl, vtext in leftover:
                log(f"    top={vtop} left={vleft}  window_text()={vtext.get('window_text()')!r}  get_value()={vtext.get('get_value()')!r}", f)

        # ---------------------------------------------------------
        # Step 4 — direct targeted check: try to specifically grab
        # whatever Edit sits at the same row as the "Building 6" label
        # ---------------------------------------------------------
        separator(f)
        log("STEP 4 — Targeted check: value next to 'Building 6' label", f)
        separator(f)
        try:
            b6_label = win.child_window(title="Building 6", control_type="Text")
            b6_rect = b6_label.rectangle()
            log(f"  'Building 6' label rect: {b6_rect}", f)

            found = False
            for child in win.descendants(control_type="Edit"):
                rect = child.rectangle()
                if abs(rect.top - b6_rect.top) <= 3 and rect.left > b6_rect.right:
                    text_info = get_text_safely(child)
                    log(f"  MATCH — Edit at {rect}", f)
                    log(f"    window_text() = {text_info.get('window_text()')!r}", f)
                    log(f"    get_value()   = {text_info.get('get_value()')!r}", f)
                    log(f"    texts()       = {text_info.get('texts()')!r}", f)
                    found = True
            if not found:
                log("  No Edit control found at matching row to the right of 'Building 6'.", f)
        except Exception as e:
            log(f"  ERROR: {e}", f)
            log(traceback.format_exc(), f)

        separator(f)
        log("TEST COMPLETE", f)
        separator(f)

    print(f"\nDone. Open {OUTPUT_FILE} for full results.")
    print("Compare the printed Edit text against what you see on screen RIGHT NOW")
    print("for Building 6 (Front Rack) — does it say 'Wait', 'Down', etc.?")


if __name__ == "__main__":
    main()
