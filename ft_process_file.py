"""
ft_process_file.py  —  FT PC
Monitors CSV log files in log_dir, combines all stations (_01 to _06),
counts fails in the active window, and sends STOP to Main PC when
combined fails hit block_at_fails threshold.

CSV filename pattern: <any_prefix>_01.csv ... <any_prefix>_06.csv
CSV format:
  - #INIT      marks start of each record section
  - DATE :     2026/07/08  (slashes)
  - TIME :     14:32:07
  - JIG :      1
  - RESULT :   PASS or FAIL  (space before colon)
  - Field order may vary within a section
  - No ARRAY field (unlike DL PC)

Run continuously:
    python ft_process_file.py
"""

import os
import re
import json
import time
import logging
import pandas as pd
from datetime import datetime, date

from ft_config_loader import (
    log_dir, log_reg_dir, poll_interval, record_window,
    warn_at_fail, block_at_fail, ft_display_label, ft_id
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

# ── Pre-compiled field patterns ────────────────────────────
# Fix Bug 6: match field names regardless of internal whitespace
# e.g. "RESULT :", "RESULT:", "RESULT  :" all match _RE_RESULT
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
def _save_pending_stop(fails, rate, fails_60, rate_60) -> None:
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


def _load_pending_stop():
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
# Stats persistence — dashboard reads this instead of re-scanning
# =========================================================
def save_stats(stats: dict) -> None:
    path = _stats_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic write: write to temp then rename so dashboard
        # never reads a half-written file (Bug 3 partial write defence)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save stats: {e}")


def load_stats() -> dict:
    path = _stats_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return _empty_stats("STOPPED")


# =========================================================
# Last stop tracker
# =========================================================
def get_last_stop():
    # Fix Bug 5: json already imported at top — removed duplicate import
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
    # Fix Bug 5: json already imported at top — removed duplicate import
    path = _last_stop_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "last_stop": ts.isoformat(),
                "ft_id":     ft_id(),
            }, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save last_stop: {e}")


# =========================================================
# CSV reader
# Fix Bug 1: field order independence — collect all fields per
#   section first, then emit record once. Handles any order of
#   DATE/TIME/JIG/RESULT within a #INIT section.
# Fix Bug 6: regex patterns tolerate whitespace variations.
# Fix Bug 7: NaT rows dropped after parsing.
# =========================================================
def read_csv_file(filepath: str):
    """
    Parse an FT CSV file into a DataFrame.
    Returns DataFrame with columns [DATE, Update_Time, JIG, RESULT,
    Time_stamp] or None if file is empty / unreadable.

    Robustness:
    - Field order within a #INIT section does not matter.
    - Whitespace variations around ':' are tolerated.
    - Partial sections (missing any required field) are skipped.
    - NaT timestamps (corrupt date/time values) are dropped.
    - File read errors return None rather than raising.
    - Windows file-lock errors: opened with errors="replace" so
      a concurrent write producing partial UTF-8 doesn't crash.
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
        # File locked by FT machine during write — skip this poll
        logger.warning(
            f"[ft_process] Could not open {os.path.basename(filepath)}: {e}")
        return None
    except Exception as e:
        logger.error(
            f"[ft_process] Unexpected error reading "
            f"{os.path.basename(filepath)}: {e}")
        return None

    rows = []
    for section in sections:
        # Fix Bug 1: collect ALL fields in section before emitting record
        # so field order within a section doesn't matter
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

        # Emit record only if all required fields found
        required = ("DATE", "Update_Time", "JIG", "RESULT")
        if all(k in record for k in required):
            # Validate RESULT value — skip garbage entries
            if record["RESULT"].upper() not in ("PASS", "FAIL"):
                logger.debug(
                    f"[ft_process] Skipping record with "
                    f"unknown RESULT '{record['RESULT']}'")
                continue
            rows.append(record)
        elif any(k in record for k in required):
            # Partial section — likely a file mid-write, skip silently
            logger.debug(
                f"[ft_process] Partial section skipped "
                f"(missing: "
                f"{[k for k in required if k not in record]})")

    if not rows:
        return None

    # Fix Bug 8: collect list then concat once instead of repeated concat
    df = pd.DataFrame(rows, columns=["DATE", "Update_Time", "JIG", "RESULT"])

    # DATE format: 2026/07/08 — pd.to_datetime handles slashes
    # format="%Y/%m/%d %H:%M:%S" matches real FT CSV date format
    df["Time_stamp"] = pd.to_datetime(
        df["DATE"] + " " + df["Update_Time"],
        format="%Y/%m/%d %H:%M:%S",
        errors="coerce")

    # Fix Bug 7: drop NaT timestamps (corrupt/partial date strings)
    nat_count = df["Time_stamp"].isna().sum()
    if nat_count:
        logger.warning(
            f"[ft_process] {os.path.basename(filepath)}: "
            f"dropped {nat_count} record(s) with unparseable timestamps")
    df = df.dropna(subset=["Time_stamp"])

    if df.empty:
        return None

    return df.sort_values("Time_stamp").reset_index(drop=True)


def is_today(filepath: str) -> bool:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        return mtime.date() == date.today()
    except Exception:
        return False


def _format_last_stop(last_stop_ts) -> str:
    """Format last_stop timestamp for display — handles None."""
    if last_stop_ts is None:
        return "—"
    try:
        if last_stop_ts.date() == date.today():
            return last_stop_ts.strftime("%H:%M:%S")
        return last_stop_ts.strftime("%b %d  %H:%M:%S")
    except Exception:
        return str(last_stop_ts)


# =========================================================
# Main scan
# =========================================================
def scan_and_check() -> dict:
    """
    Scans all CSV files in log_dir, combines today's data
    across all stations, computes fail stats, and sends ONE
    STOP to Main PC if combined fails hit threshold.

    Returns stats dict for ft_dashboard.py to display.
    """
    directory = log_dir()
    label     = ft_display_label()

    if not os.path.isdir(directory):
        logger.error(f"[ft_process] Log dir not found: {directory}")
        return _empty_stats("NO DIR")

    # Collect all today's CSV files
    try:
        all_files = sorted([
            os.path.join(directory, fn)
            for fn in os.listdir(directory)
            if fn.endswith(".csv") and
            is_today(os.path.join(directory, fn))
        ])
    except OSError as e:
        logger.error(f"[ft_process] Cannot list log dir: {e}")
        return _empty_stats("STOPPED")

    if not all_files:
        logger.debug(f"[ft_process] {label} — no CSV files today")
        s = _empty_stats("STOPPED")
        save_stats(s)
        return s

    # Fix Bug 8: collect DataFrames in list, concat once
    frames = []
    for fpath in all_files:
        df = read_csv_file(fpath)
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        s = _empty_stats("STOPPED")
        save_stats(s)
        return s

    combined = pd.concat(frames, ignore_index=True)

    # Fix Bug 2: deduplicate on (DATE, Update_Time, JIG, RESULT)
    # prevents double-counting if same record appears in multiple files
    before = len(combined)
    combined = combined.drop_duplicates(
        subset=["DATE", "Update_Time", "JIG", "RESULT"]
    ).reset_index(drop=True)
    dupes = before - len(combined)
    if dupes:
        logger.debug(
            f"[ft_process] {label} — dropped {dupes} duplicate record(s)")

    combined = combined.sort_values("Time_stamp").reset_index(drop=True)

    # Fix Bug 7: latest_ts must not be NaT
    valid_ts = combined["Time_stamp"].dropna()
    if valid_ts.empty:
        logger.warning(
            f"[ft_process] {label} — all timestamps invalid, skipping")
        return _empty_stats("STOPPED")

    latest_ts = valid_ts.max()

    # No-data timeout check
    try:
        minutes_since = (datetime.now() - latest_ts).total_seconds() / 60
    except Exception:
        minutes_since = 0

    from ft_config_loader import no_data_minutes
    no_data_limit = no_data_minutes()
    if minutes_since >= no_data_limit:
        logger.info(
            f"[ft_process] {label} — no data for "
            f"{int(minutes_since)}min (limit={no_data_limit}min) → STOPPED")
        s = _empty_stats("STOPPED")
        save_stats(s)
        return s

    last_stop_ts = get_last_stop()

    # ── Last-60 from FULL combined — never resets on STOP ──────
    # Always computed before active-window filter so fails_60/rate_60
    # are preserved after a STOP is sent and active becomes empty.
    last60   = combined.tail(60)
    fails_60 = int(last60[last60["RESULT"].str.upper() == "FAIL"].shape[0])
    rate_60  = (fails_60 / len(last60) * 100) if len(last60) > 0 else 0.0

    last_stop_str = _format_last_stop(last_stop_ts)

    # ── Active window — records strictly AFTER last stop ────────
    if last_stop_ts is not None:
        active = combined[
            combined["Time_stamp"] > pd.Timestamp(last_stop_ts)
        ].copy()
    else:
        active = combined.copy()

    if len(active) > record_window():
        active = active.tail(record_window()).reset_index(drop=True)

    total = len(active)

    # No new records after last stop → still BLOCKED, preserve last_60
    if total == 0:
        blocked_min = 0
        if last_stop_ts:
            blocked_min = int(
                (datetime.now() - last_stop_ts).total_seconds() / 60)
        status = "BLOCKED" if last_stop_ts else "RUNNING"
        stats = {
            "label":         label,
            "ft_id":         ft_id(),
            "status":        status,
            "total":         0,
            "fails":         0,
            "rate":          0.0,
            "fails_60":      fails_60,
            "rate_60":       rate_60,
            "last_stop":     last_stop_str,
            "last_data":     latest_ts.strftime("%H:%M:%S"),
            "blocked_min":   blocked_min,
            "files_today":   len(all_files),
            "minutes_since": int(minutes_since),
        }
        save_stats(stats)
        return stats

    fails      = int(active[active["RESULT"].str.upper() == "FAIL"].shape[0])
    pass_count = int(active[active["RESULT"].str.upper() == "PASS"].shape[0])
    rate       = (fails / total * 100) if total > 0 else 0.0

    # ── Status determination ─────────────────────────────────────
    if fails >= block_at_fail():
        status = "BLOCKED"
    elif last_stop_ts is not None and pass_count == 0 and fails > 0:
        status = "BLOCKED"
    elif fails >= warn_at_fail():
        status = "WARNING"
    else:
        status = "RUNNING"

    blocked_min = 0
    if status == "BLOCKED" and last_stop_ts is not None:
        blocked_min = int(
            (datetime.now() - last_stop_ts).total_seconds() / 60)

    # ── STOP signal — sent exactly once per blocking event ───────
    # Fix Bug 4: correct already_stopped logic.
    # We should NOT send STOP if:
    #   - last_stop_ts already exists (STOP was already sent), AND
    #   - there are no NEW fails in the active window AFTER last_stop
    #     i.e. the machine was properly blocked and hasn't produced
    #     new fails since then.
    # We SHOULD send STOP if:
    #   - status is BLOCKED, AND
    #   - last_stop_ts is None (first time), OR
    #   - fails > 0 in active window that started AFTER last_stop
    #     (new blocking event — machine was unblocked then failed again)
    new_fails_after_stop = (
        fails > 0 and
        last_stop_ts is not None
    )
    first_block = (last_stop_ts is None and status == "BLOCKED")
    should_send = first_block or (status == "BLOCKED" and new_fails_after_stop)

    if should_send:
        logger.warning(
            f"[ft_process] {label} — BLOCKED: {fails} fails / "
            f"{total} records ({rate:.1f}%). Sending STOP to Main PC.")

        # Check pending stop from previous failed attempt
        pending = _load_pending_stop()
        if pending:
            logger.info(
                f"[ft_process] {label} — retrying previously "
                f"failed STOP signal.")

        sent = send_stop_signal()
        if sent:
            ts = datetime.now()
            save_last_stop(ts)
            _clear_pending_stop()
            logger.info(
                f"[ft_process] {label} — STOP sent OK. "
                f"last_stop saved: {ts.isoformat()}")
        else:
            _save_pending_stop(fails, rate, fails_60, rate_60)
            logger.error(
                f"[ft_process] {label} — STOP signal FAILED. "
                f"Saved as pending — will retry next poll.")

    logger.info(
        f"[ft_process] {label} — {status} | "
        f"fails={fails}/{total} ({rate:.1f}%) | "
        f"f60={fails_60} r60={rate_60:.1f}% | "
        f"files={len(all_files)}")

    stats = {
        "label":         label,
        "ft_id":         ft_id(),
        "status":        status,
        "total":         total,
        "fails":         fails,
        "rate":          rate,
        "fails_60":      fails_60,
        "rate_60":       rate_60,
        "last_stop":     last_stop_str,
        "last_data":     latest_ts.strftime("%H:%M:%S"),
        "blocked_min":   blocked_min,
        "files_today":   len(all_files),
        "minutes_since": int(minutes_since),
    }
    save_stats(stats)
    return stats


def _empty_stats(status="STOPPED", last_stop=None) -> dict:
    return {
        "label":         ft_display_label(),
        "ft_id":         ft_id(),
        "status":        status,
        "total":         0,
        "fails":         0,
        "rate":          0.0,
        "fails_60":      0,
        "rate_60":       0.0,
        "last_stop":     _format_last_stop(last_stop),
        "last_data":     "—",
        "blocked_min":   0,
        "files_today":   0,
        "minutes_since": 0,
    }


# =========================================================
# Entry point — runs 24/7
# =========================================================
if __name__ == "__main__":
    interval = poll_interval()
    label    = ft_display_label()
    logger.info(
        f"[ft_process] {label} monitor started. "
        f"Scanning every {interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            scan_and_check()
        except Exception as e:
            logger.error(f"[ft_process] Unhandled error in scan: {e}",
                         exc_info=True)
        time.sleep(interval)
