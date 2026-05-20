"""
build_all.py
============
Run this script on ANY PC that has Python installed.
It will:
  1. Install all required packages (pyinstaller, pandas, pywinauto)
  2. Build dl_monitor.exe        (for DL PC)
  3. Build main_pc_listener.exe  (for Main PC)

HOW TO RUN:
-----------
  1. Open a Command Prompt (Win + R -> type cmd -> Enter)
  2. Navigate to this folder:
         cd C:\path\to\this\folder
  3. Run:
         python build_all.py

OUTPUT:
-------
  dist\dl_monitor.exe          -> copy this to DL PC
  dist\main_pc_listener.exe    -> copy this to Main PC
"""

import subprocess
import sys
import os


def run(cmd, description):
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"\n[ERROR] Step failed: {description}")
        print("Fix the error above and re-run this script.")
        input("\nPress Enter to exit...")
        sys.exit(1)
    print(f"[OK] {description}")


# ── Step 1: Install dependencies ──────────────────────────────────────────────
run("pip install pyinstaller pandas pywinauto",
    "Installing pyinstaller, pandas, pywinauto")

# ── Paths ─────────────────────────────────────────────────────────────────────
here    = os.path.dirname(os.path.abspath(__file__))
dl_dir  = os.path.join(here, "..", "DL_PC")
main_dir = os.path.join(here, "..", "MAIN_PC")
dist_dir = os.path.join(here, "dist")

os.makedirs(dist_dir, exist_ok=True)

# ── Step 2: Build dl_monitor.exe ──────────────────────────────────────────────
run(
    f'pyinstaller '
    f'--onefile '
    f'--console '
    f'--name dl_monitor '
    f'--distpath "{dist_dir}" '
    f'--workpath "{os.path.join(here, "build_temp_dl")}" '
    f'--specpath "{os.path.join(here, "spec_temp")}" '
    f'"{os.path.join(dl_dir, "process_files.py")}" '
    f'--paths "{dl_dir}"',
    "Building dl_monitor.exe (DL PC)"
)

# ── Step 3: Build main_pc_listener.exe ────────────────────────────────────────
run(
    f'pyinstaller '
    f'--onefile '
    f'--console '
    f'--name main_pc_listener '
    f'--distpath "{dist_dir}" '
    f'--workpath "{os.path.join(here, "build_temp_main")}" '
    f'--specpath "{os.path.join(here, "spec_temp")}" '
    f'"{os.path.join(main_dir, "main_pc_listener.py")}"',
    "Building main_pc_listener.exe (Main PC)"
)

# ── Done ──────────────────────────────────────────────────────────────────────
print(f"""

{'='*60}
  BUILD COMPLETE
{'='*60}

  Output folder: {dist_dir}

  Files created:
    dl_monitor.exe         -> Copy to DL PC,   double-click to run
    main_pc_listener.exe   -> Copy to Main PC,  double-click to run

  NEXT STEPS:
    1. Copy dl_monitor.exe        to DL PC
    2. Copy main_pc_listener.exe  to Main PC
    3. On Main PC run:  main_pc_listener.exe spy
       (to find button names inside your EXE)
    4. See README.txt for full instructions

{'='*60}
""")

input("Press Enter to exit...")
