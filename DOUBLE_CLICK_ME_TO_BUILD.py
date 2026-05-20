"""
DOUBLE_CLICK_ME_TO_BUILD.py
============================
Just double-click this file in Windows Explorer.
Python will open and build both EXE files automatically.

Requirements:
  - Python must be installed on this PC
  - Download Python from: https://www.python.org/downloads/
    (tick "Add Python to PATH" during install)
"""

import subprocess
import sys
import os

# Open a visible console window and run the build
os.chdir(os.path.dirname(os.path.abspath(__file__)))
subprocess.run([sys.executable, "build_all.py"], shell=False)
