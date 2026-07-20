"""
ft_process_file.py  —  FT PC
Monitors CSV log files in log_dir, combines all stations (_01 to _06),
counts fails in the active window, and sends STOP to Main PC when
combined fails hit block_at_fails threshold.

CSV filename pattern: <any_prefix>_01.csv ... <any_prefix>_06.csv
CSV format: same as DL PC (DATE, TIME, JIG, ARRAY, RESULT columns)

Run continuously:
    python ft_process_file.py
"""

import os
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


# =========================================================
# Last stop tracker — one shared timestamp for this FT PC
# =========================================================
def _last_stop_path() -> str:
    safe_id = ft_id().replace("/", "_")
    return os.path.join(log_reg_dir(), f"ft_{safe_id}_last_stop.json")


def _stats_path() -> str:
    """Path to the latest stats JSON — read by ft_dashboard.py."""
    safe_id = ft_id().replace("/", "_")
    return os.path.join(log_reg_dir(), f"ft_{safe_id}_stats.json")


def _pending_stop_path() -> str:
    """Path to pending stop flag — written when STOP send fails."""
    safe_id = ft_id().replace("/", "_")
    return os.path.join(log_reg_dir(), f"ft_{safe_id}_pending_stop.json")


def _save_pending_stop(fails, rate, fails_60, rate_60) -> None:
    """Save blocked stats so they survive to next poll retry."""
    path = _pending_stop_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "fails":    fails,
                "rate":     rate,
                "fails_60": fails_60,
                "rate_60":  rate_60,
                "ts":       datetime.now().isoformat(),
            }, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save pending_stop: {e}")


def _load_pending_stop() -> dict:
    """Load pending stop — returns dict if exists, else None."""
    path = _pending_stop_path()
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _clear_pending_stop() -> None:
    """Remove pending stop flag after successful send."""
    path = _pending_stop_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def save_stats(stats: dict) -> None:
    """Save latest stats to JSON so dashboard can read without re-scanning."""
    path = _stats_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stats, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save stats: {e}")


def load_stats() -> dict:
    """Load latest stats JSON — called by ft_dashboard.py."""
    path = _stats_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return _empty_stats("STOPPED")


def get_last_stop():
    import json
    path = _last_stop_path()
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            ts = data.get("last_stop")
            if ts:
                return datetime.fromisoformat(ts)
    except Exception:
        pass
    return None


def save_last_stop(ts: datetime) -> None:
    import json
    path = _last_stop_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            json.dump({
                "last_stop": ts.isoformat(),
                "ft_id":     ft_id(),
            }, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save last_stop: {e}")


# =========================================================
# Read a single FT CSV file
# Same structure as DL PC reader but adapted for FT format:
#   - RESULT : PASS/FAIL  (space before colon, unlike DL)
#   - No ARRAY field
#   - DATE : 2026/07/08   (slashes, not dashes)
# =========================================================
def read_csv_file(filepath: str):
    sections        = []
    current_section = []
    rows            = []
    current         = {}

    current_section = []
    try:
        with open(filepath, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#INIT"):
                    if current_section:
                        sections.append(current_section)
                        current_section = []
                    current_section.append(line)
                else:
                    current_section.append(line)
        if current_section:
            sections.append(current_section)
    except Exception as e:
        logger.error(f"[ft_process] Could not read {filepath}: {e}")
        return None

    for section in sections:
        for line in section:
            if line.startswith("RESULT :"):
                current["RESULT"] = line.split("RESULT :")[1].strip()
            if line.startswith("TIME :"):
                current["Update_Time"] = line.split("TIME :")[1].strip()
            if line.startswith("DATE :"):
                current["DATE"] = line.split("DATE :")[1].strip()
            if line.startswith("JIG :"):
                current["JIG"] = line.split("JIG :")[1].strip()
            # FT has no ARRAY field — collect on DATE+TIME+JIG+RESULT
            if all(k in current for k in
                   ["DATE", "Update_Time", "JIG", "RESULT"]):
                rows.append(current)
                current = {}

    if not rows:
        return None

    data = pd.DataFrame(rows, columns=["DATE", "Update_Time",
                                       "JIG", "RESULT"])
    # DATE format: 2026/07/08 — pd.to_datetime handles slashes
    data["Time_stamp"] = pd.to_datetime(
        data["DATE"] + " " + data["Update_Time"], errors="coerce")
    sorted_df = data.sort_values(by="Time_stamp")
    return sorted_df


def is_today(filepath: str) -> bool:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        return mtime.date() == date.today()
    except Exception:
        return False


# =========================================================
# Main scan
# =========================================================
def scan_and_check() -> dict:
    """
    Scans all CSV files in log_dir, combines today's data
    across all stations (_01-_06), computes fail stats,
    and sends ONE STOP to Main PC if combined fails hit threshold.

    Returns stats dict for ft_dashboard.py to display.
    """
    directory = log_dir()
    label     = ft_display_label()

    if not os.path.isdir(directory):
        logger.error(f"[ft_process] Log dir not found: {directory}")
        return _empty_stats("NO DIR")

    # Collect all today's CSV files regardless of prefix
    all_files = [
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.endswith(".csv") and is_today(
            os.path.join(directory, f))
    ]
    if not all_files:
        logger.debug(f"[ft_process] {label} — no CSV files today")
        s = _empty_stats("STOPPED")
        save_stats(s)
        return s

    # Combine all station files
    combined = pd.DataFrame()
    for fpath in all_files:
        df = read_csv_file(fpath)
        if df is not None:
            combined = pd.concat([combined, df], ignore_index=True)
    if combined.empty:
        return _empty_stats("STOPPED")

    combined  = combined.sort_values("Time_stamp").reset_index(drop=True)
    latest_ts = combined["Time_stamp"].max()

    try:
        minutes_since=(datetime.now()-latest_ts).total_seconds()/60
    except Exception:
        minutes_since=0
    
    if minutes_since>=60:
        logger.info(
            f"[ft_process] {label} - no data for {int(minutes_since)}min -> STOPPED"
        )
        return _empty_stats("STOPPED")
    last_stop_ts = get_last_stop()

    # ── Last-60 from FULL combined data ────────────────────────
    # Always computed from all today's data regardless of last_stop.
    # This ensures fails_60/rate_60 never reset when a STOP is sent.
    last60   = combined.tail(60)
    fails_60 = int(last60[last60["RESULT"].str.upper() == "FAIL"].shape[0])
    rate_60  = (fails_60 / len(last60) * 100) if len(last60) > 0 else 0.0

    last_stop_str = "—"
    if last_stop_ts:
        last_stop_str = (
            last_stop_ts.strftime("%H:%M:%S")
            if last_stop_ts.date() == date.today()
            else last_stop_ts.strftime("%b %d  %H:%M:%S")
        )

    # ── Active window — records AFTER last stop ─────────────────
    if last_stop_ts is not None:
        active = combined[combined["Time_stamp"] >
                          pd.Timestamp(last_stop_ts)]
    else:
        active = combined

    if len(active) > record_window():
        active = active.tail(record_window()).reset_index(drop=True)

    total = len(active)
    if total == 0:
        # No new records after last stop — still BLOCKED
        # But show fails_60/rate_60 from full data
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

    # Determine status
    # BLOCKED if:
    #   a) fails hit block threshold in the active window, OR
    #   b) last_stop exists and no new PASS since then (total=0 caught above)
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

    # ── Send STOP only once when first hitting threshold ──────
    # Only send if last_stop_ts is None or if new fails AFTER last stop
    # This prevents re-sending STOP on every poll while still BLOCKED
    already_stopped = (
        last_stop_ts is not None and
        active[active["RESULT"].str.upper() == "FAIL"].shape[0] == 0
    )
    should_send = status == "BLOCKED" and not already_stopped

    if should_send:
        logger.warning(
            f"[ft_process] {label} — BLOCKED: {fails} fails / "
            f"{total} records ({rate:.1f}%). Sending STOP to Main PC.")
        sent = send_stop_signal()
        if sent:
            ts = datetime.now()
            save_last_stop(ts)
            logger.info(
                f"[ft_process] {label} — STOP sent OK. "
                f"last_stop saved: {ts.isoformat()}")
        else:
            logger.error(
                f"[ft_process] {label} — STOP signal FAILED. "
                f"Will retry next poll.")

    logger.info(
        f"[ft_process] {label} — {status} | "
        f"fails={fails}/{total} ({rate:.1f}%) | "
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
    # Save stats so ft_dashboard can read without re-scanning
    save_stats(stats)
    return stats


def _empty_stats(status="STOPPED", last_stop=None) -> dict:
    last_stop_str = "—"
    if last_stop:
        if last_stop.date() == date.today():
            last_stop_str = last_stop.strftime("%H:%M:%S")
        else:
            last_stop_str = last_stop.strftime("%b %d  %H:%M:%S")
    return {
        "label":         ft_display_label(),
        "ft_id":         ft_id(),
        "status":        status,
        "total":         0,
        "fails":         0,
        "rate":          0.0,
        "fails_60":      0,
        "rate_60":       0.0,
        "last_stop":     last_stop_str,
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
            logger.error(f"[ft_process] Unhandled error: {e}")
        time.sleep(interval)