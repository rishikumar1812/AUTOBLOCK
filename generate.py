"""
create_sample_ft_csv.py  —  FT PC test utility
Creates minimal sample CSV files that ft_process_file.py can read.

The real FT CSV has 660+ lines of test data per record.
This script creates the same structure but only the fields
that ft_process_file.py actually reads:
  - #INIT      (section start)
  - DATE :     2026/07/08
  - TIME :     14:32:07
  - JIG :      1
  - RESULT :   PASS or FAIL

Everything else in the real file is ignored by the reader.

Usage:
    python create_sample_ft_csv.py            # interactive
    python create_sample_ft_csv.py --pass 8  # 8 PASS records
    python create_sample_ft_csv.py --fail 5  # 5 FAIL records
    python create_sample_ft_csv.py --pass 6 --fail 4  # mixed
    python create_sample_ft_csv.py --clean   # delete samples
"""

import os
import sys
import argparse
from datetime import datetime, timedelta

try:
    from ft_config_loader import log_dir
    OUT_DIR = log_dir()
except Exception:
    OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_logs")

# Station files to generate (matches real naming: _01 to _06)
STATIONS   = [1, 2, 3, 4, 5, 6]
MODEL      = "SM-S938BE"
TESTCODE   = "TOP20_SAMPLE"


def _write_record(f, ts: datetime, jig: int, result: str) -> None:
    """Write one minimal #INIT record matching real FT CSV format."""
    f.write("\r\n")
    f.write("#INIT\r\n")
    f.write(f"MODEL : {MODEL}\r\n")
    f.write(f"DATE : {ts.strftime('%Y/%m/%d')}\r\n")
    f.write(f"TIME : {ts.strftime('%H:%M:%S')}\r\n")
    f.write(f"TESTCODE : {TESTCODE}\r\n")
    f.write(f"JIG : {jig}\r\n")
    # Minimal test section — just enough to look like real file
    f.write("\r\n")
    f.write("#TEST\r\n")
    f.write("/*==== Sample test data ====*/\r\n")
    f.write(f"SAMPLE_TEST, OK, OK, OK, P, 30\r\n")
    f.write("\r\n")
    f.write(f"RESULT : {result}\r\n")
    f.write(f"TEST-TIME : 30\r\n")


def create_csv(station: int, pass_count: int, fail_count: int,
               start_time: datetime = None) -> str:
    """
    Create a sample CSV for one station.
    Records are spaced 2 minutes apart so timestamps look realistic.
    Returns the filepath written.
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    today    = datetime.now().strftime("%Y%m%d")
    filename = f"{MODEL}_SMD_FT_TOP20_{today}_{station:02d}.csv"
    filepath = os.path.join(OUT_DIR, filename)

    now = start_time or datetime.now()
    total   = pass_count + fail_count
    results = (["FAIL"] * fail_count) + (["PASS"] * pass_count)

    # Shuffle fails into the middle to be realistic
    import random
    random.shuffle(results)

    with open(filepath, "w", encoding="utf-8", newline="") as f:
        for i, result in enumerate(results):
            # Space records 2 minutes apart, going backwards from now
            ts = now - timedelta(minutes=(total - i) * 2)
            _write_record(f, ts, station, result)

    print(f"  Created: {filename}  "
          f"({total} records: {pass_count} PASS, {fail_count} FAIL)")
    return filepath


def clean_samples() -> None:
    """Delete all generated sample CSV files."""
    if not os.path.isdir(OUT_DIR):
        print(f"  Dir not found: {OUT_DIR}")
        return
    deleted = 0
    today   = datetime.now().strftime("%Y%m%d")
    for fn in os.listdir(OUT_DIR):
        if fn.endswith(".csv") and today in fn:
            os.remove(os.path.join(OUT_DIR, fn))
            print(f"  Deleted: {fn}")
            deleted += 1
    print(f"  {deleted} file(s) removed.")


def verify_readable() -> None:
    """Quick check that generated files are readable by ft_process_file."""
    try:
        from ft_process_file import read_csv_file
    except ImportError:
        print("  [skip] ft_process_file not importable — skipping verify")
        return

    today = datetime.now().strftime("%Y%m%d")
    ok    = 0
    for fn in os.listdir(OUT_DIR):
        if fn.endswith(".csv") and today in fn:
            df = read_csv_file(os.path.join(OUT_DIR, fn))
            if df is not None:
                fails = df[df["RESULT"].str.upper() == "FAIL"].shape[0]
                print(f"  ✓ {fn}: {len(df)} records  FAIL={fails}")
                ok += 1
            else:
                print(f"  ✗ {fn}: read_csv_file returned None")
    if ok:
        print(f"\n  {ok} file(s) readable by ft_process_file ✓")


def interactive_menu() -> None:
    print("""
╔══════════════════════════════════════════════╗
║       FT Sample CSV Generator                ║
╠══════════════════════════════════════════════╣
║  Output dir: {dir:<28} ║
╠══════════════════════════════════════════════╣
║  1.  All PASS  (RUNNING state)               ║
║  2.  Some FAIL — below warn (RUNNING)        ║
║  3.  At warn threshold (WARNING)             ║
║  4.  At block threshold (BLOCKED → STOP)     ║
║  5.  Custom pass/fail count                  ║
║  6.  Clean today's sample files              ║
║  7.  Verify files readable by ft_process     ║
║  0.  Exit                                    ║
╚══════════════════════════════════════════════╝
""".format(dir=OUT_DIR[:28]))

    try:
        from ft_config_loader import warn_at_fail, block_at_fail
        warn  = warn_at_fail()
        block = block_at_fail()
    except Exception:
        warn, block = 2, 4

    scenarios = {
        "1": (10, 0,     "RUNNING"),
        "2": (10, warn-1,"RUNNING"),
        "3": (10, warn,  "WARNING"),
        "4": (10, block, "BLOCKED → triggers STOP"),
    }

    while True:
        choice = input("Choice [0-7]: ").strip()

        if choice == "0":
            break

        elif choice in scenarios:
            total, fails, label = scenarios[choice]
            passes = total - fails
            print(f"\n  [{label}] {passes} PASS + {fails} FAIL per station")
            for s in STATIONS:
                create_csv(s, passes, fails)

        elif choice == "5":
            try:
                p = int(input("  PASS count per station [8]: ").strip() or "8")
                f = int(input("  FAIL count per station [2]: ").strip() or "2")
            except ValueError:
                p, f = 8, 2
            print(f"\n  Custom: {p} PASS + {f} FAIL per station")
            for s in STATIONS:
                create_csv(s, p, f)

        elif choice == "6":
            print("\n  Cleaning sample files...")
            clean_samples()

        elif choice == "7":
            print("\n  Verifying...")
            verify_readable()

        else:
            print("  Invalid choice")
            continue

        print(f"\n  Done. Files in: {OUT_DIR}")
        print("  Run ft_dashboard.py or ft_process_file.py to test.\n")


def main():
    parser = argparse.ArgumentParser(
        description="Create minimal FT sample CSV files")
    parser.add_argument("--pass",  type=int, default=None,
                        dest="passes", metavar="N",
                        help="Number of PASS records per station")
    parser.add_argument("--fail",  type=int, default=None,
                        dest="fails",  metavar="N",
                        help="Number of FAIL records per station")
    parser.add_argument("--clean", action="store_true",
                        help="Delete today's sample files")
    parser.add_argument("--verify", action="store_true",
                        help="Verify files readable by ft_process_file")
    args = parser.parse_args()

    if args.clean:
        clean_samples()
        return
    if args.verify:
        verify_readable()
        return
    if args.passes is not None or args.fails is not None:
        p = args.passes or 8
        f = args.fails  or 0
        print(f"Creating: {p} PASS + {f} FAIL per station → {OUT_DIR}")
        for s in STATIONS:
            create_csv(s, p, f)
        verify_readable()
        return

    interactive_menu()


if __name__ == "__main__":
    main()
