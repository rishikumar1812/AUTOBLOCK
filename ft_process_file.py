"""
ft_process_file.py  —  FT PC
Monitors CSV log files per station (_01 to _06) independently.
Sends ONE STOP to Main PC when ANY station hits block_at_fails
in its last `record_window` records since last_stop.

Design:
  - One shared last_stop timestamp for the whole FT PC
  - Each station's last `record_window` records checked independently
  - First station to hit threshold → STOP sent → last_stop saved
  - All stations reset their active window from that point

CSV filename: <any_prefix>_01.csv ... _06.csv
CSV format:   same as DL PC (#INIT sections, DATE/TIME/JIG/ARRAY/RESULT)

Run continuously:
    python ft_process_file.py
"""

import os
import re
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
def _tracker_path() -> str:
    safe_id = ft_id().replace("/", "_")
    return os.path.join(log_reg_dir(), f"ft_{safe_id}_last_stop.json")


def get_last_stop():
    import json
    path = _tracker_path()
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
    path = _tracker_path()
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
# CSV reader — same format as DL PC
# =========================================================
def read_csv_file(filepath: str):
    """
    Parse FT CSV file (same #INIT section format as DL PC).
    Returns DataFrame with columns: DATE, Update_Time, JIG, ARRAY,
    RESULT, Time_stamp — or None if file is empty/unreadable.
    """
    sections        = []
    current_section = []
    rows            = []
    current         = {}

    try:
        with open(filepath, "r", errors="replace") as f:
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
        logger.error(f"[ft_process] Cannot read {filepath}: {e}")
        return None

    for section in sections:
        for line in section:
            if line.startswith("RESULT:"):
                current["RESULT"] = line.split("RESULT:")[1].strip()
            elif line.startswith("TIME :"):
                current["Update_Time"] = line.split("TIME :")[1].strip()
            elif line.startswith("DATE :"):
                current["DATE"] = line.split("DATE :")[1].strip()
            elif line.startswith("ARRAY :"):
                current["ARRAY"] = line.split("ARRAY :")[1].strip()
            elif line.startswith("JIG :"):
                current["JIG"] = line.split("JIG :")[1].strip()
            if all(k in current for k in
                   ["DATE", "Update_Time", "JIG", "ARRAY", "RESULT"]):
                rows.append(current)
                current = {}

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=[
        "DATE", "Update_Time", "JIG", "ARRAY", "RESULT"])
    df["Time_stamp"] = pd.to_datetime(
        df["DATE"] + " " + df["Update_Time"], errors="coerce")
    return df.sort_values("Time_stamp").reset_index(drop=True)


def is_today(filepath: str) -> bool:
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        return mtime.date() == date.today()
    except Exception:
        return False


def station_num(filepath: str):
    """Extract station number from filename e.g. 'prefix_03.csv' → 3"""
    m = re.search(r'_(\d{2})\.csv$', os.path.basename(filepath))
    return int(m.group(1)) if m else None


# =========================================================
# Main scan — called every poll_interval seconds
# =========================================================
def scan_and_check() -> dict:
    """
    Scan all today's station CSV files independently.
    Returns stats dict for ft_dashboard.py to display.

    Logic:
      1. Load shared last_stop timestamp
      2. For each station file (_01 to _06):
         a. Read records
         b. Filter to records after last_stop
         c. Take last record_window records
         d. Count fails — if >= block_at_fails → STOP
      3. On first station to hit threshold:
         → send one STOP to Main PC
         → save last_stop (all stations reset from here)
         → stop checking remaining stations
    """
    label     = ft_display_label()
    directory = log_dir()

    if not os.path.isdir(directory):
        logger.error(f"[ft_process] Log dir not found: {directory}")
        return _empty_stats("NO DIR")

    # Collect today's station files, sorted by station number
    all_files = sorted([
        os.path.join(directory, fn)
        for fn in os.listdir(directory)
        if fn.endswith(".csv") and is_today(
            os.path.join(directory, fn))
        and re.search(r'_\d{2}\.csv$', fn)
    ], key=lambda p: station_num(p) or 0)

    if not all_files:
        logger.debug(f"[ft_process] {label} — no CSV files today")
        return _empty_stats("STOPPED")

    last_stop_ts = get_last_stop()

    # Per-station stats for dashboard display
    station_stats = []
    overall_status = "RUNNING"
    triggered_by   = None

    for fpath in all_files:
        snum = station_num(fpath)
        df   = read_csv_file(fpath)
        if df is None or df.empty:
            station_stats.append({
                "station": snum, "total": 0, "fails": 0,
                "rate": 0.0, "status": "EMPTY"
            })
            continue

        # Filter records after last_stop
        if last_stop_ts is not None:
            active = df[df["Time_stamp"] >
                        pd.Timestamp(last_stop_ts)]
        else:
            active = df

        # Take last record_window records
        if len(active) > record_window():
            active = active.tail(record_window()).reset_index(drop=True)

        total = len(active)
        if total == 0:
            station_stats.append({
                "station": snum, "total": 0, "fails": 0,
                "rate": 0.0, "status": "BLOCKED"
            })
            if overall_status == "RUNNING":
                overall_status = "BLOCKED"
            continue

        fails = int(
            active[active["RESULT"].str.upper() == "FAIL"].shape[0])
        rate  = (fails / total * 100) if total > 0 else 0.0

        if fails >= block_at_fail():
            st_status = "BLOCKED"
        elif fails >= warn_at_fail():
            st_status = "WARNING"
        else:
            st_status = "RUNNING"

        station_stats.append({
            "station": snum, "total": total,
            "fails": fails, "rate": rate, "status": st_status
        })

        # First station to hit threshold triggers STOP
        if st_status == "BLOCKED" and triggered_by is None:
            triggered_by   = snum
            overall_status = "BLOCKED"

    # ── Send STOP if threshold hit ─────────────────────────
    if triggered_by is not None:
        logger.warning(
            f"[ft_process] {label} — Station {triggered_by:02d} "
            f"triggered STOP. Sending to Main PC..."
        )
        sent = send_stop_signal()
        ts   = datetime.now()
        if sent:
            save_last_stop(ts)
            logger.info(
                f"[ft_process] {label} — STOP sent OK. "
                f"last_stop={ts.isoformat()}"
            )
        else:
            logger.error(
                f"[ft_process] {label} — STOP send FAILED. "
                f"Will retry next poll."
            )
    elif overall_status == "RUNNING":
        # Check if any station is WARNING
        if any(s["status"] == "WARNING" for s in station_stats):
            overall_status = "WARNING"

    # Summary stats across all stations
    total_all = sum(s["total"] for s in station_stats)
    fails_all = sum(s["fails"] for s in station_stats)
    rate_all  = (fails_all / total_all * 100) if total_all > 0 else 0.0

    last_stop_str = (
        last_stop_ts.strftime("%b %d  %H:%M:%S")
        if last_stop_ts and last_stop_ts.date() != date.today()
        else last_stop_ts.strftime("%H:%M:%S")
        if last_stop_ts else "—"
    )

    blocked_min = 0
    if overall_status == "BLOCKED" and last_stop_ts:
        blocked_min = int(
            (datetime.now() - last_stop_ts).total_seconds() / 60)

    logger.info(
        f"[ft_process] {label} — {overall_status} | "
        f"stations={len(all_files)} | "
        f"fails={fails_all}/{total_all} ({rate_all:.1f}%)"
    )

    return {
        "label":        label,
        "ft_id":        ft_id(),
        "status":       overall_status,
        "total":        total_all,
        "fails":        fails_all,
        "rate":         rate_all,
        "last_stop":    last_stop_str,
        "files_today":  len(all_files),
        "blocked_min":  blocked_min,
        "triggered_by": triggered_by,
        "stations":     station_stats,
    }


def _empty_stats(status="STOPPED") -> dict:
    return {
        "label":        ft_display_label(),
        "ft_id":        ft_id(),
        "status":       status,
        "total":        0,
        "fails":        0,
        "rate":         0.0,
        "last_stop":    "—",
        "files_today":  0,
        "blocked_min":  0,
        "triggered_by": None,
        "stations":     [],
    }


# =========================================================
# Entry point — runs 24/7
# =========================================================
if __name__ == "__main__":
    interval = poll_interval()
    label    = ft_display_label()
    logger.info(
        f"[ft_process] {label} monitor started. "
        f"Scanning every {interval}s. Press Ctrl+C to stop."
    )
    while True:
        try:
            scan_and_check()
        except Exception as e:
            logger.error(f"[ft_process] Unhandled error: {e}")
        time.sleep(interval)
