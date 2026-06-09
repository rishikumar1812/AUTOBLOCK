"""
inspect_inline.py  —  Main PC
Deep inspector for InLine_Pro window controls.

Tries every method pywinauto has to find and list all controls
so you can identify exactly what type "Stop" is —
Button, Custom, Pane, Image, MenuItem, or something else.

Run while InLine_Pro is open:
    python inspect_inline.py

Output goes to console AND inspect_output.txt in the same folder.
"""

import os
import sys
import time
import traceback

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "inspect_output.txt")

# ── Try to import pywinauto ───────────────────────────────
try:
    from pywinauto import Application, Desktop
    from pywinauto import findwindows
except ImportError:
    print("ERROR: pywinauto not installed.")
    print("Run:  pip install pywinauto")
    sys.exit(1)


def log(msg: str, f):
    print(msg)
    f.write(msg + "\n")


def separator(f, char="=", width=70):
    log(char * width, f)


# =========================================================
# Step 1 — Find all windows matching InLine_Pro
# =========================================================
def find_all_matching_windows(f):
    separator(f)
    log("STEP 1 — Finding all windows matching 'InLine'", f)
    separator(f)

    for backend in ["uia", "win32"]:
        log(f"\n  Backend: {backend}", f)
        try:
            handles = findwindows.find_windows(title_re=".*InLine.*",
                                               backend=backend)
            if not handles:
                log(f"    No windows found with backend={backend}", f)
            for h in handles:
                try:
                    app = Application(backend=backend).connect(handle=h)
                    win = app.window(handle=h)
                    log(f"    Handle: {h}", f)
                    log(f"    Title:  {win.window_text()}", f)
                    log(f"    Class:  {win.class_name()}", f)
                    log(f"    Rect:   {win.rectangle()}", f)
                except Exception as e:
                    log(f"    Handle {h} error: {e}", f)
        except Exception as e:
            log(f"    findwindows error: {e}", f)


# =========================================================
# Step 2 — Connect and print ALL control identifiers
# Both uia and win32 backends
# =========================================================
def print_all_identifiers(f):
    separator(f)
    log("STEP 2 — print_control_identifiers() — both backends", f)
    separator(f)

    for backend in ["uia", "win32"]:
        log(f"\n{'='*30} Backend: {backend} {'='*30}", f)
        try:
            app = Application(backend=backend).connect(
                title_re=".*InLine.*", timeout=10
            )
            win = app.window(title_re=".*InLine.*")
            log(f"  Connected to: '{win.window_text()}'", f)
            log(f"  Class:        {win.class_name()}", f)
            log("", f)
            log("  --- Control Identifiers ---", f)

            # Redirect print_control_identifiers to file
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                win.print_control_identifiers()
            output = buf.getvalue()
            log(output, f)

        except Exception as e:
            log(f"  ERROR with backend={backend}: {e}", f)
            log(traceback.format_exc(), f)


# =========================================================
# Step 3 — Walk every child control manually
# Shows: type, title, class, enabled, visible, rect
# =========================================================
def walk_all_children(f):
    separator(f)
    log("STEP 3 — Walking ALL child controls manually (uia)", f)
    separator(f)

    try:
        app = Application(backend="uia").connect(
            title_re=".*InLine.*", timeout=10
        )
        win = app.window(title_re=".*InLine.*")
        log(f"  Window: '{win.window_text()}'", f)
        log("", f)

        log(f"  {'#':<4} {'Type':<20} {'Title':<30} {'Class':<20} {'En':<4} {'Vi':<4} Rect", f)
        log(f"  {'-'*4} {'-'*20} {'-'*30} {'-'*20} {'-'*4} {'-'*4} {'-'*30}", f)

        children = win.descendants()
        for idx, child in enumerate(children):
            try:
                ctrl_type = child.element_info.control_type or "?"
                title     = (child.window_text() or "")[:28]
                cls       = (child.class_name()  or "")[:18]
                enabled   = "Y" if child.is_enabled()  else "N"
                visible   = "Y" if child.is_visible()  else "N"
                rect      = child.rectangle()
                log(
                    f"  {idx:<4} {ctrl_type:<20} {title:<30} {cls:<20} "
                    f"{enabled:<4} {visible:<4} {rect}",
                    f
                )
            except Exception as e:
                log(f"  {idx:<4} ERROR reading child: {e}", f)

    except Exception as e:
        log(f"  ERROR: {e}", f)
        log(traceback.format_exc(), f)


# =========================================================
# Step 4 — Search specifically for anything containing
# "Stop", "Setup", "Start", "Yes", "OK" in any property
# =========================================================
def search_key_controls(f):
    separator(f)
    log("STEP 4 — Searching for Stop / SetUp / Start / Yes / OK", f)
    separator(f)

    keywords = ["stop", "setup", "start", "yes", "ok"]

    for backend in ["uia", "win32"]:
        log(f"\n  Backend: {backend}", f)
        try:
            app = Application(backend=backend).connect(
                title_re=".*InLine.*", timeout=10
            )
            win = app.window(title_re=".*InLine.*")

            for child in win.descendants():
                try:
                    title = (child.window_text() or "").strip()
                    if any(kw in title.lower() for kw in keywords):
                        ctrl_type = getattr(child.element_info,
                                            "control_type", "?") or "?"
                        cls       = child.class_name() or "?"
                        enabled   = child.is_enabled()
                        visible   = child.is_visible()
                        rect      = child.rectangle()
                        log(f"    FOUND: '{title}'", f)
                        log(f"      control_type : {ctrl_type}", f)
                        log(f"      class_name   : {cls}", f)
                        log(f"      enabled      : {enabled}", f)
                        log(f"      visible      : {visible}", f)
                        log(f"      rect         : {rect}", f)
                        log("", f)
                except Exception:
                    pass
        except Exception as e:
            log(f"  ERROR: {e}", f)


# =========================================================
# Step 5 — Try win32 menu inspection
# In case Stop is a menu item not a button
# =========================================================
def inspect_menus(f):
    separator(f)
    log("STEP 5 — Menu inspection (win32)", f)
    separator(f)

    try:
        app = Application(backend="win32").connect(
            title_re=".*InLine.*", timeout=10
        )
        win = app.window(title_re=".*InLine.*")

        menu = win.menu()
        if menu:
            log(f"  Menu found — items:", f)
            for i in range(menu.item_count()):
                try:
                    item = menu.item(i)
                    log(f"    [{i}] {item.text()}", f)
                except Exception as e:
                    log(f"    [{i}] error: {e}", f)
        else:
            log("  No menu found on main window", f)

    except Exception as e:
        log(f"  ERROR: {e}", f)


# =========================================================
# Step 6 — Screenshot of the window
# Saves inline_screenshot.png so you can see the layout
# =========================================================
def take_screenshot(f):
    separator(f)
    log("STEP 6 — Screenshot", f)
    separator(f)

    try:
        from PIL import ImageGrab
        import pywinauto

        app = Application(backend="uia").connect(
            title_re=".*InLine.*", timeout=10
        )
        win    = app.window(title_re=".*InLine.*")
        rect   = win.rectangle()
        region = (rect.left, rect.top, rect.right, rect.bottom)
        img    = ImageGrab.grab(bbox=region)
        out    = os.path.join(os.path.dirname(OUTPUT_FILE), "inline_screenshot.png")
        img.save(out)
        log(f"  Screenshot saved → {out}", f)

    except ImportError:
        log("  Pillow not installed — skipping screenshot", f)
        log("  Install with:  pip install Pillow", f)
    except Exception as e:
        log(f"  Screenshot error: {e}", f)


# =========================================================
# Step 7 — Try clicking Stop by every possible method
# Reports which one works without actually clicking
# (dry run — just checks if element is found)
# =========================================================
def dry_run_find_stop(f):
    separator(f)
    log("STEP 7 — Dry run: try every method to find 'Stop'", f)
    separator(f)

    attempts = [
        ("uia", "Button",   {"title": "Stop", "control_type": "Button"}),
        ("uia", "Custom",   {"title": "Stop", "control_type": "Custom"}),
        ("uia", "Pane",     {"title": "Stop", "control_type": "Pane"}),
        ("uia", "Image",    {"title": "Stop", "control_type": "Image"}),
        ("uia", "Text",     {"title": "Stop", "control_type": "Text"}),
        ("uia", "title_re", {"title_re": ".*Stop.*"}),
        ("uia", "auto_id",  {"auto_id": "Stop"}),
        ("win32","win32_btn",{"title": "Stop", "class_name": "Button"}),
        ("win32","win32_any",{"title": "Stop"}),
    ]

    for backend, method_name, kwargs in attempts:
        try:
            app = Application(backend=backend).connect(
                title_re=".*InLine.*", timeout=5
            )
            win  = app.window(title_re=".*InLine.*")
            ctrl = win.child_window(**kwargs)
            ctrl.wait("exists", timeout=3)
            log(f"  ✓ FOUND with backend={backend}  method={method_name}  kwargs={kwargs}", f)
            log(f"      type    : {getattr(ctrl.element_info, 'control_type', '?')}", f)
            log(f"      title   : {ctrl.window_text()}", f)
            log(f"      class   : {ctrl.class_name()}", f)
            log(f"      enabled : {ctrl.is_enabled()}", f)
            log(f"      rect    : {ctrl.rectangle()}", f)
            log("", f)
        except Exception as e:
            log(f"  ✗ NOT found  backend={backend}  method={method_name} — {e}", f)


# =========================================================
# Entry point
# =========================================================
if __name__ == "__main__":
    print(f"\nInLine_Pro Inspector")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Make sure InLine_Pro is open and visible.\n")

    time.sleep(1)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        log(f"InLine_Pro Inspector — {__import__('datetime').datetime.now()}", f)
        log("", f)

        find_all_matching_windows(f)
        print_all_identifiers(f)
        walk_all_children(f)
        search_key_controls(f)
        inspect_menus(f)
        take_screenshot(f)
        dry_run_find_stop(f)

        separator(f)
        log("INSPECTION COMPLETE", f)
        separator(f)

    print(f"\nDone. Open inspect_output.txt for full results.")
    print(f"Look at STEP 4 and STEP 7 first — they show exactly")
    print(f"what type 'Stop' is and which method finds it.\n")
