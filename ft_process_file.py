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
import time
import logging
import pandas as pd
from datetime import datetime, date

from ft_config_loader import (
    log_dir, log_reg_dir, poll_interval, max_records,
    warn_at_fail, block_at_fail, ft_label, ft_number, ft_side
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

    logger = logging.getLogger("ft_process_file")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = DailyFileHandler(f"ft_process_{ft_label().replace(' ','_')}",
                              ldir, retention_days=7)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    return logger

logger = _setup_logger()


# =========================================================
# Last stop tracker — simple JSON per FT PC
# =========================================================
def _last_stop_path() -> str:
    return os.path.join(log_reg_dir(),
                        f"ft{ft_number()}_{ft_side()}_last_stop.json")


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
            json.dump({"last_stop": ts.isoformat()}, f)
    except Exception as e:
        logger.error(f"[ft_process] Failed to save last_stop: {e}")


# =========================================================
# Read a single CSV file — same format as DL PC
# =========================================================
def read_csv_file(filepath: str):
    sections        = []
    current_section = []
    rows            = []
    current         = {}

    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#INIT"):
                    if current_section:
                        sections.append(current_section)
                    current_section = [line]
                else:
                    current_section.append(line)
        if current_section:
            sections.append(current_section)
    except Exception as e:
        logger.error(f"[ft_process] Could not read {filepath}: {e}")
        return None

    for section in sections:
        for line in section:
            if line.startswith("RESULT:"):
                current["RESULT"] = line.split("RESULT:")[1].strip()
            if line.startswith("TIME :"):
                current["Update_Time"] = line.split("TIME :")[1].strip()
            if line.startswith("DATE :"):
                current["DATE"] = line.split("DATE :")[1].strip()
            if line.startswith("ARRAY :"):
                current["ARRAY"] = line.split("ARRAY :")[1].strip()
            if line.startswith("JIG :"):
                current["JIG"] = line.split("JIG :")[1].strip()
            if all(k in current for k in
                   ["DATE", "Update_Time", "JIG", "ARRAY", "RESULT"]):
                rows.append(current)
                current = {}

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["DATE", "Update_Time",
                                     "JIG", "ARRAY", "RESULT"])
    df["Time_stamp"] = pd.to_datetime(
        df["DATE"] + " " + df["Update_Time"], errors="coerce")
    return df.sort_values("Time_stamp").reset_index(drop=True)


def is_today(filepath: str) -> bool:
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    return mtime.date() == date.today()


# =========================================================
# Main scan
# =========================================================
def scan_and_check() -> dict:
    """
    Scans all CSV files in log_dir, combines today's data
    across all stations (_01–_06), computes fail stats,
    and sends STOP to Main PC if threshold is hit.

    Returns stats dict for ft_dashboard.py to display.
    """
    directory = log_dir()
    label     = ft_label()

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
        return _empty_stats("STOPPED")

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

    # Filter records after last stop
    last_stop_ts = get_last_stop()
    if last_stop_ts is not None:
        active = combined[combined["Time_stamp"] >
                          pd.Timestamp(last_stop_ts)]
    else:
        active = combined

    if len(active) > max_records():
        active = active.tail(max_records()).reset_index(drop=True)

    total = len(active)
    if total == 0:
        return _empty_stats(
            "BLOCKED" if last_stop_ts else "RUNNING",
            last_stop=last_stop_ts
        )

    fails      = int(active[active["RESULT"].str.upper() == "FAIL"].shape[0])
    pass_count = int(active[active["RESULT"].str.upper() == "PASS"].shape[0])
    rate       = (fails / total * 100) if total > 0 else 0.0

    # Last-60 for display even when blocked
    last60   = combined.tail(60)
    fails_60 = int(last60[last60["RESULT"].str.upper() == "FAIL"].shape[0])
    rate_60  = (fails_60 / len(last60) * 100) if len(last60) > 0 else 0.0

    # Determine status
    if last_stop_ts is not None and pass_count == 0:
        status = "BLOCKED"
    elif fails >= block_at_fail():
        status = "BLOCKED"
    elif fails >= warn_at_fail():
        status = "WARNING"
    else:
        status = "RUNNING"

    blocked_since_min = 0
    if status == "BLOCKED" and last_stop_ts is not None:
        blocked_since_min = int(
            (datetime.now() - last_stop_ts).total_seconds() / 60)

    # Send STOP signal when threshold hit
    if status == "BLOCKED" and fails >= block_at_fail():
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

    stats = {
        "label":              label,
        "status":             status,
        "total":              total,
        "fails":              fails,
        "rate":               rate,
        "fails_60":           fails_60,
        "rate_60":            rate_60,
        "last_stop":          last_stop_ts.strftime("%H:%M:%S")
                              if last_stop_ts else "—",
        "last_data":          latest_ts.strftime("%H:%M:%S"),
        "blocked_since_min":  blocked_since_min,
        "files_today":        len(all_files),
    }
    logger.info(
        f"[ft_process] {label} — {status} | "
        f"fails={fails}/{total} ({rate:.1f}%) | "
        f"files={len(all_files)}")
    return stats


def _empty_stats(status="STOPPED", last_stop=None) -> dict:
    return {
        "label":             ft_label(),
        "status":            status,
        "total":             0,
        "fails":             0,
        "rate":              0.0,
        "fails_60":          0,
        "rate_60":           0.0,
        "last_stop":         last_stop.strftime("%H:%M:%S")
                             if last_stop else "—",
        "last_data":         "—",
        "blocked_since_min": 0,
        "files_today":       0,
    }


# =========================================================
# Entry point — runs 24/7
# =========================================================
if __name__ == "__main__":
    interval = poll_interval()
    logger.info(
        f"[ft_process] {ft_label()} monitor started. "
        f"Scanning every {interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            scan_and_check()
        except Exception as e:
            logger.error(f"[ft_process] Unhandled error: {e}")
        time.sleep(interval)