"""
generate_ft_test_logs.py  —  FT PC test utility
Generates realistic CSV log files matching the real FT format
(SM-S938BE_SMD_FT_TOP20_YYYYMMDD_XX.csv) to test ft_process_file.py.

Each generated file matches the real FT CSV structure:
  - #INIT section with DATE, TIME, JIG and metadata headers
  - #TEST section (minimal test lines)
  - RESULT : PASS or RESULT : FAIL
  - TEST-TIME line at end of each record

Usage:
    python generate_ft_test_logs.py            # interactive menu
    python generate_ft_test_logs.py --clean    # delete test files only
"""

import os
import sys
import random
import argparse
from datetime import datetime, timedelta, date

# ── Load config ────────────────────────────────────────────
try:
    from ft_config_loader_D3 import log_dir, warn_at_fail, block_at_fail
    LOG_DIR  = log_dir()
    WARN_AT  = warn_at_fail()
    BLOCK_AT = block_at_fail()
except Exception as e:
    print(f"[warning] Could not load ft_config_loader: {e}")
    LOG_DIR  = "C:\\FT\\logs"
    WARN_AT  = 2
    BLOCK_AT = 4

NUM_STATIONS = 6
MODEL        = "SM-S938BE"
TESTCODE     = "TOP20"
PREFIX       = f"{MODEL}_SMD_FT_{TESTCODE}"


# =========================================================
# Write one FT CSV record (one #INIT block)
# Matches real FT CSV structure exactly
# =========================================================
def _write_record(f, ts: datetime, jig: int, result: str) -> None:
    """Write one test record in real FT CSV format."""
    date_str = ts.strftime("%Y/%m/%d")
    time_str = ts.strftime("%H:%M:%S")
    res_upper = result.upper()

    f.write("\r\n")
    f.write("#INIT\r\n")
    f.write(f"MODEL_LAST : \r\n")
    f.write(f"MODEL_PROGRAM : {MODEL}\r\n")
    f.write(f"MODEL : {MODEL[:-1]}\r\n")
    f.write(f"P/N : TEST{jig:02d}000000000000\r\n")
    f.write(f"S/W : TEST_SW_VER\r\n")
    f.write(f"DATE : {date_str}\r\n")
    f.write(f"TIME : {time_str}\r\n")
    f.write(f"TESTCODE : {TESTCODE}\r\n")
    f.write(f"LOGVERSION : Ver 2.4\r\n")
    f.write(f"INSTRUMENT : /N/A/N/A\r\n")
    f.write(f"JIG : {jig}\r\n")
    f.write(f"PROGRAM : TEST_PROGRAM_1.0\r\n")
    f.write(f"CN : TEST{ts.strftime('%H%M%S')}{jig:02d}\r\n")
    f.write(f"LINENAME : TEST_LINE\r\n")
    f.write("\r\n")
    f.write("#TEST\r\n")
    f.write("/*================\r\n")
    f.write("Test Conditions, Measured Value, Lower Limit, Upper Limit, P/F\r\n")
    f.write("================*/\r\n")
    f.write(f"SPEC Reload, Not Use, , , P, 30\r\n")
    f.write(f"CHECK_CONNECTION, OK, OK, OK, P, 30\r\n")
    if res_upper == "FAIL":
        f.write(f"SIGNAL_TEST, -99.00, -80.00, -10.00, F, 30\r\n")
    else:
        f.write(f"SIGNAL_TEST, -45.00, -80.00, -10.00, P, 30\r\n")
    f.write("\r\n")
    f.write(f"RESULT : {res_upper}\r\n")
    f.write(f"TEST-TIME : {random.randint(150, 220)}\r\n")
    f.write(f"RF TEST-TIME : 0\r\n")
    f.write(f"NON-RF TEST-TIME : {random.randint(150, 220)}\r\n")
    f.write(f"//Total : 1 Pass : {'1' if res_upper=='PASS' else '0'} "
            f"Fail : {'0' if res_upper=='PASS' else '1'}\r\n")
    f.write("\f\r\n")


# =========================================================
# Write one station CSV file
# =========================================================
def _write_station_file(station: int, records: list,
                        start_time: datetime = None,
                        substations: int = 6) -> str:
    """
    Write one station's CSV files with the given records, split across
    its substations — matching the real FT naming convention:

        {PREFIX}_{YYYYMMDD}_{SS}{N}.csv
          SS = station number (01-08), N = substation (1-6)

    e.g. station 1 -> _011 _012 _013 _014 _015 _016
         station 8 -> _081 _082 _083 _084 _085 _086

    ft_process_file merges all of a station's substation files back
    into that one station, so the records are dealt round-robin
    across the substations here.
    Returns the directory the files were written to.
    """
    today = date.today().strftime("%Y%m%d")
    os.makedirs(LOG_DIR, exist_ok=True)

    now      = start_time or datetime.now()
    interval = 30

    # Deal records round-robin into per-substation buckets
    buckets = {n: [] for n in range(1, substations + 1)}
    for i, result in enumerate(records):
        ts = now - timedelta(seconds=(len(records) - i) * interval)
        buckets[(i % substations) + 1].append((ts, result))

    written = 0
    for sub, rows in buckets.items():
        if not rows:
            continue
        filename = f"{PREFIX}_{today}_{station:02d}{sub}.csv"
        filepath = os.path.join(LOG_DIR, filename)
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            for ts, result in rows:
                _write_record(f, ts, station * 10 + sub, result)
        written += len(rows)

    print(f"  Written {written} records "
          f"({records.count('FAIL')} FAIL) → station {station:02d} "
          f"across {substations} substation files")
    return LOG_DIR


def _make_records(total: int, fails: int) -> list:
    """Generate shuffled list of PASS/FAIL results."""
    results = ["FAIL"] * fails + ["PASS"] * (total - fails)
    random.shuffle(results)
    return results


# =========================================================
# Scenarios
# =========================================================
def scenario_running(total=20, fails=0):
    print(f"\n[RUNNING] {total} records, {fails} fails per station "
          f"(warn≥{WARN_AT}, block≥{BLOCK_AT})")
    for n in range(1, NUM_STATIONS + 1):
        _write_station_file(n, _make_records(total, fails))
    print(f"\n  ✓ Status should show: RUNNING")


def scenario_warning(total=20):
    fails = WARN_AT
    print(f"\n[WARNING] {total} records, {fails} fails per station "
          f"(warn≥{WARN_AT}, block≥{BLOCK_AT})")
    for n in range(1, NUM_STATIONS + 1):
        _write_station_file(n, _make_records(total, fails))
    print(f"\n  ✓ Status should show: WARNING")


def scenario_blocked(total=20):
    fails = BLOCK_AT
    print(f"\n[BLOCKED] {total} records, {fails} fails per station")
    print(f"  ⚠  This WILL send a STOP signal to Main PC!")
    for n in range(1, NUM_STATIONS + 1):
        _write_station_file(n, _make_records(total, fails))
    print(f"\n  ✓ Status should show: BLOCKED → STOP sent to Main PC")


def scenario_no_data():
    """Write old files (>60 min ago) so STOPPED is triggered."""
    print(f"\n[NO DATA] Writing files with timestamps >60min ago")
    old_time = datetime.now() - timedelta(minutes=90)
    for n in range(1, NUM_STATIONS + 1):
        _write_station_file(n, _make_records(10, 0),
                            start_time=old_time)
    print(f"\n  ✓ Status should show: STOPPED (no data for 90min)")


def scenario_empty():
    """Delete all today's test files → STOPPED."""
    print(f"\n[EMPTY] Deleting today's test CSV files from {LOG_DIR}")
    today   = date.today().strftime("%Y%m%d")
    deleted = 0
    if os.path.isdir(LOG_DIR):
        for fn in os.listdir(LOG_DIR):
            if (fn.startswith(PREFIX) and
                    today in fn and fn.endswith(".csv")):
                os.remove(os.path.join(LOG_DIR, fn))
                print(f"  Deleted {fn}")
                deleted += 1
    if deleted == 0:
        print("  No test files found for today.")
    print(f"\n  ✓ Status should show: STOPPED")


def scenario_custom():
    print(f"\n[CUSTOM] (warn≥{WARN_AT}, block≥{BLOCK_AT})")
    try:
        total    = int(input("  Total records per station [20]: ").strip() or "20")
        fails    = int(input(f"  Fail count per station [0]:  ").strip() or "0")
        stations = int(input(f"  Number of stations [6]:      ").strip() or "6")
    except ValueError:
        print("  Invalid input — using defaults (20 total, 0 fails, 6 stations)")
        total, fails, stations = 20, 0, 6

    fails = min(fails, total)
    print(f"\n  Generating {stations} files: {total} records, {fails} fails each")
    for n in range(1, stations + 1):
        _write_station_file(n, _make_records(total, fails))


def clean_all():
    print(f"\n[CLEAN] Removing generated test CSV files from {LOG_DIR}")
    deleted = 0
    if os.path.isdir(LOG_DIR):
        for fn in os.listdir(LOG_DIR):
            if fn.startswith(PREFIX) and fn.endswith(".csv"):
                os.remove(os.path.join(LOG_DIR, fn))
                print(f"  Deleted {fn}")
                deleted += 1
    print(f"  Done — {deleted} file(s) removed.")


# =========================================================
# Menu
# =========================================================
MENU = """
╔═══════════════════════════════════════════════════════════╗
║          FT Process File — Test Log Generator             ║
╠═══════════════════════════════════════════════════════════╣
║  Log dir  : {log_dir:<41} ║
║  Warn ≥   : {warn:<41} ║
║  Block ≥  : {block:<41} ║
║  Model    : {model:<41} ║
╠═══════════════════════════════════════════════════════════╣
║  1.  RUNNING   — 0 fails, all stations OK                 ║
║  2.  WARNING   — fails at warn threshold                  ║
║  3.  BLOCKED   — fails at block threshold  (→ STOP)       ║
║  4.  NO DATA   — files older than 60min   (→ STOPPED)     ║
║  5.  EMPTY     — delete today's files     (→ STOPPED)     ║
║  6.  CUSTOM    — choose your own counts                   ║
║  7.  CLEAN     — delete all generated files               ║
║  0.  EXIT                                                 ║
╚═══════════════════════════════════════════════════════════╝
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true",
                        help="Delete all test CSVs and exit")
    args = parser.parse_args()

    if args.clean:
        clean_all()
        return

    print(MENU.format(
        log_dir=LOG_DIR[:41],
        warn=str(WARN_AT),
        block=str(BLOCK_AT),
        model=PREFIX[:41],
    ))

    actions = {
        "1": scenario_running,
        "2": scenario_warning,
        "3": scenario_blocked,
        "4": scenario_no_data,
        "5": scenario_empty,
        "6": scenario_custom,
        "7": clean_all,
        "0": None,
    }

    while True:
        try:
            choice = input("\nChoice [0-7]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice == "0":
            print("Exiting.")
            break

        action = actions.get(choice)
        if action is None:
            print(f"  Invalid choice '{choice}'")
            continue

        action()
        print(f"\n  Run ft_process_file.py to see the effect,")
        print(f"  or wait for next poll if already running.")
        print(f"  Files written to: {LOG_DIR}")


if __name__ == "__main__":
    main()
