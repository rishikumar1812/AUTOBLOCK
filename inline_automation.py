import re
import sys
import time
import logging

# pywinauto is Windows-only — guard import so the file can at least
# be imported on Mac/Linux for testing non-automation code paths.
try:
    from pywinauto import Application, Desktop
    from pywinauto.findwindows import (
        ElementNotFoundError,
        ElementAmbiguousError,
    )
    from pywinauto.timings import TimeoutError as PWTimeoutError
    _PYWINAUTO_AVAILABLE = True
except (ImportError, Exception):
    _PYWINAUTO_AVAILABLE = False
    # Stub classes so the rest of the file loads without error
    class Application:
        def __init__(self, *a, **k): pass
    class Desktop:
        pass
    class ElementNotFoundError(Exception): pass
    class ElementAmbiguousError(Exception): pass
    class PWTimeoutError(Exception): pass
    if sys.platform != "win32":
        logging.getLogger("Process").warning(
            "[inline_automation] pywinauto not available on this platform. "
            "Automation will not work — run on Windows Main PC."
        )

from config_loader import get_config
from ini_editor import uncheck_dl, uncheck_ft

# =========================================================
# Use the SAME logger name ("Process") that main_pc_popup.py
# configures with a DailyFileHandler writing to
# Process_YYYY-MM-DD.log — every pywinauto automation step lands
# in that single dedicated automation log, separate from
# connection_status_YYYY-MM-DD.log (HELLO/connect/disconnect).
# main_pc_popup.log has been retired — no general catch-all log.
# =========================================================
logger=logging.getLogger("Process")

# config access
def _exe_name()->str:
    return get_config()['app']['exe_name']
def _window_keyword()->str:
    return get_config()['app']['window_title']

def _step_wait()->int:
    return int(get_config()['automation']['step_wait_sec'])
def _max_wait()->int:
    return int(get_config()["automation"]['max_wait_sec'])
def _retry_attempts()->int:
    return int(get_config()['automation']['retry_attempts'])


# =========================================================
# Building occupancy check — config access
# =========================================================

# Hardcoded fallback used when config.json has no building_check
# section yet (e.g. old config.json not updated after code deploy).
# Geometry values confirmed via live diagnostic run on
# InLine_Pro_Ver 3.1.8.01 — update config.json to override.
_BC_DEFAULTS = {
    "enabled": True,
    "front_label_left": 143,
    "front_value_left": 200,
    "rear_label_left": 436,
    "rear_value_left": 496,
    "row_top_tolerance_px": 5,
    "ready_text": "Wait",
    "poll_interval_sec": 5,
    "max_wait_sec": 600,
}

def _bc_cfg() -> dict:
    cfg = get_config()
    if "building_check" not in cfg:
        logger.warning(
            "[automation] 'building_check' section missing from config.json "
            "— using hardcoded defaults. Add it to config.json to customise."
        )
        return dict(_BC_DEFAULTS)
    # Merge: any key missing from config.json falls back to the default
    result = dict(_BC_DEFAULTS)
    result.update(cfg["building_check"])
    return result

def _bc_enabled() -> bool:
    return bool(_bc_cfg().get('enabled', True))


# =========================================================
# DL name -> (rack, building_num)
#
# Mirrors ini_editor.dl_to_ini_key() so the rack-split logic
# lives in one place conceptually, even though it's duplicated
# here because ini_editor maps to Data.ini section names
# (RACK1/RACK2) while this maps to screen side (front/rear).
# Both follow the same DL01-10 -> rack1(front), DL11-20 -> rack2(rear)
# split — keep these in sync if that split ever changes.
# =========================================================
def dl_to_rack_building(dl_name: str) -> tuple:
    """
    Maps a DL name to (rack, building_num) for screen lookup.

    Handles all real-world formats sent by the DL PC:
      'DL6'  / 'DL06'  / 'DL 6'  -> ('front', 6)
      'DL13' / 'DL013' / 'DL 13' -> ('rear',  3)

    building_num is always 1-9 (matches Building 1-9 on screen).
    Raises ValueError on bad input.
    """
    name = dl_name.strip()
    if not name.upper().startswith("DL"):
        raise ValueError(
            f"Invalid DL name (must start with 'DL'): {dl_name!r}"
        )
    try:
        dl_num = int(name[2:].strip())
    except (ValueError, IndexError):
        raise ValueError(
            f"Invalid DL name (non-numeric suffix): {dl_name!r}"
        )

    if not (1 <= dl_num <= 20):
        raise ValueError(
            f"DL number out of range 1-20: {dl_name!r} (parsed as {dl_num})"
        )

    if dl_num <= 10:
        rack = "front"
        building_num = dl_num
    else:
        rack = "rear"
        building_num = dl_num - 10

    # Screen only has Building 1-9. DL10->front-10 and DL20->rear-10
    # don't exist on screen — catch this early with a clear message
    # rather than silently searching for "Building 10" and failing.
    if building_num > 9:
        raise ValueError(
            f"{dl_name!r} maps to Building {building_num} on {rack} rack "
            f"but screen only has Buildings 1-9 "
            f"(DL10/DL20 are not valid building positions)."
        )

    return rack, building_num


def _connect_to_app()->Application:
    exe=_exe_name()
    logger.info(f"[automation] STEP: Connect to process '{exe}' — attempting...")
    try:
        app=Application(backend='uia').connect(path=exe,timeout=10,)
        logger.info(f"[automation] STEP: Connect to process '{exe}' — OK")
        return app
    except Exception as e1:
        logger.warning(f"[automation] STEP: Connect by process name FAILED — {e1}")
        logger.info(f"[automation] STEP: Connect by title_re fallback — attempting...")
        try:
            app=Application(backend='uia').connect(title_re=f".*{re.escape(_window_keyword())}.*",timeout=10,)
            logger.info(f"[automation] STEP: Connect by title_re fallback — OK "
                        f"(*{_window_keyword()}*)")
            return app
        except Exception as e2:
            logger.error(f"[automation] STEP: Connect by title_re fallback — FAILED: {e2}")
            raise RuntimeError(
                f"[automation] Cannot find InLine_Pro — "
                f"process '{exe}' is not running. Error: {e2}"
            )


def _get_main_window(app:Application):
    keyword=_window_keyword()
    logger.info(f"[automation] STEP: Find main window matching '*{keyword}*' — attempting...")
    try:
        window=app.window(title_re=f".*{re.escape(_window_keyword())}.*")
        window.wait("visible",timeout=_max_wait())
        logger.info(
            f"[automation] STEP: Find main window — OK "
            f"(title='{window.window_text()}')"
        )
        return window
    except PWTimeoutError:
        logger.error(
            f"[automation] STEP: Find main window — FAILED "
            f"(not visible after {_max_wait()}s)"
        )
        raise RuntimeError(
            f"[automation] Main window matching '*{keyword}*' "
            f"not visible after {_max_wait()}s"
        )
    except ElementNotFoundError:
        logger.error(
            f"[automation] STEP: Find main window — FAILED "
            f"(no window matching '*{keyword}*')"
        )
        raise RuntimeError(
            f"[automation] No window matching '*{keyword}*' found "
            f"in InLine_Pro process"
        )


# =========================================================
# =========================================================
# Building occupancy check — core read logic
# =========================================================

def _safe_text(ctrl) -> str:
    """
    Safely read window_text() from a pywinauto control.
    On UIA backend, window_text() can return a bound method
    instead of a string when the UIA element goes stale mid-walk
    (window redraws during descendants() iteration).
    str() cast defends against this — returns empty string on error.
    """
    try:
        val = ctrl.window_text()
        return str(val).strip() if val is not None else ""
    except Exception:
        return ""


def _safe_value(ctrl) -> str:
    """
    Safely read get_value() from a pywinauto Edit control.
    Same stale-element guard as _safe_text.
    Strips internal letter-spacing (e.g. 'W a i t' -> 'Wait').
    Returns empty string on any failure.
    """
    try:
        val = ctrl.get_value()
        if val is None:
            return ""
        return str(val).replace(" ", "").strip()
    except Exception:
        return ""


def _find_building_label(window, rack: str, building_num: int):
    """
    Find the Text control for 'Building {N}' on the correct side.
    Two matches exist window-wide (front+rear) for the same title —
    disambiguate by the label's known left x-position.
    Uses _safe_text() so stale UIA elements don't crash the walk.
    """
    cfg = _bc_cfg()
    target_left = cfg['front_label_left'] if rack == "front" else cfg['rear_label_left']
    tol = cfg['row_top_tolerance_px']
    title = f"Building {building_num}"

    candidates = []
    for c in window.descendants():
        try:
            if _safe_text(c) == title:
                candidates.append(c)
        except Exception:
            continue

    # Log actual left coords of every candidate so we can see
    # exactly what value the app is reporting vs what we expect.
    # This fires on EVERY failed poll — visible in the log.
    candidate_lefts = []
    for c in candidates:
        try:
            left = c.rectangle().left
            candidate_lefts.append(left)
            if abs(left - target_left) <= max(tol, 10):
                return c
        except Exception:
            candidate_lefts.append("err")
            continue

    raise RuntimeError(
        f"[automation] Could not find '{title}' label on {rack} side "
        f"(expected near left={target_left}, tolerance={max(tol,10)}px, "
        f"found {len(candidates)} candidate(s) with actual lefts={candidate_lefts}. "
        f"If lefts are consistent, update config.json building_check."
        f"{rack}_label_left to match."
    )


def _read_building_status(window, rack: str, building_num: int) -> str:
    """
    Returns the live status text for the given rack/building,
    e.g. 'Wait', 'Down', 'NotUse', whitespace-stripped.
    Raises RuntimeError if the label or paired value can't be found.
    Uses _safe_value() so stale UIA elements don't crash the read.
    """
    cfg = _bc_cfg()
    tol = cfg['row_top_tolerance_px']
    value_left = cfg['front_value_left'] if rack == "front" else cfg['rear_value_left']

    label = _find_building_label(window, rack, building_num)
    label_top = label.rectangle().top

    for c in window.descendants(control_type="Edit"):
        try:
            r = c.rectangle()
            if abs(r.top - label_top) <= tol and abs(r.left - value_left) <= max(tol, 10):
                return _safe_value(c)
        except Exception:
            continue

    raise RuntimeError(
        f"[automation] Found 'Building {building_num}' label on {rack} side "
        f"(top={label_top}) but no matching value Edit control at "
        f"left~{value_left}"
    )



def wait_for_building_clear(dl_name: str, app: Application, window) -> bool:
    """
    Polls the live HMI screen until the target Building shows the
    'ready' status (default 'Wait', meaning no board currently
    occupies that position), or until max_wait_sec is exceeded.

    This MUST run before Data.ini is edited / before the click
    sequence starts. Proceeding while the building still shows
    'Down' (board present) would uncheck/stop a position with a
    physical PCB still sitting in it — that board would never be
    carried forward and would stay stuck, causing a production loss
    at full line capacity.

    Returns:
        True  — building reached ready state, safe to proceed
        False — timed out, building never cleared; caller must NOT
                proceed with the Data.ini edit or click sequence
    """
    cfg = _bc_cfg()
    if not cfg.get('enabled', True):
        logger.warning(
            f"[automation] {dl_name} — building_check.enabled=false in "
            f"config.json, SKIPPING occupancy check (old behavior)"
        )
        return True

    try:
        rack, building_num = dl_to_rack_building(dl_name)
    except ValueError as e:
        logger.error(f"[automation] {dl_name} — {e}")
        raise RuntimeError(str(e))

    ready_text = cfg['ready_text'].replace(" ", "").strip()
    poll_interval = int(cfg['poll_interval_sec'])
    max_wait = int(cfg['max_wait_sec'])

    logger.info(
        f"[automation] {dl_name} — STEP 0/9: Wait for Building {building_num} "
        f"({rack} rack) to show '{ready_text}' before proceeding "
        f"(max wait {max_wait}s, poll every {poll_interval}s)"
    )

    start = time.time()
    last_seen = None
    while True:
        try:
            status = _read_building_status(window, rack, building_num)
        except RuntimeError as e:
            # Couldn't read the control at all this poll — log and retry
            # rather than aborting immediately, in case it's a transient
            # UIA hiccup (window briefly redrawing, etc).
            logger.warning(f"[automation] {dl_name} — STEP 0/9: read failed this poll: {e}")
            status = None

        if status is not None and status != last_seen:
            logger.info(
                f"[automation] {dl_name} — STEP 0/9: Building {building_num} "
                f"({rack}) currently shows '{status}'"
            )
            last_seen = status

        if status == ready_text:
            logger.info(
                f"[automation] {dl_name} — STEP 0/9: Building {building_num} "
                f"({rack}) is '{ready_text}' — OK, proceeding"
            )
            return True

        elapsed = time.time() - start
        if elapsed >= max_wait:
            logger.error(
                f"[automation] {dl_name} — STEP 0/9: TIMED OUT after {max_wait}s "
                f"waiting for Building {building_num} ({rack}) to clear "
                f"(last seen status: '{last_seen}'). NOT proceeding — "
                f"board may still be physically present. Manual check needed."
            )
            return False

        time.sleep(poll_interval)


def _click_button(window,button_name:str)->None:
    wait=_step_wait()
    logger.info(f"[automation] STEP: Click '{button_name}' — waiting {wait}s before click")
    time.sleep(wait)

    ctrl=None
    last_err=None
    attempts = [
        {"title":button_name,"control_type":"Button"},
        {"title_re":f".*{re.escape(button_name)}.*","control_type":"Button"},
        {"title":button_name},
    ]
    for n, kwargs in enumerate(attempts, start=1):
        logger.info(f"[automation] STEP: Click '{button_name}' — trying method {n}/3: {kwargs}")
        try:
            candidate=window.child_window(**kwargs)
            candidate.wait("visible enabled",timeout=3)
            ctrl=candidate
            logger.info(
                f"[automation] STEP: Click '{button_name}' — found via method {n} ({kwargs})"
            )
            break
        except Exception as e:
            last_err=e
            logger.warning(
                f"[automation] STEP: Click '{button_name}' — method {n} failed: {e}"
            )
            continue

    if ctrl is None:
        logger.error(
            f"[automation] STEP: Click '{button_name}' — FAILED, button not found by any method. "
            f"Last error: {last_err}"
        )
        raise RuntimeError(
            f"[automation] Button '{button_name}' not found "
            f"by any method. Last error: {last_err}"
        )

    try:
        ctrl.click_input()
        logger.info(f"[automation] STEP: Click '{button_name}' — OK, click executed")
    except Exception as e:
        logger.error(f"[automation] STEP: Click '{button_name}' — FAILED on click_input(): {e}")
        raise RuntimeError(
            f"[automation] Click failed on '{button_name}': {e}"
        )


def _click_dialog_button(app:Application,button_name:str)->None:
    wait=_step_wait()
    logger.info(f"[automation] STEP: Click dialog '{button_name}' — waiting {wait}s before click")
    time.sleep(wait)

    dialog=app.top_window()
    ctrl=None
    last_err=None
    attempts = [
        {"title":button_name,"control_type":"Button"},
        {"title_re":f".*{re.escape(button_name)}.*","control_type":"Button"},
        {"title":button_name},
    ]
    for n, kwargs in enumerate(attempts, start=1):
        logger.info(f"[automation] STEP: Click dialog '{button_name}' — trying method {n}/3: {kwargs}")
        try:
            candidate=dialog.child_window(**kwargs)
            candidate.wait("visible enabled",timeout=3)
            ctrl=candidate
            logger.info(
                f"[automation] STEP: Click dialog '{button_name}' — found via method {n} ({kwargs})"
            )
            break
        except Exception as e:
            last_err=e
            logger.warning(
                f"[automation] STEP: Click dialog '{button_name}' — method {n} failed: {e}"
            )
            continue

    if ctrl is None:
        logger.error(
            f"[automation] STEP: Click dialog '{button_name}' — FAILED, button not found by any method. "
            f"Last error: {last_err}"
        )
        raise RuntimeError(
            f"[automation] Dialog button '{button_name}' not found "
            f"by any method. Last error: {last_err}"
        )

    try:
        ctrl.click_input()
        logger.info(f"[automation] STEP: Click dialog '{button_name}' — OK, click executed")
    except Exception as e:
        logger.error(f"[automation] STEP: Click dialog '{button_name}' — FAILED on click_input(): {e}")
        raise RuntimeError(
            f"[automation] Click failed on dialog '{button_name}': {e}"
        )


# =========================================================
# FT task key parser
# task_key format: "FT1_FRONT_Function 1"
# =========================================================
def _is_ft_task(task_key: str) -> bool:
    return task_key.upper().startswith("F")


def _parse_ft_task(task_key: str) -> tuple:
    """
    Parse new format 'FT_F1_front_Function 1' into (rack, function_num).
    Format: FT_{ft_id}_{rack}_{function_label}
    e.g. 'FT_F1_front_Function 1' → ('front', 1)
         'FT_R3_rear_Function 3'  → ('rear',  3)
    """
    # "FT_F1_front_Function 1" → ["FT", "F1", "front", "Function 1"]
    parts = task_key.split("_", 3)
    print(parts)
    if len(parts) < 4:
        raise ValueError(f"Invalid FT task key: {task_key!r}")
    rack         = parts[2].lower()    # 'front' or 'rear'
    function_str = parts[3]            # 'Function 1'
    try:
        fn_num = int(function_str.split()[-1])
    except (ValueError, IndexError):
        raise ValueError(
            f"Cannot parse function number from {function_str!r} "
            f"in task key {task_key!r}"
        )
    return rack, fn_num


def _find_function_label(window, rack: str, fn_num: int):
    """
    Find the Text control for 'Function {N}' on the correct side.
    Same approach as _find_building_label — disambiguate by x-position.
    Front Rack function labels sit near left=143,
    Rear Rack function labels near left=436.
    """
    cfg        = _bc_cfg()
    target_left = (cfg['front_label_left'] if rack == "front"
                   else cfg['rear_label_left'])
    tol   = cfg['row_top_tolerance_px']
    title = f"Function {fn_num}"

    candidates = []
    for c in window.descendants():
        try:
            if _safe_text(c) == title:
                candidates.append(c)
        except Exception:
            continue

    candidate_lefts = []
    for c in candidates:
        try:
            left = c.rectangle().left
            candidate_lefts.append(left)
            if abs(left - target_left) <= max(tol, 10):
                return c
        except Exception:
            candidate_lefts.append("err")
            continue

    raise RuntimeError(
        f"[automation] Could not find '{title}' label on {rack} side "
        f"(expected near left={target_left}, tolerance={max(tol,10)}px, "
        f"found {len(candidates)} candidate(s) with "
        f"actual lefts={candidate_lefts})"
    )


def _read_function_status(window, rack: str, fn_num: int) -> str:
    """
    Read live status text for Function N on the given rack.
    Same Edit-control pattern as _read_building_status.
    """
    cfg        = _bc_cfg()
    tol        = cfg['row_top_tolerance_px']
    value_left = (cfg['front_value_left'] if rack == "front"
                  else cfg['rear_value_left'])

    label     = _find_function_label(window, rack, fn_num)
    label_top = label.rectangle().top

    for c in window.descendants(control_type="Edit"):
        try:
            r = c.rectangle()
            if (abs(r.top - label_top) <= tol and
                    abs(r.left - value_left) <= max(tol, 10)):
                return _safe_value(c)
        except Exception:
            continue

    raise RuntimeError(
        f"[automation] Found 'Function {fn_num}' label on {rack} side "
        f"(top={label_top}) but no matching value Edit at left~{value_left}"
    )


def wait_for_function_clear(task_key: str, app, window) -> bool:
    """
    Polls the HMI screen until Function N on the correct rack
    shows 'Wait' — same gate logic as wait_for_building_clear
    but for FT PC signals targeting Function rows.
    """
    cfg = _bc_cfg()
    if not cfg.get('enabled', True):
        logger.warning(
            f"[automation] {task_key} — building_check disabled, "
            f"skipping function check")
        return True

    try:
        rack, fn_num = _parse_ft_task(task_key)
    except ValueError as e:
        logger.error(f"[automation] {task_key} — {e}")
        raise RuntimeError(str(e))

    ready_text   = cfg['ready_text'].replace(" ", "").strip()
    poll_interval = int(cfg['poll_interval_sec'])
    max_wait     = int(cfg['max_wait_sec'])

    logger.info(
        f"[automation] {task_key} — STEP 0/9: Wait for Function {fn_num} "
        f"({rack} rack) to show '{ready_text}' "
        f"(max wait {max_wait}s, poll every {poll_interval}s)"
    )

    start    = time.time()
    last_seen = None
    while True:
        try:
            status = _read_function_status(window, rack, fn_num)
        except RuntimeError as e:
            logger.warning(
                f"[automation] {task_key} — STEP 0/9: read failed: {e}")
            status = None

        if status is not None and status != last_seen:
            logger.info(
                f"[automation] {task_key} — STEP 0/9: "
                f"Function {fn_num} ({rack}) currently shows '{status}'"
            )
            last_seen = status

        if status == ready_text:
            logger.info(
                f"[automation] {task_key} — STEP 0/9: "
                f"Function {fn_num} ({rack}) is '{ready_text}' — OK, proceeding"
            )
            return True

        if time.time() - start >= max_wait:
            logger.error(
                f"[automation] {task_key} — STEP 0/9: TIMED OUT after "
                f"{max_wait}s waiting for Function {fn_num} ({rack}) to clear "
                f"(last seen: '{last_seen}'). NOT proceeding."
            )
            return False

        time.sleep(poll_interval)


def run_stop_sequence(dl_name:str)->bool:
    """
    Full stop sequence for a DL or FT task.
    Accepts either:
      DL task:  dl_name = 'DL06'
      FT task:  dl_name = 'FT1_FRONT_Function 1'

    0. Wait for the target Building/Function to show 'Wait'
    1. Uncheck building in Data.ini  (DL only — FT skips ini edit)
    2. Attach to InLine_Pro window
    3. STOP → SETUP → OK → START → Yes → OK
    Retries up to retry_attempts times on failure.
    Every step is logged to main_pc_popup_YYYY-MM-DD.log so an
    engineer can find the EXACT step that failed.
    Returns:
        True  — sequence completed successfully
        False — all retries failed, OR Step 0 timed out waiting
                for the building to clear (no retries are spent
                on a Step-0 timeout — that's not a transient error,
                retrying immediately won't change a board still
                being physically present)
    """
    if not _PYWINAUTO_AVAILABLE:
        logger.error(
            f"[automation] {dl_name} — pywinauto not available on this "
            f"platform. Run on Windows Main PC."
        )
        return False

    retries=_retry_attempts()

    for attempt in range(1,retries+1):
        logger.info(f"[automation] {'='*60}")
        logger.info(
            f"[automation] {dl_name} — STARTING stop sequence "
            f"(attempt {attempt}/{retries})"
        )
        logger.info(f"[automation] {'='*60}")
        try:
            is_ft = _is_ft_task(dl_name)

            logger.info(
                f"[automation] STEP 0/9: Connect to InLine_Pro "
                f"({'FT' if is_ft else 'DL'} task: {dl_name})"
            )
            app    = _connect_to_app()
            window = _get_main_window(app)

            # Step 0 — wait for the target slot to clear
            if is_ft:
                logger.info(
                    f"[automation] STEP 0/9: Wait for Function to clear (FT task)")
                cleared = wait_for_function_clear(dl_name, app, window)
            else:
                logger.info(
                    f"[automation] STEP 0/9: Wait for Building to clear (DL task)")
                cleared = wait_for_building_clear(dl_name, app, window)

            if not cleared:
                logger.error(
                    f"[automation] {dl_name} — STEP 0/9: slot never cleared. "
                    f"ABORTING — no Data.ini edit, no clicks."
                )
                return False

            # Step 1 — ini edit (DL only)
            if not is_ft:
                logger.info(f"[automation] STEP 1/9: Edit Data.ini — uncheck {dl_name}")
                updated = uncheck_dl(dl_name)
                if updated:
                    logger.info(
                        f"[automation] STEP 1/9: Edit Data.ini — OK")
                else:
                    logger.warning(
                        f"[automation] STEP 1/9: Edit Data.ini — SKIPPED "
                        f"(already unchecked or error)")
            else:
                # FT task — uncheck FUNCTION row in Data.ini
                try:
                    rack, fn_num = _parse_ft_task(dl_name)
                    updated = uncheck_ft(fn_num, rack)
                    if updated:
                        logger.info(
                            f"[automation] STEP 1/9: Data.ini — OK, "
                            f"FUNCTION{fn_num} ({rack}) set to NOT_CHECK"
                        )
                    else:
                        logger.warning(
                            f"[automation] STEP 1/9: Data.ini — SKIPPED "
                            f"(FUNCTION{fn_num} already NOT_CHECK or error)"
                        )
                except Exception as e:
                    logger.error(
                        f"[automation] STEP 1/9: Data.ini FT edit failed: {e}"
                    )

            logger.info(f"[automation] STEP 4/9: Click STOP")
            _click_button(window,"STOP")

            logger.info(f"[automation] STEP 5/9: Click SETUP")
            _click_button(window,'SETUP')

            logger.info(f"[automation] STEP 6/9: Click OK (setup dialog)")
            _click_dialog_button(app,"OK")

            logger.info(f"[automation] STEP 7/9: Click START")
            _click_button(window,"START")

            logger.info(f"[automation] STEP 8/9: Click Yes (start confirmation)")
            _click_dialog_button(app,'Yes')

            logger.info(f"[automation] STEP 9/9: Click OK (final dialog)")
            _click_dialog_button(app,"OK")

            logger.info(
                f"[automation] {dl_name} — ALL STEPS COMPLETED SUCCESSFULLY "
                f"(attempt {attempt}/{retries})"
            )
            logger.info(f"[automation] {'='*60}")
            return True

        except RuntimeError as e:
            logger.error(
                f"[automation] {dl_name} — attempt {attempt} FAILED at the step above: {e}"
            )
            if attempt<retries:
                logger.info(
                    f"[automation] {dl_name} — retrying in {_step_wait()}s..."
                )
                time.sleep(_step_wait())
        except Exception as e:
            logger.error(
                f"[automation] {dl_name} — unexpected error on attempt {attempt}: {e}"
            )
            if attempt<retries:
                time.sleep(_step_wait())

    logger.error(
        f"[automation] {dl_name} — ALL {retries} ATTEMPTS FAILED. "
        f"Manual intervention required. Check STEP lines above for exact failure point."
    )
    logger.error(f"[automation] {'='*60}")
    return False

