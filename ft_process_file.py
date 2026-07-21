"""
ft_process_file.py  —  FT PC
Monitors FT machine CSV log files, computes statistics, and sends
a STOP signal to Main PC when failures exceed the configured threshold.

Architecture:
  read_csv_files()           — read all station CSVs, return raw frames
      ↓
  merge_records()            — deduplicate, sort by timestamp
      ↓
  calculate_last60()         — INDEPENDENT of STOP state, always latest 60
      ↓
  calculate_active_window()  — records strictly after last_stop only
      ↓
  calculate_status()         — derive RUNNING/WARNING/BLOCKED
      ↓
  send_stop_if_required()    — exactly once per blocking event
      ↓
  save_stats()               — atomic JSON write for dashboard
      ↓
  Dashboard reads JSON only — never touches CSV files or calculates anything

Key invariant:
  fails_60 / rate_60 are ALWAYS computed from the latest 60 records
  in the full combined dataset. They are NEVER affected by:
    - last_stop timestamp
    - active window boundaries
    - STOP signal being sent
    - Status being BLOCKED
    - Machine being idle (no_data timeout)
    - Missing files

  The only time fails_60 changes is when new production records arrive.

Run continuously (one per FT PC):
    python ft_process_file.py
"""

import os
import re
import json
import time
import logging
import pandas as pd
from datetime import datetime, date
from typing import Optional

from ft_config_loader import (
    log_dir, log_reg_dir, poll_interval, record_window,
    warn_at_fail, block_at_fail, ft_display_label, ft_id,
    no_data_minutes,
)
from ft_network_sender import send_stop_signal
from log_cleanup import cleanup_old_logs, DailyFileHandler


# =========================================================
# Logger
# =========================================================
def _setup_logger() -> logging.Logger:
    ldir = log_reg_dir()
    os.makedirs(ldir, exist_ok=True)
    cleanup_old_logs(ldir, retention_days=7)

    logger = logging.getLogger("ft_process")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        safe_id = ft_id().replace("/", "_")
        fh = DailyFileHandler(
            f"ft_process_{safe_id}", ldir, retention_days=7)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    return logger


logger = _setup_logger()

# ── Pre-compiled field patterns ─────────────────────────
# Tolerates any whitespace around ':' and is case-insensitive
_RE_RESULT = re.compile(r"^RESULT\s*:\s*(.+)$",  re.IGNORECASE)
_RE_DATE   = re.compile(r"^DATE\s*:\s*(.+)$",    re.IGNORECASE)
_RE_TIME   = re.compile(r"^TIME\s*:\s*(.+)$",    re.IGNORECASE)
_RE_JIG    = re.compile(r"^JIG\s*:\s*(.+)$",     re.IGNORECASE)


# =========================================================
# Path helpers
# =========================================================
def _safe_id() -> str:
    return ft_id().replace("/", "_").replace("\\", "_")


def _last_stop_path() -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id()}_last_stop.json")


def _stats_path() -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id()}_stats.json")


def _pending_stop_path() -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id()}_pending_stop.json")


# =========================================================
# Pending stop — survives failed network sends
# =========================================================
def _save_pending_stop(fails: int, rate: float,
                       fails_60: int, rate_60: float) -> None:
    path = _pending_stop_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "fails":    fails,
                "rate":     rate,
                "fails_60": fails_60,
                "rate_60":  rate_60,
                "ts":       datetime.now().isoformat(),
            }, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save pending_stop: {e}")


def _load_pending_stop() -> Optional[dict]:
    path = _pending_stop_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _clear_pending_stop() -> None:
    path = _pending_stop_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# =========================================================
# Stats persistence
# Dashboard reads this JSON — never touches CSV files
# =========================================================
def save_stats(stats: dict) -> None:
    """
    Atomic write: write to .tmp then os.replace() so the dashboard
    never reads a half-written file even if we crash mid-write.
    """
    path = _stats_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save stats: {e}")


def load_stats() -> dict:
    """Called by ft_dashboard.py — reads JSON only, no CSV processing."""
    path = _stats_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return _empty_stats("STOPPED")


def _load_last_saved_last60() -> tuple:
    """
    Load fails_60 and rate_60 from the previously saved stats JSON.

    Purpose: preserve last known Last-60 values in all early-return
    paths (no files today, all files unreadable, no-data timeout).
    This ensures fails_60 / rate_60 only change when new production
    records arrive — they never reset to 0 due to machine idle time
    or file rotation at midnight.

    Returns (fails_60, rate_60) — defaults (0, 0.0) if no saved stats.
    """
    try:
        stats = load_stats()
        return int(stats.get("fails_60", 0)), float(stats.get("rate_60", 0.0))
    except Exception:
        return 0, 0.0


# =========================================================
# Last stop tracker
# =========================================================
def get_last_stop() -> Optional[datetime]:
    path = _last_stop_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("last_stop")
            if ts:
                return datetime.fromisoformat(ts)
    except Exception as e:
        logger.warning(f"[ft_process] Could not read last_stop: {e}")
    return None


def save_last_stop(ts: datetime) -> None:
    path = _last_stop_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_stop": ts.isoformat(), "ft_id": ft_id()}, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save last_stop: {e}")


def _format_last_stop(last_stop_ts: Optional[datetime]) -> str:
    if last_stop_ts is None:
        return "—"
    try:
        if last_stop_ts.date() == date.today():
            return last_stop_ts.strftime("%H:%M:%S")
        return last_stop_ts.strftime("%b %d  %H:%M:%S")
    except Exception:
        return str(last_stop_ts)


# =========================================================
# Step 1: read_csv_files()
# Read all today's FT CSV files, return list of DataFrames
# =========================================================
def read_csv_file(filepath: str) -> Optional[pd.DataFrame]:
    """
    Parse one FT CSV file into a DataFrame.
    Field order within each #INIT section is irrelevant.
    Whitespace variations around ':' are tolerated.
    NaT timestamps and partial sections are dropped safely.
    File-lock errors (Windows concurrent write) return None.
    """
    sections        = []
    current_section = []

    try:
        with open(filepath, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#INIT"):
                    if current_section:
                        sections.append(current_section)
                    current_section = [line]
                else:
                    current_section.append(line)
        if current_section:
            sections.append(current_section)
    except OSError as e:
        logger.warning(
            f"[ft_process] Could not open "
            f"{os.path.basename(filepath)}: {e}")
        return None
    except Exception as e:
        logger.error(
            f"[ft_process] Unexpected read error "
            f"{os.path.basename(filepath)}: {e}")
        return None

    rows = []
    for section in sections:
        record = {}
        for line in section:
            m = _RE_RESULT.match(line)
            if m:
                record["RESULT"] = m.group(1).strip()
                continue
            m = _RE_DATE.match(line)
            if m:
                record["DATE"] = m.group(1).strip()
                continue
            m = _RE_TIME.match(line)
            if m:
                record["Update_Time"] = m.group(1).strip()
                continue
            m = _RE_JIG.match(line)
            if m:
                record["JIG"] = m.group(1).strip()

        required = ("DATE", "Update_Time", "JIG", "RESULT")
        if all(k in record for k in required):
            if record["RESULT"].upper() not in ("PASS", "FAIL"):
                logger.debug(
                    f"[ft_process] Skipping unknown RESULT "
                    f"'{record['RESULT']}'")
                continue
            rows.append(record)
        elif any(k in record for k in required):
            logger.debug(
                f"[ft_process] Partial section skipped "
                f"(missing: {[k for k in required if k not in record]})")

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["DATE", "Update_Time", "JIG", "RESULT"])
    df["Time_stamp"] = pd.to_datetime(
        df["DATE"] + " " + df["Update_Time"],
        format="%Y/%m/%d %H:%M:%S",
        errors="coerce")

    nat_count = df["Time_stamp"].isna().sum()
    if nat_count:
        logger.warning(
            f"[ft_process] {os.path.basename(filepath)}: "
            f"dropped {nat_count} record(s) with unparseable timestamps")
    df = df.dropna(subset=["Time_stamp"])

    return df.sort_values("Time_stamp").reset_index(drop=True) if not df.empty else None


def is_today(filepath: str) -> bool:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        return mtime.date() == date.today()
    except Exception:
        return False


def read_csv_files(directory: str) -> tuple:
    """
    Step 1: Collect and read all today's CSV files.
    Returns (all_files: list[str], frames: list[DataFrame])
    """
    try:
        all_files = sorted([
            os.path.join(directory, fn)
            for fn in os.listdir(directory)
            if fn.endswith(".csv") and
            is_today(os.path.join(directory, fn))
        ])
    except OSError as e:
        logger.error(f"[ft_process] Cannot list log dir: {e}")
        return [], []

    frames = []
    for fpath in all_files:
        df = read_csv_file(fpath)
        if df is not None and not df.empty:
            frames.append(df)

    return all_files, frames


# =========================================================
# Step 2: merge_records()
# Combine frames, deduplicate, sort by timestamp
# =========================================================
def merge_records(frames: list) -> Optional[pd.DataFrame]:
    """
    Step 2: Merge all station DataFrames into one clean dataset.
    Deduplicates on (DATE, Update_Time, JIG, RESULT) to prevent
    double-counting when the same record appears in multiple files.
    Returns None if result is empty or all timestamps are invalid.
    """
    combined = pd.concat(frames, ignore_index=True)

    before = len(combined)
    combined = combined.drop_duplicates(
        subset=["DATE", "Update_Time", "JIG", "RESULT"]
    ).reset_index(drop=True)
    dupes = before - len(combined)
    if dupes:
        logger.debug(f"[ft_process] Dropped {dupes} duplicate record(s)")

    combined = combined.sort_values("Time_stamp").reset_index(drop=True)

    valid_ts = combined["Time_stamp"].dropna()
    if valid_ts.empty:
        logger.warning("[ft_process] All timestamps invalid after merge")
        return None

    return combined


# =========================================================
# Step 3: calculate_last60()
# COMPLETELY INDEPENDENT of last_stop or active window
# =========================================================
def calculate_last60(combined: pd.DataFrame) -> tuple:
    """
    Step 3: Compute Last-60 statistics from the FULL combined dataset.

    INVARIANT: This function NEVER receives last_stop_ts as a parameter
    and NEVER filters by any stop-related criteria. It always operates
    on the complete production history, taking the most recent 60 records.

    This guarantees that fails_60 / rate_60 can ONLY change when new
    production records arrive in the CSV files. They are immune to:
      - STOP signals being sent
      - Active window resets
      - Machine being idle
      - Status being BLOCKED
      - last_stop_ts value

    Returns (fails_60: int, rate_60: float)
    """
    last60   = combined.tail(60)
    n        = len(last60)
    fails_60 = int(last60[last60["RESULT"].str.upper() == "FAIL"].shape[0])
    rate_60  = (fails_60 / n * 100) if n > 0 else 0.0
    return fails_60, rate_60


# =========================================================
# Step 4: calculate_active_window()
# Records strictly after last_stop — used ONLY for STOP logic
# =========================================================
def calculate_active_window(combined: pd.DataFrame,
                            last_stop_ts: Optional[datetime]) -> pd.DataFrame:
    """
    Step 4: Extract the active window — records AFTER last_stop.

    This is the ONLY place last_stop_ts affects statistics. The result
    is used exclusively for:
      - fails (current cycle fail count)
      - rate  (current cycle fail rate)
      - status determination
      - STOP signal decision

    It has NO effect on fails_60 / rate_60 which are calculated in
    Step 3 from the full dataset before this function is called.
    """
    if last_stop_ts is not None:
        active = combined[
            combined["Time_stamp"] > pd.Timestamp(last_stop_ts)
        ].copy()
    else:
        active = combined.copy()

    window = record_window()
    if len(active) > window:
        active = active.tail(window).reset_index(drop=True)

    return active


# =========================================================
# Step 5: calculate_status()
# Derive machine status from active window stats
# =========================================================
def calculate_status(fails: int, pass_count: int,
                     last_stop_ts: Optional[datetime]) -> str:
    """
    Step 5: Determine machine status from active window statistics.

    Rules:
      BLOCKED  — fails >= block threshold, OR
                 last_stop exists but no new PASS (still blocked)
      WARNING  — fails >= warn threshold
      RUNNING  — all else
    """
    if fails >= block_at_fail():
        return "BLOCKED"
    if last_stop_ts is not None and pass_count == 0 and fails > 0:
        return "BLOCKED"
    if fails >= warn_at_fail():
        return "WARNING"
    return "RUNNING"


# =========================================================
# Step 6: send_stop_if_required()
# Send exactly once per blocking event
# =========================================================
def send_stop_if_required(status: str, fails: int, rate: float,
                          fails_60: int, rate_60: float,
                          last_stop_ts: Optional[datetime],
                          label: str) -> Optional[datetime]:
    """
    Step 6: Send STOP signal to Main PC if conditions are met.

    Sends STOP exactly once per blocking event:
      - First block: last_stop_ts is None and status is BLOCKED
      - Re-block:    last_stop_ts exists but new fails appeared after it
                     (machine was unblocked, ran, and failed again)

    Does NOT send if:
      - status is not BLOCKED
      - last_stop_ts exists and no new fails in active window
        (machine is still blocked from previous STOP — don't spam)

    Returns new last_stop datetime if STOP was sent, else None.
    """
    first_block       = (last_stop_ts is None and status == "BLOCKED")
    new_fails_after   = (last_stop_ts is not None and fails > 0 and
                         status == "BLOCKED")
    should_send       = first_block or new_fails_after

    if not should_send:
        return None

    pending = _load_pending_stop()
    if pending:
        logger.info(f"[ft_process] {label} — retrying pending STOP signal")

    logger.warning(
        f"[ft_process] {label} — BLOCKED: {fails} fails "
        f"({rate:.1f}%). Sending STOP to Main PC.")

    sent = send_stop_signal()
    if sent:
        new_ts = datetime.now()
        save_last_stop(new_ts)
        _clear_pending_stop()
        logger.info(
            f"[ft_process] {label} — STOP sent OK "
            f"at {new_ts.strftime('%H:%M:%S')}")
        return new_ts
    else:
        _save_pending_stop(fails, rate, fails_60, rate_60)
        logger.error(
            f"[ft_process] {label} — STOP FAILED — saved pending, "
            f"will retry next poll")
        return None


# =========================================================
# scan_and_check() — orchestrates all steps
# =========================================================
def scan_and_check() -> dict:
    """
    Main entry point — orchestrates all processing steps.

    Step 1: read_csv_files()
    Step 2: merge_records()
    Step 3: calculate_last60()         ← independent, always from full data
    Step 4: calculate_active_window()  ← only for STOP logic
    Step 5: calculate_status()
    Step 6: send_stop_if_required()
    Step 7: save_stats()               ← dashboard reads this JSON only
    """
    directory = log_dir()
    label     = ft_display_label()

    # ── Load previously saved Last-60 values ───────────────
    # These are returned in all early-return paths so that fails_60
    # and rate_60 are NEVER reset to 0 due to idle time or missing files.
    # They only change when new production records arrive in Step 3.
    saved_fails_60, saved_rate_60 = _load_last_saved_last60()

    if not os.path.isdir(directory):
        logger.error(f"[ft_process] Log dir not found: {directory}")
        s = _empty_stats("NO DIR",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(s)
        return s

    # ── Step 1: Read CSV files ──────────────────────────────
    all_files, frames = read_csv_files(directory)

    if not all_files:
        logger.debug(f"[ft_process] {label} — no CSV files today")
        s = _empty_stats("STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(s)
        return s

    if not frames:
        logger.warning(f"[ft_process] {label} — all CSV files unreadable")
        s = _empty_stats("STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(s)
        return s

    # ── Step 2: Merge and deduplicate ──────────────────────
    combined = merge_records(frames)
    if combined is None:
        s = _empty_stats("STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(s)
        return s

    # ── No-data timeout check ──────────────────────────────
    latest_ts = combined["Time_stamp"].max()
    try:
        minutes_since = (datetime.now() - latest_ts).total_seconds() / 60
    except Exception:
        minutes_since = 0

    no_data_limit = no_data_minutes()
    if minutes_since >= no_data_limit:
        logger.info(
            f"[ft_process] {label} — no data for "
            f"{int(minutes_since)}min → STOPPED")
        # Preserve last-known fails_60/rate_60 — machine idle, not reset
        s = _empty_stats("STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(s)
        return s

    last_stop_ts  = get_last_stop()
    last_stop_str = _format_last_stop(last_stop_ts)

    # ── Step 3: Last-60 (INDEPENDENT — computed from full data) ──
    # This is computed BEFORE the active window filter.
    # It has NO dependency on last_stop_ts, status, or anything else.
    # Result is used directly in stats dict — never recalculated elsewhere.
    fails_60, rate_60 = calculate_last60(combined)

    # ── Step 4: Active window (for STOP logic only) ────────
    active = calculate_active_window(combined, last_stop_ts)
    total  = len(active)

    # ── No new records after last stop ─────────────────────
    if total == 0:
        blocked_min = 0
        if last_stop_ts:
            blocked_min = int(
                (datetime.now() - last_stop_ts).total_seconds() / 60)
        status = "BLOCKED" if last_stop_ts else "RUNNING"
        stats  = _build_stats(
            label=label, status=status,
            total=0, fails=0, rate=0.0,
            fails_60=fails_60, rate_60=rate_60,
            last_stop_str=last_stop_str,
            latest_ts=latest_ts,
            blocked_min=blocked_min,
            files_today=len(all_files),
            minutes_since=int(minutes_since),
        )
        save_stats(stats)
        return stats

    # ── Step 5: Status ─────────────────────────────────────
    fails      = int(active[active["RESULT"].str.upper() == "FAIL"].shape[0])
    pass_count = int(active[active["RESULT"].str.upper() == "PASS"].shape[0])
    rate       = (fails / total * 100) if total > 0 else 0.0
    status     = calculate_status(fails, pass_count, last_stop_ts)

    blocked_min = 0
    if status == "BLOCKED" and last_stop_ts is not None:
        blocked_min = int(
            (datetime.now() - last_stop_ts).total_seconds() / 60)

    # ── Step 6: Send STOP if required ──────────────────────
    new_stop_ts = send_stop_if_required(
        status, fails, rate, fails_60, rate_60, last_stop_ts, label)
    if new_stop_ts:
        # Recalculate last_stop_str and blocked_min after successful send
        last_stop_str = _format_last_stop(new_stop_ts)
        blocked_min   = 0  # just sent — 0 minutes blocked so far

    # ── Step 7: Save stats ─────────────────────────────────
    logger.info(
        f"[ft_process] {label} — {status} | "
        f"fails={fails}/{total} ({rate:.1f}%) | "
        f"f60={fails_60} r60={rate_60:.1f}% | "
        f"files={len(all_files)}")

    stats = _build_stats(
        label=label, status=status,
        total=total, fails=fails, rate=rate,
        fails_60=fails_60, rate_60=rate_60,
        last_stop_str=last_stop_str,
        latest_ts=latest_ts,
        blocked_min=blocked_min,
        files_today=len(all_files),
        minutes_since=int(minutes_since),
    )
    save_stats(stats)
    return stats


def _build_stats(*, label, status, total, fails, rate,
                 fails_60, rate_60, last_stop_str, latest_ts,
                 blocked_min, files_today, minutes_since) -> dict:
    """Build the stats dict that is written to JSON and read by dashboard."""
    try:
        last_data = latest_ts.strftime("%H:%M:%S")
    except Exception:
        last_data = "—"
    return {
        "label":         label,
        "ft_id":         ft_id(),
        "status":        status,
        "total":         total,
        "fails":         fails,
        "rate":          round(rate, 2),
        "fails_60":      fails_60,
        "rate_60":       round(rate_60, 2),
        "last_stop":     last_stop_str,
        "last_data":     last_data,
        "blocked_min":   blocked_min,
        "files_today":   files_today,
        "minutes_since": minutes_since,
    }


def _empty_stats(status: str = "STOPPED",
                 last_stop: Optional[datetime] = None,
                 fails_60: int = 0,
                 rate_60: float = 0.0) -> dict:
    """
    Returns a minimal stats dict for error/idle states.
    fails_60 and rate_60 are passed in from the last saved values
    so they are never reset to 0 in early-return paths.
    """
    return {
        "label":         ft_display_label(),
        "ft_id":         ft_id(),
        "status":        status,
        "total":         0,
        "fails":         0,
        "rate":          0.0,
        "fails_60":      fails_60,
        "rate_60":       round(rate_60, 2),
        "last_stop":     _format_last_stop(last_stop),
        "last_data":     "—",
        "blocked_min":   0,
        "files_today":   0,
        "minutes_since": 0,
    }


# =========================================================
# Hide console window when running as PyInstaller EXE
# =========================================================
def _hide_console() -> None:
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def _setup_signal_handlers() -> None:
    import signal

    def _shutdown(signum, frame):
        logger.info(
            f"[ft_process] {ft_display_label()} — "
            f"shutdown signal {signum} received. Stopping.")
        import sys
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass


# =========================================================
# Entry point — runs 24/7
# =========================================================
if __name__ == "__main__":
    _hide_console()
    _setup_signal_handlers()

    interval = poll_interval()
    label    = ft_display_label()
    logger.info(
        f"[ft_process] {label} monitor started. "
        f"Scanning every {interval}s.")
    while True:
        try:
            scan_and_check()
        except Exception as e:
            logger.error(
                f"[ft_process] Unhandled error: {e}", exc_info=True)
        time.sleep(interval)
