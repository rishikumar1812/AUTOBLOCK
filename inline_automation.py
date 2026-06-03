"""
inline_automation.py  —  Main PC
Automates InLine_Pro GUI after a DL stop signal is received.

Window finding strategy:
  - Connects by EXE process name (InLine_Pro.exe) — immune to
    version string changes in title (InLine_Pro_Version3.2xyz etc.)
  - Then finds the main window using title_re partial regex match
    against the configured window_title keyword
  - set_focus() called only on InLine_Pro window — all other open
    apps are completely ignored

Sequence per stop:
  1. Edit Data.ini  — uncheck the stopped DL building
  2. Connect to InLine_Pro.exe process
  3. Find main window by partial title regex
  4. Stop → SetUp → OK → Start → Yes → OK
     (3 second wait between each step)

Install: pip install pywinauto pyautogui Pillow
"""

import re
import time
import logging

from pywinauto import Application, Desktop
from pywinauto.exceptions import (
    ElementNotFoundError,
    ElementAmbiguousError,
)
from pywinauto.timings import TimeoutError as PWTimeoutError

from config_loader import get_config
from ini_editor import uncheck_dl

logger = logging.getLogger("inline_automation")


# =========================================================
# Config accessors
# =========================================================
def _exe_name()       -> str: return get_config()["app"]["exe_name"]
def _window_keyword() -> str: return get_config()["app"]["window_title"]
def _step_wait()      -> int: return int(get_config()["automation"]["step_wait_sec"])
def _max_wait()       -> int: return int(get_config()["automation"]["max_wait_sec"])
def _retry_attempts() -> int: return int(get_config()["automation"]["retry_attempts"])


# =========================================================
# Connect to InLine_Pro by EXE process name
#
# Why process name instead of title:
#   Title can be "InLine_Pro_Version3.2xyz" or any version —
#   it changes with updates.
#   EXE name (InLine_Pro.exe) never changes.
#
# Returns the Application object connected to InLine_Pro.exe
# Raises RuntimeError if process not found.
# =========================================================
def _connect_to_app() -> Application:
    exe = _exe_name()
    try:
        app = Application(backend="uia").connect(
            path=exe,
            timeout=10,
        )
        logger.info(f"[automation] Connected to process: {exe}")
        return app
    except Exception:
        # Fallback: find by iterating running processes
        try:
            app = Application(backend="uia").connect(
                title_re=f".*{re.escape(_window_keyword())}.*",
                timeout=10,
            )
            logger.info(
                f"[automation] Connected via title_re fallback: "
                f"*{_window_keyword()}*"
            )
            return app
        except Exception as e:
            raise RuntimeError(
                f"[automation] Cannot find InLine_Pro — "
                f"process '{exe}' not running. Error: {e}"
            )


# =========================================================
# Get the main InLine_Pro window from the app
#
# Uses title_re partial match so version string in title
# (InLine_Pro_Version3.2xyz) is handled automatically.
# Other open apps are never touched — we target only the
# process we connected to above.
# =========================================================
def _get_main_window(app: Application):
    keyword = _window_keyword()
    try:
        # Partial title regex — matches any version suffix
        window = app.window(title_re=f".*{re.escape(keyword)}.*")
        window.wait("visible", timeout=_max_wait())
        logger.info(
            f"[automation] Main window found: "
            f"'{window.window_text()}'"
        )
        return window
    except PWTimeoutError:
        raise RuntimeError(
            f"[automation] Main window matching '*{keyword}*' "
            f"not visible after {_max_wait()}s"
        )
    except ElementNotFoundError:
        raise RuntimeError(
            f"[automation] No window matching '*{keyword}*' found "
            f"in InLine_Pro process"
        )


# =========================================================
# Click a named button in a specific window
#
# Does NOT call set_focus() on the whole screen —
# operates directly on the InLine_Pro window element.
# Other apps stay untouched.
# =========================================================
def _click_button(window, button_name: str) -> None:
    wait = _step_wait()
    logger.info(
        f"[automation] Waiting {wait}s → clicking '{button_name}'"
    )
    time.sleep(wait)

    try:
        btn = window.child_window(
            title=button_name,
            control_type="Button",
        )
        btn.wait("visible enabled", timeout=_max_wait())
        btn.click_input()
        logger.info(f"[automation] Clicked '{button_name}'")
    except PWTimeoutError:
        raise RuntimeError(
            f"[automation] Button '{button_name}' not ready "
            f"after {_max_wait()}s"
        )
    except ElementNotFoundError:
        raise RuntimeError(
            f"[automation] Button '{button_name}' not found in window"
        )


# =========================================================
# Click a button in the topmost dialog of InLine_Pro
#
# app.top_window() targets only the InLine_Pro process —
# dialogs from other apps are never matched.
# =========================================================
def _click_dialog_button(app: Application, button_name: str) -> None:
    wait = _step_wait()
    logger.info(
        f"[automation] Waiting {wait}s → clicking dialog '{button_name}'"
    )
    time.sleep(wait)

    try:
        # top_window() returns the foremost window of THIS process only
        dialog = app.top_window()
        btn    = dialog.child_window(
            title=button_name,
            control_type="Button",
        )
        btn.wait("visible enabled", timeout=_max_wait())
        btn.click_input()
        logger.info(f"[automation] Clicked dialog '{button_name}'")
    except PWTimeoutError:
        raise RuntimeError(
            f"[automation] Dialog button '{button_name}' not ready "
            f"after {_max_wait()}s"
        )
    except ElementNotFoundError:
        raise RuntimeError(
            f"[automation] Dialog button '{button_name}' not found"
        )


# =========================================================
# Full automation sequence for one DL stop
#
# Steps:
#   1. Uncheck building in Data.ini
#   2. Connect to InLine_Pro.exe (by process name)
#   3. Find main window (by partial title regex)
#   4. Stop → SetUp → OK (dialog) → Start → Yes (dialog) → OK (dialog)
#
# Retries up to retry_attempts on any step failure.
# Returns True on success, False after all retries exhausted.
# =========================================================
def run_stop_sequence(dl_name: str) -> bool:
    retries = _retry_attempts()

    for attempt in range(1, retries + 1):
        logger.info(
            f"[automation] {dl_name} — attempt {attempt}/{retries}"
        )
        try:
            # ── Step 1: Edit Data.ini ─────────────────────
            updated = uncheck_dl(dl_name)
            if updated:
                logger.info(
                    f"[automation] {dl_name} — Data.ini updated"
                )
            else:
                logger.warning(
                    f"[automation] {dl_name} — Data.ini not updated "
                    f"(already unchecked or file error)"
                )

            # ── Step 2: Connect by EXE process name ───────
            app = _connect_to_app()

            # ── Step 3: Find main window by partial title ──
            window = _get_main_window(app)

            # ── Step 4: Stop ──────────────────────────────
            _click_button(window, "Stop")

            # ── Step 5: SetUp ─────────────────────────────
            _click_button(window, "SetUp")

            # ── Step 6: OK (SetUp confirmation dialog) ────
            _click_dialog_button(app, "OK")

            # ── Step 7: Start ─────────────────────────────
            _click_button(window, "Start")

            # ── Step 8: Yes (Start confirmation dialog) ───
            _click_dialog_button(app, "Yes")

            # ── Step 9: OK (final dialog) ─────────────────
            _click_dialog_button(app, "OK")

            logger.info(
                f"[automation] {dl_name} — sequence completed OK"
            )
            return True

        except RuntimeError as e:
            logger.error(
                f"[automation] {dl_name} — attempt {attempt} failed: {e}"
            )
            if attempt < retries:
                logger.info(
                    f"[automation] {dl_name} — "
                    f"retrying in {_step_wait()}s..."
                )
                time.sleep(_step_wait())

        except Exception as e:
            logger.error(
                f"[automation] {dl_name} — "
                f"unexpected error attempt {attempt}: {e}"
            )
            if attempt < retries:
                time.sleep(_step_wait())

    logger.error(
        f"[automation] {dl_name} — all {retries} attempts failed. "
        f"Manual intervention required."
    )
    return False
