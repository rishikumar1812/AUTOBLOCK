import re
import time
import logging
from pywinauto import Application,Desktop
from pywinauto.findwindows import(
    ElementNotFoundError,
    ElementAmbiguousError,  
)
from pywinauto.timings import  TimeoutError as PWTimeoutError

from config_loader import get_config
from ini_editor import uncheck_dl

# =========================================================
# Use the SAME logger name as main_pc_popup.py so every
# pywinauto step lands in the same main_pc_popup_YYYY-MM-DD.log
# file the engineer already checks — no separate log file
# to dig through when something fails.
# =========================================================
logger=logging.getLogger("main_pc_popup")

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

    for c in candidates:
        try:
            left = c.rectangle().left
            if abs(left - target_left) <= max(tol, 10):
                return c
        except Exception:
            continue

    raise RuntimeError(
        f"[automation] Could not find '{title}' label on {rack} side "
        f"(expected near left={target_left}, found {len(candidates)} "
        f"candidate(s) total)"
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


def run_stop_sequence(dl_name:str)->bool:
    """
    Full stop sequence for a DL:
    0. Wait for the target Building to show 'Wait' (no board present)
       — NEW. Prevents stranding a physically-present PCB by
       unchecking/stopping a building that's still occupied.
    1. Uncheck building in Data.ini
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
    retries=_retry_attempts()

    for attempt in range(1,retries+1):
        logger.info(f"[automation] {'='*60}")
        logger.info(
            f"[automation] {dl_name} — STARTING stop sequence "
            f"(attempt {attempt}/{retries})"
        )
        logger.info(f"[automation] {'='*60}")
        try:
            logger.info(f"[automation] STEP 0/9: Connect to InLine_Pro (needed early for building check)")
            app=_connect_to_app()
            window=_get_main_window(app)

            logger.info(f"[automation] STEP 0/9: Wait for Building to clear before any action")
            cleared = wait_for_building_clear(dl_name, app, window)
            if not cleared:
                logger.error(
                    f"[automation] {dl_name} — STEP 0/9: building never cleared. "
                    f"ABORTING — no Data.ini edit, no clicks. Manual intervention required."
                )
                return False

            logger.info(f"[automation] STEP 1/9: Edit Data.ini — uncheck {dl_name}")
            updated=uncheck_dl(dl_name)
            if updated:
                logger.info(
                    f"[automation] STEP 1/9: Edit Data.ini — OK, {dl_name} updated successfully"
                )
            else:
                logger.warning(
                    f"[automation] STEP 1/9: Edit Data.ini — SKIPPED "
                    f"({dl_name} already unchecked or error, see ini_editor log above)"
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
