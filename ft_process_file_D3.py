"""
ft_process_file.py  —  FT PC (DL PC architecture)

Behaves exactly like DL_PC/process_file.py: ONE shared log_dir holds
CSV files for every station, named "..._YYYYMMDD_SSN.csv" where the
3-digit suffix is SS = station number (01-08) and N = substation (1-6).
Each station has 6 substations, and all 6 of their files merge into
that single station:

    _011 _012 _013 _014 _015 _016  ->  station 01  (F1)
    _081 _082 _083 _084 _085 _086  ->  station 08  (R4)

Files are bucketed by that station key each scan, then the SAME
detection engine used before (read -> merge -> last-60 -> active
window -> status -> STOP-if-required -> save stats) runs once per
station over its merged substation data. The only FT-specific piece
is the station list, which comes from setup_type (6 or 8 stations,
front F/ rear R) via ft_config_loader.ft_stations() — everything
else is unchanged from the original algorithm.

Architecture (per station, same as before):
  read_csv_files()           — read that station's CSVs for today
      ↓
  merge_records()            — deduplicate, sort by timestamp
      ↓
  calculate_last60()         — INDEPENDENT of STOP state, always latest 60
      ↓
  calculate_active_window()  — records strictly after last_stop only
      ↓
  calculate_status()         — derive RUNNING/WARNING/BLOCKED
      ↓
  send_stop_if_required()    — exactly once per blocking event, over the
                                network to Main PC (same as before)
      ↓
  save_stats()               — atomic JSON write for dashboard, one file
                                per station

Run continuously (one process covers all configured stations):
    python ft_process_file.py
"""

import os
import re
import sys
import json
import time
import logging
import pandas as pd
from datetime import datetime, date
from typing import Optional

from ft_config_loader_D3 import (
    log_dir, log_reg_dir, poll_interval, record_window,
    warn_at_fail, block_at_fail, no_data_minutes,
    ft_stations, station_display, station_rack_function, setup_type,
    heartbeat_sec,
)
from ft_network_sender_D3 import send_stop_signal, send_hello_all
from log_cleanup_D3 import cleanup_old_logs, DailyFileHandler


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
        fh = DailyFileHandler(
            "ft_process", ldir, retention_days=7)
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
# Path helpers — namespaced by station id so all stations on
# this one PC safely share the same log_reg_dir (same naming
# scheme as before, just parametrized instead of global).
# =========================================================
def _safe_id(station: str) -> str:
    return station.replace("/", "_").replace("\\", "_")


def _last_stop_path(station: str) -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id(station)}_last_stop.json")


def _stats_path(station: str) -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id(station)}_stats.json")


def _pending_stop_path(station: str) -> str:
    return os.path.join(log_reg_dir(), f"ft_{_safe_id(station)}_pending_stop.json")


# =========================================================
# Pending stop — survives failed network sends
# =========================================================
def _save_pending_stop(station: str, fails: int, rate: float,
                       fails_60: int, rate_60: float) -> None:
    path = _pending_stop_path(station)
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
        logger.error(f"[ft_process] {station} — Failed to save pending_stop: {e}")


def _load_pending_stop(station: str) -> Optional[dict]:
    path = _pending_stop_path(station)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _clear_pending_stop(station: str) -> None:
    path = _pending_stop_path(station)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# =========================================================
# Stats persistence
# Dashboard reads this JSON — never touches CSV files
# =========================================================
def save_stats(station: str, stats: dict) -> None:
    """
    Atomic write: write to .tmp then os.replace() so the dashboard
    never reads a half-written file even if we crash mid-write.
    """
    path = _stats_path(station)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[ft_process] {station} — Failed to save stats: {e}")


def load_stats(station: str) -> dict:
    """Called by ft_dashboard.py — reads JSON only, no CSV processing."""
    path = _stats_path(station)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return _empty_stats(station, "STOPPED")


def _load_last_saved_last60(station: str) -> tuple:
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
        stats = load_stats(station)
        return int(stats.get("fails_60", 0)), float(stats.get("rate_60", 0.0))
    except Exception:
        return 0, 0.0


# =========================================================
# Last stop tracker
# =========================================================
def get_last_stop(station: str) -> Optional[datetime]:
    path = _last_stop_path(station)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("last_stop")
            if ts:
                return datetime.fromisoformat(ts)
    except Exception as e:
        logger.warning(f"[ft_process] {station} — Could not read last_stop: {e}")
    return None


def save_last_stop(station: str, ts: datetime) -> None:
    path = _last_stop_path(station)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_stop": ts.isoformat(), "ft_id": station}, f)
    except Exception as e:
        logger.error(f"[ft_process] {station} — Failed to save last_stop: {e}")


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


def read_csv_files(files: list) -> tuple:
    """
    Step 1: Read a pre-selected list of today's CSV files for one station.
    Returns (all_files: list[str], frames: list[DataFrame])
    """
    frames = []
    for fpath in files:
        df = read_csv_file(fpath)
        if df is not None and not df.empty:
            frames.append(df)

    return files, frames


def bucket_files_by_station(directory: str, stations: list) -> dict:
    """
    DL-PC-style bucketing: ONE shared log_dir holds today's CSVs for
    every station, named "..._YYYYMMDD_SSN.csv" where the 3-digit
    suffix is SS = station number (01-08) and N = substation (1-6).

    Each station has 6 substations whose files ALL merge into that one
    station, e.g.:
        _011 _012 _013 _014 _015 _016  ->  station 01  (F1)
        _081 _082 _083 _084 _085 _086  ->  station 08  (R4)

    So the station key is basename[-7:-5] — identical to
    DL_PC/process_file.py, which buckets DL01..DL20 the same way.

    stations[i] corresponds to station number (i+1).
    Returns {station_name: [filepaths]}.
    """
    buckets = {s: [] for s in stations}
    try:
        all_files = sorted([
            os.path.join(directory, fn)
            for fn in os.listdir(directory)
            if fn.endswith(".csv") and
            is_today(os.path.join(directory, fn))
        ])
    except OSError as e:
        logger.error(f"[ft_process] Cannot list log dir: {e}")
        return buckets

    for fpath in all_files:
        # 2-digit station number, ignoring the trailing substation digit
        key = os.path.basename(fpath)[-7:-5]
        try:
            index = int(key) - 1
        except ValueError:
            logger.debug(
                f"[ft_process] Skipping file with no station suffix: "
                f"{os.path.basename(fpath)}")
            continue
        if 0 <= index < len(stations):
            buckets[stations[index]].append(fpath)
        else:
            logger.debug(
                f"[ft_process] Station {key} outside configured range "
                f"(1-{len(stations)}): {os.path.basename(fpath)}")

    for st, fl in buckets.items():
        if fl:
            logger.debug(f"[ft_process] {st}: {len(fl)} substation file(s)")

    return buckets


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
def send_stop_if_required(station: str, status: str, fails: int, rate: float,
                          fails_60: int, rate_60: float,
                          last_stop_ts: Optional[datetime],
                          label: str) -> Optional[datetime]:
    """
    Step 6: Send STOP signal to Main PC if conditions are met.
    Unchanged logic from before — only now takes a station argument
    so it can be called once per station in the scan loop.

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

    pending = _load_pending_stop(station)
    if pending:
        logger.info(f"[ft_process] {label} — retrying pending STOP signal")

    logger.warning(
        f"[ft_process] {label} — BLOCKED: {fails} fails "
        f"({rate:.1f}%). Sending STOP to Main PC.")

    sent = send_stop_signal(station)
    if sent:
        new_ts = datetime.now()
        save_last_stop(station, new_ts)
        _clear_pending_stop(station)
        logger.info(
            f"[ft_process] {label} — STOP sent OK "
            f"at {new_ts.strftime('%H:%M:%S')}")
        return new_ts
    else:
        _save_pending_stop(station, fails, rate, fails_60, rate_60)
        logger.error(
            f"[ft_process] {label} — STOP FAILED — saved pending, "
            f"will retry next poll")
        return None


# =========================================================
# scan_and_check_station() — orchestrates all steps for ONE
# station's already-bucketed file list. Steps 1-7 are exactly
# the same algorithm as before — only the outer loop changed.
# =========================================================
def scan_and_check_station(station: str, files: list) -> dict:
    label = station_display(station)

    # ── Load previously saved Last-60 values ───────────────
    # These are returned in all early-return paths so that fails_60
    # and rate_60 are NEVER reset to 0 due to idle time or missing files.
    # They only change when new production records arrive in Step 3.
    saved_fails_60, saved_rate_60 = _load_last_saved_last60(station)

    # ── Step 1: Read this station's CSV files ───────────────
    all_files, frames = read_csv_files(files)

    if not all_files:
        logger.debug(f"[ft_process] {label} — no CSV files today")
        s = _empty_stats(station, "STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(station, s)
        return s

    if not frames:
        logger.warning(f"[ft_process] {label} — all CSV files unreadable")
        s = _empty_stats(station, "STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(station, s)
        return s

    # ── Step 2: Merge and deduplicate ──────────────────────
    combined = merge_records(frames)
    if combined is None:
        s = _empty_stats(station, "STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(station, s)
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
        s = _empty_stats(station, "STOPPED",
                         fails_60=saved_fails_60, rate_60=saved_rate_60)
        save_stats(station, s)
        return s

    last_stop_ts  = get_last_stop(station)
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
            station=station, label=label, status=status,
            total=0, fails=0, rate=0.0,
            fails_60=fails_60, rate_60=rate_60,
            last_stop_str=last_stop_str,
            latest_ts=latest_ts,
            blocked_min=blocked_min,
            files_today=len(all_files),
            minutes_since=int(minutes_since),
        )
        save_stats(station, stats)
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
        station, status, fails, rate, fails_60, rate_60, last_stop_ts, label)
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
        station=station, label=label, status=status,
        total=total, fails=fails, rate=rate,
        fails_60=fails_60, rate_60=rate_60,
        last_stop_str=last_stop_str,
        latest_ts=latest_ts,
        blocked_min=blocked_min,
        files_today=len(all_files),
        minutes_since=int(minutes_since),
    )
    save_stats(station, stats)
    return stats


# =========================================================
# scan_and_check() — DL-PC-style outer loop: ONE shared
# log_dir, bucketed by station, same as DL_PC/process_file.py
# looping over DL_COUNT() DLs from one shared directory.
# =========================================================
def scan_and_check() -> dict:
    """
    Main entry point — orchestrates all stations for one poll cycle.
    Returns a dict of {station: stats} for convenience/testing.
    """
    directory = log_dir()
    stations  = ft_stations()

    if not os.path.isdir(directory):
        logger.error(f"[ft_process] Log dir not found: {directory}")
        results = {}
        for station in stations:
            saved_fails_60, saved_rate_60 = _load_last_saved_last60(station)
            s = _empty_stats(station, "NO DIR",
                             fails_60=saved_fails_60, rate_60=saved_rate_60)
            save_stats(station, s)
            results[station] = s
        return results

    buckets = bucket_files_by_station(directory, stations)

    results = {}
    for station in stations:
        try:
            results[station] = scan_and_check_station(station, buckets.get(station, []))
        except Exception as e:
            logger.error(f"[ft_process] {station_display(station)} — unhandled error: {e}", exc_info=True)
    return results


def _build_stats(*, station, label, status, total, fails, rate,
                 fails_60, rate_60, last_stop_str, latest_ts,
                 blocked_min, files_today, minutes_since) -> dict:
    """Build the stats dict that is written to JSON and read by dashboard."""
    try:
        last_data = latest_ts.strftime("%H:%M:%S")
    except Exception:
        last_data = "—"
    return {
        "label":         label,
        "ft_id":         station,
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


def _empty_stats(station: str, status: str = "STOPPED",
                 last_stop: Optional[datetime] = None,
                 fails_60: int = 0,
                 rate_60: float = 0.0) -> dict:
    """
    Returns a minimal stats dict for error/idle states.
    fails_60 and rate_60 are passed in from the last saved values
    so they are never reset to 0 in early-return paths.
    """
    return {
        "label":         station_display(station),
        "ft_id":         station,
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
        logger.info(f"[ft_process] shutdown signal {signum} received. Stopping.")
        import sys
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass


# =========================================================
# Heartbeat HELLO
# The worker runs 24/7, the dashboard may be closed — so the
# worker is what keeps Main PC's FT dots green. One bulk HELLO
# covers every configured station (Main PC marks them all from
# a single message).
# =========================================================
_last_hello_ts = 0.0


def _send_heartbeat_hello(force: bool = False) -> None:
    global _last_hello_ts
    interval = heartbeat_sec()
    now = time.time()
    if not force and (now - _last_hello_ts) < interval:
        return
    stations = ft_stations()
    ok = send_hello_all(stations)
    _last_hello_ts = now
    if ok:
        logger.info(f"[ft_process] HELLO sent for {stations} — Main PC ACKed")
    else:
        logger.warning(
            "[ft_process] HELLO failed — Main PC unreachable; "
            "FT dots will show disconnected")


# =========================================================
# Entry point — runs 24/7 (same shape as DL_PC/process_file.py)
# =========================================================
if __name__ == "__main__":
    _hide_console()
    _setup_signal_handlers()

    # ── Single instance — prevent two workers on same FT PC ──
    try:
        from tray_utils_D3 import SingleInstance
        _si = SingleInstance("FTProcess")
        if not _si.acquire():
            logger.error("[ft_process] Another instance already running. Exiting.")
            sys.exit(1)
    except Exception as _e:
        logger.warning(f"[ft_process] SingleInstance check failed: {_e}")
        _si = None

    interval = poll_interval()
    stations = ft_stations()
    logger.info(
        f"[ft_process] setup_type={setup_type()} — monitoring stations "
        f"{stations}. Scanning every {interval}s.")

    # Announce ourselves immediately so Main PC lights up on startup
    _send_heartbeat_hello(force=True)

    try:
        while True:
            try:
                _send_heartbeat_hello()
                scan_and_check()
            except Exception as e:
                logger.error(
                    f"[ft_process] Unhandled error: {e}", exc_info=True)
            time.sleep(interval)
    finally:
        if _si:
            _si.release()
