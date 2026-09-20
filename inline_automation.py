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
from ini_editor import uncheck_dl, uncheck_ft, check_dl, check_ft

# =========================================================
# run_stop_sequence() result codes
#
# Four distinguishable outcomes, not just True/False — a site can
# succeed OR fail in meaningfully different ways depending on
# whether the Step 10/10 post-check needed to use its recovery
# restart at all, and if so, whether that restart worked:
#   RESULT_STOPPED           — 9 steps succeeded, post-check found
#                               the site already correctly 'Not
#                               Use', no recovery needed at all
#   RESULT_RESTARTED         — 9 steps succeeded, but a board rolled
#                               in mid-sequence; post-check's
#                               recovery re-check + restart BOTH
#                               succeeded — site is in a good state,
#                               but it's still flagged for review
#                               since a recovery action was taken
#   RESULT_POST_CHECK_FAILED — 9 steps succeeded, a board rolled in,
#                               but the recovery re-check or restart
#                               itself then failed — genuinely needs
#                               a human to check
#   RESULT_ERROR              — the 9-step sequence itself failed
# These need different operator-facing labels (see
# main_pc_popup.py's STATE_LABEL/STATE_COLOR).
# =========================================================
RESULT_STOPPED           = "stopped"
RESULT_RESTARTED         = "restarted"
RESULT_POST_CHECK_FAILED = "post_check_failed"
RESULT_ERROR             = "error"

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
# Post-sequence safety re-check — config access
#
# Guards against a PCB rolling into this exact site DURING the
# several seconds the stop sequence takes to run (STOP -> ini edit
# -> SETUP -> OK -> START -> Yes -> OK). If that happens, the site
# is left NOT_CHECK in Data.ini but physically occupied — InLine_Pro
# can then try to download to it anyway and time out, sometimes
# auto-stopping the whole line right after we just restarted it.
#
# Confirmed against a live screenshot of Auto-Status Windows
# (InLine_Pro_Ver 3.1.8.01): a site that's correctly unchecked shows
# "Not Use" (distinct from "Wait", which is Step 0's pre-check ready
# state — a checked, empty site also shows "Wait", not "Not Use").
#
# Recovery: if the site does NOT show "Not Use" after the wait, a
# board rolled in mid-sequence — it needs to be processed normally,
# not left disabled. So re-CHECK the site in Data.ini (undo the
# earlier uncheck) and restart the machine (SETUP -> OK -> START ->
# Yes -> OK) so InLine_Pro picks it up.
# =========================================================
_PC_DEFAULTS = {
    "enabled": True,
    "recheck_wait_sec": 10,        # wait after the sequence before re-reading the site
    "expected_text": "Not Use",    # what the site should show once correctly unchecked
}

def _pc_cfg() -> dict:
    cfg = get_config()
    if "post_check" not in cfg:
        logger.warning(
            "[automation] 'post_check' section missing from config.json "
            "— using hardcoded defaults. Add it to config.json to customise."
        )
        return dict(_PC_DEFAULTS)
    result = dict(_PC_DEFAULTS)
    result.update(cfg["post_check"])
    return result

def _pc_enabled() -> bool:
    return bool(_pc_cfg().get('enabled', True))


def verify_and_recover(dl_name: str, app, window, is_ft: bool) -> str:
    """
    Post-sequence safety net — called after all 9 click/edit steps
    complete successfully.

    Returns one of (see the RESULT_* constants above — reused here
    directly since these map 1:1 to run_stop_sequence()'s own
    result):
        RESULT_STOPPED           — site already correctly shows
                                    'Not Use', no recovery needed
        RESULT_RESTARTED         — a board rolled in mid-sequence;
                                    recovery re-check + restart BOTH
                                    succeeded
        RESULT_POST_CHECK_FAILED — a board rolled in, but the
                                    recovery re-check or restart
                                    itself then failed — genuinely
                                    needs a human to go look

    Previously this returned a plain bool that collapsed
    RESULT_STOPPED and RESULT_RESTARTED into the same True — so a
    site that needed its recovery restart, and successfully got it,
    was indistinguishable on the dashboard from one that never
    needed any recovery at all. This return value is what lets
    run_stop_sequence() (see its call site) and main_pc_popup.py's
    state tracking tell all three cases apart.

    1. Wait recheck_wait_sec, re-read the site's status.
    2. Shows 'Not Use' (expected_text) — correctly unchecked, log
       success, done.
    3. Shows anything else — a board rolled into this site during
       the sequence, so it needs to be processed normally rather
       than left disabled. Re-CHECK the site in Data.ini (undo the
       uncheck_dl/uncheck_ft from earlier in the sequence) and
       restart the machine (SETUP -> OK -> START -> Yes -> OK) so
       InLine_Pro picks the board up.
    """
    if not _pc_enabled():
        return RESULT_STOPPED

    cfg = _pc_cfg()
    wait1 = int(cfg["recheck_wait_sec"])
    expected = cfg["expected_text"].replace(" ", "").strip()

    logger.info(
        f"[automation] STEP 10/10: post-check — waiting {wait1}s then "
        f"re-reading {dl_name}'s status"
    )
    time.sleep(wait1)

    try:
        if is_ft:
            rack, fn_num = _parse_ft_task(dl_name)
            current_status = _read_function_status(window, rack, fn_num)
        else:
            rack, building_num = dl_to_rack_building(dl_name)
            current_status = _read_building_status(window, rack, building_num)
    except (RuntimeError, ValueError) as e:
        logger.warning(f"[automation] STEP 10/10: post-check read failed: {e}")
        current_status = None

    if current_status == expected:
        logger.info(
            f"[automation] STEP 10/10: {dl_name} shows '{expected}' as "
            f"expected — automation complete, all clear."
        )
        return RESULT_STOPPED

    logger.warning(
        f"[automation] STEP 10/10: {dl_name} shows '{current_status}' "
        f"(expected '{expected}') — a board entered during the "
        f"sequence. Re-checking site in Data.ini and restarting the "
        f"machine so it's processed normally."
    )

    # Re-CHECK the site — undo the uncheck_dl/uncheck_ft from earlier
    # in this same sequence, since the site is now genuinely occupied
    # and InLine_Pro needs to process the board rather than skip it.
    try:
        if is_ft:
            rechecked = check_ft(fn_num, rack)
            label = f"FUNCTION{fn_num} ({rack})"
        else:
            rechecked = check_dl(dl_name)
            label = dl_name
        if rechecked:
            logger.info(
                f"[automation] STEP 10/10: Data.ini — {label} re-checked "
                f"(CHECK)")
        else:
            logger.info(
                f"[automation] STEP 10/10: Data.ini — {label} already "
                f"CHECK, no change needed")
    except Exception as e:
        logger.error(
            f"[automation] STEP 10/10: Data.ini re-check failed: {e} — "
            f"MANUAL INTERVENTION NEEDED, site may be stuck NOT_CHECK "
            f"with a board physically present"
        )
        return RESULT_POST_CHECK_FAILED

    # Restart the machine so InLine_Pro reloads Data.ini (now with
    # this site re-checked) and resumes normal processing.
    #
    # IMPORTANT: by this point the main 9-step sequence has already
    # clicked STOP (step 1) *and* SETUP -> OK -> START -> Yes -> OK
    # (steps 5-9) — so the machine is already RUNNING again by the
    # time this post-check runs. SETUP is only clickable right after
    # STOP; clicking it while the machine is running is a no-op (the
    # button click fires but changes nothing), which is exactly why
    # the recovery restart appeared to silently do nothing. Click
    # STOP again here first so the machine is actually stopped before
    # driving through SETUP -> OK -> START -> Yes -> OK a second time.
    try:
        logger.info(f"[automation] STEP 10/10: recovery restart — Click STOP")
        _click_button(window, "STOP")

        logger.info(f"[automation] STEP 10/10: recovery restart — Click SETUP")
        _click_button(window, "SETUP")

        logger.info(f"[automation] STEP 10/10: recovery restart — Click OK (setup dialog)")
        _click_dialog_button(app, "OK")

        logger.info(f"[automation] STEP 10/10: recovery restart — Click START")
        _click_button(window, "START")

        logger.info(f"[automation] STEP 10/10: recovery restart — Click Yes (start confirmation)")
        _click_dialog_button(app, "Yes")

        logger.info(f"[automation] STEP 10/10: recovery restart — Click OK (final dialog)")
        _click_dialog_button(app, "OK")

        logger.info(
            f"[automation] STEP 10/10: {dl_name} — recovery restart "
            f"complete, machine resumed"
        )
        return RESULT_RESTARTED
    except RuntimeError as e:
        logger.error(
            f"[automation] STEP 10/10: recovery restart failed: {e} — "
            f"MANUAL INTERVENTION NEEDED, line may still be stopped"
        )
        return RESULT_POST_CHECK_FAILED


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

    # DL01-DL10 -> front rack, DL11-DL20 -> rear rack — must match
    # ini_editor.dl_to_ini_key()'s RACK1/RACK2 split exactly (<=10,
    # not <=11). The old "<=11" here mapped DL11 to
    # (front, building_num=11), which then always failed the
    # "screen only has Buildings 1-10" check below — DL11 could
    # never be stopped. DL13 etc. happened to still come out right
    # because both boundaries only disagree on DL11 itself.
    if dl_num <= 10:
        rack = "front"
        building_num = dl_num
    else:
        rack = "rear"
        building_num = dl_num - 10

    # Screen only has Building 1-10. DL10->front-10 and DL20->rear-10
    if building_num > 10:
        raise ValueError(
            f"{dl_name!r} maps to Building {building_num} on {rack} rack "
            f"but screen only has Buildings 1-10 "
            f"(DL11/DL21 are not valid building positions)."
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

    Building 1-9: plain exact match, confirmed working live —
    UNTOUCHED, do not modify this part.
    """
    cfg = _bc_cfg()
    target_left = cfg['front_label_left'] if rack == "front" else cfg['rear_label_left']
    tol = cfg['row_top_tolerance_px']
    title = f"Building {building_num}"

    # --- Building 10 / Building 20 special case (hardcoded) --------
    # Two conflicting reports on the real text for this one label:
    # test_read_building_status.py's raw captured output showed
    # "Building 1 0" (space inside the number), but a direct look at
    # the actual screen shows "Building10" (no space at all) — unlike
    # Building 1-9, which render plainly as "Building N" with a
    # normal single space. Accepting all three variants costs nothing
    # for Building 1-9 (untouched, still strict exact match) and
    # maximizes the chance of matching Building 10/20 correctly
    # without needing to resolve which report was right. Comment out
    # / remove this block if a future InLine_Pro build renders
    # Building 10 differently.
    acceptable_titles = {title}
    if building_num == 10:
        acceptable_titles.add("Building 1 0")
        acceptable_titles.add("Building10")
    # -----------------------------------------------------------------

    candidates = []
    for c in window.descendants():
        try:
            if _safe_text(c) in acceptable_titles:
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


def run_stop_sequence(dl_name:str)->str:
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

    Returns one of (see the RESULT_* constants above):
        RESULT_STOPPED           — sequence AND post-check both
                                    completed successfully
        RESULT_POST_CHECK_FAILED — the 9-step sequence completed
                                    successfully, but Step 10/10's
                                    post-check recovery then failed
                                    (a board rolled in mid-sequence
                                    and the automatic re-check +
                                    restart didn't work) — the site
                                    WAS correctly stopped, but needs
                                    a human to check the recovery
        RESULT_ERROR              — the 9-step sequence itself
                                    failed after all retries, OR
                                    Step 0 timed out waiting for the
                                    building to clear (no retries are
                                    spent on a Step-0 timeout — that's
                                    not a transient error, retrying
                                    immediately won't change a board
                                    still being physically present)
    """
    if not _PYWINAUTO_AVAILABLE:
        logger.error(
            f"[automation] {dl_name} — pywinauto not available on this "
            f"platform. Run on Windows Main PC."
        )
        return RESULT_ERROR

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
                return RESULT_ERROR

            # Step 1 — Click STOP FIRST, before touching Data.ini.
            # (Reordered from the old STOP-comes-after-ini sequence.)
            # Rationale: wait_for_building_clear() above only confirms
            # the site was empty at the moment we last polled — a new
            # PCB can still roll in during the gap between that read
            # and the machine actually being stopped. Stopping the
            # line FIRST, immediately after confirming clear, closes
            # that window as tightly as possible. Editing Data.ini
            # (step 2) then happens with the machine already halted,
            # so no board is moving while NOT_CHECK is being written.
            logger.info(f"[automation] STEP 1/9: Click STOP")
            _click_button(window, "STOP")

            # Step 2 — ini edit (DL only), now that the line is stopped.
            #
            # uncheck_dl()/uncheck_ft() RAISE IniEditError for any
            # GENUINE failure (Data.ini not found, section/key
            # missing, bad DL/FT identity) — they only return False
            # for the harmless "already NOT_CHECK, nothing to change"
            # case. Deliberately NOT catching that exception here:
            # letting it propagate up to this function's own
            # `except Exception` below means a genuine Data.ini
            # failure now correctly ABORTS this attempt (and retries/
            # fails per the normal retry logic) with the line left
            # STOPPED — instead of logging a vague "SKIPPED (already
            # unchecked or error)" warning and proceeding straight to
            # SETUP/START anyway, restarting the line with the
            # blocked site never actually disabled.
            if not is_ft:
                logger.info(f"[automation] STEP 2/9: Edit Data.ini — uncheck {dl_name}")
                updated = uncheck_dl(dl_name)
                if updated:
                    logger.info(
                        f"[automation] STEP 2/9: Edit Data.ini — OK")
                else:
                    logger.info(
                        f"[automation] STEP 2/9: Edit Data.ini — "
                        f"{dl_name} already NOT_CHECK, no change needed")
            else:
                # FT task — uncheck FUNCTION row in Data.ini. (No
                # local try/except here — see comment above: a
                # genuine failure must propagate to this function's
                # own except block, not be swallowed.)
                rack, fn_num = _parse_ft_task(dl_name)
                updated = uncheck_ft(fn_num, rack)
                if updated:
                    logger.info(
                        f"[automation] STEP 2/9: Data.ini — OK, "
                        f"FUNCTION{fn_num} ({rack}) set to NOT_CHECK"
                    )
                else:
                    logger.info(
                        f"[automation] STEP 2/9: Data.ini — FUNCTION{fn_num} "
                        f"already NOT_CHECK, no change needed"
                    )

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

            # Post-check: did a board roll into this site DURING the
            # sequence above? Recovers by re-starting the machine if
            # so. verify_and_recover() now returns one of the three
            # RESULT_* codes directly — pass it straight through,
            # since it already distinguishes "no recovery needed"
            # from "recovery needed and succeeded" from "recovery
            # needed and failed". This is what makes the toast, tray
            # alert, and dashboard state label all correctly tell
            # these three outcomes apart instead of collapsing the
            # first two into a single generic "success".
            post_check_result = verify_and_recover(dl_name, app, window, is_ft)
            if post_check_result == RESULT_POST_CHECK_FAILED:
                logger.error(
                    f"[automation] {dl_name} — post-check recovery FAILED, "
                    f"reporting this attempt as failed despite the main "
                    f"sequence completing (see STEP 10/10 lines above)."
                )
            return post_check_result

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
    return RESULT_ERROR
