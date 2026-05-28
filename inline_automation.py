"""
inline_automation.py  —  Main PC
Automates InLine_Pro GUI after a DL stop signal is received.

Sequence per stop:
  1. Edit Data.ini — uncheck the stopped DL building
  2. Attach to InLine_Pro window (already open)
  3. Click Stop
  4. Wait 3s → Click SetUp
  5. Wait 3s → Click OK
  6. Wait 3s → Click Start
  7. Wait 3s → Click Yes
  8. Wait 3s → Click OK

Uses pywinauto (Windows UI Automation) — reliable, title-based,
no screen coordinate dependency.

Install: pip install pywinauto
"""

import time
import logging

from pywinauto import Application, findwindows
from pywinauto.exceptions import (
    ElementNotFoundError,
    ElementAmbiguousError,
    TimeoutError as PWTimeoutError,
)

from config_loader import get_config
from ini_editor import uncheck_dl

logger = logging.getLogger("inline_automation")


# =========================================================
# Config accessors
# =========================================================
def _window_title()   -> str: return get_config()["app"]["window_title"]
def _step_wait()      -> int: return int(get_config()["automation"]["step_wait_sec"])
def _retry_attempts() -> int: return int(get_config()["automation"]["retry_attempts"])


# =========================================================
# Attach to already-running InLine_Pro window
# =========================================================
def _get_app() -> Application:
    """
    Connect to the already-running InLine_Pro process.
    Raises RuntimeError if window not found.
    """
    title = _window_title()
    try:
        app = Application(backend="uia").connect(title=title, timeout=10)
        logger.info(f"[automation] Connected to window: '{title}'")
        return app
    except (ElementNotFoundError, PWTimeoutError):
        raise RuntimeError(
            f"[automation] InLine_Pro window not found: '{title}'. "
            f"Is the application open?"
        )


# =========================================================
# Click a button by name inside the main window
# =========================================================
def _click_button(window, button_name: str) -> None:
    """
    Find and click a button by its name/title in the window.
    Waits step_wait_sec before clicking.
    Raises RuntimeError if button not found.
    """
    wait = _step_wait()
    logger.info(f"[automation] Waiting {wait}s before clicking '{button_name}'")
    time.sleep(wait)

    try:
        btn = window.child_window(title=button_name, control_type="Button")
        btn.wait("visible enabled", timeout=10)
        btn.click_input()
        logger.info(f"[automation] Clicked '{button_name}'")
    except (ElementNotFoundError, PWTimeoutError) as e:
        raise RuntimeError(
            f"[automation] Button '{button_name}' not found or not ready: {e}"
        )


# =========================================================
# Handle popup dialogs (OK / Yes)
# =========================================================
def _click_dialog_button(app, button_name: str) -> None:
    """
    Find and click a button in any active dialog/popup.
    Covers OK, Yes confirmations that appear as child windows.
    Waits step_wait_sec before clicking.
    """
    wait = _step_wait()
    logger.info(
        f"[automation] Waiting {wait}s before clicking dialog '{button_name}'"
    )
    time.sleep(wait)

    try:
        # Find the topmost dialog window
        dialog = app.top_window()
        btn    = dialog.child_window(title=button_name, control_type="Button")
        btn.wait("visible enabled", timeout=10)
        btn.click_input()
        logger.info(f"[automation] Clicked dialog '{button_name}'")
    except (ElementNotFoundError, PWTimeoutError) as e:
        raise RuntimeError(
            f"[automation] Dialog button '{button_name}' not found: {e}"
        )


# =========================================================
# Full automation sequence for one DL stop
# =========================================================
def run_stop_sequence(dl_name: str) -> bool:
    """
    Full stop sequence for a DL:
      1. Uncheck building in Data.ini
      2. Attach to InLine_Pro window
      3. Stop → SetUp → OK → Start → Yes → OK

    Retries up to retry_attempts times on failure.

    Returns:
        True  — sequence completed successfully
        False — all retries failed
    """
    retries = _retry_attempts()

    for attempt in range(1, retries + 1):
        logger.info(
            f"[automation] {dl_name} — starting stop sequence "
            f"(attempt {attempt}/{retries})"
        )
        try:
            # ── Step 1: Edit Data.ini ─────────────────────
            updated = uncheck_dl(dl_name)
            if updated:
                logger.info(
                    f"[automation] {dl_name} — Data.ini updated successfully"
                )
            else:
                logger.warning(
                    f"[automation] {dl_name} — Data.ini not updated "
                    f"(already unchecked or error)"
                )

            # ── Step 2: Attach to InLine_Pro ──────────────
            app    = _get_app()
            window = app.window(title=_window_title())
            window.set_focus()
            logger.info(f"[automation] {dl_name} — window focused")

            # ── Step 3: Stop ──────────────────────────────
            _click_button(window, "Stop")

            # ── Step 4: SetUp ─────────────────────────────
            _click_button(window, "SetUp")

            # ── Step 5: OK (SetUp dialog) ─────────────────
            _click_dialog_button(app, "OK")

            # ── Step 6: Start ─────────────────────────────
            _click_button(window, "Start")

            # ── Step 7: Yes (Start confirmation) ──────────
            _click_dialog_button(app, "Yes")

            # ── Step 8: OK (final confirmation) ───────────
            _click_dialog_button(app, "OK")

            logger.info(
                f"[automation] {dl_name} — stop sequence completed successfully"
            )
            return True

        except RuntimeError as e:
            logger.error(
                f"[automation] {dl_name} — attempt {attempt} failed: {e}"
            )
            if attempt < retries:
                logger.info(
                    f"[automation] {dl_name} — retrying in {_step_wait()}s..."
                )
                time.sleep(_step_wait())

        except Exception as e:
            logger.error(
                f"[automation] {dl_name} — unexpected error on attempt {attempt}: {e}"
            )
            if attempt < retries:
                time.sleep(_step_wait())

    logger.error(
        f"[automation] {dl_name} — all {retries} attempts failed. "
        f"Manual intervention required."
    )
    return False
