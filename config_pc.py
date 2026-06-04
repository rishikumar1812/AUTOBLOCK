"""
config_loader.py  —  DL PC  (SHARED)
Single source of truth for config.json.
All other files import get_config() instead of hardcoding values.

EXE-safe: when running as a PyInstaller onefile EXE, __file__
points to the temp _MEIPASS extraction folder which is wiped on
every launch. We use sys.executable instead so config.json is
always read and written from the folder where the EXE actually
sits — changes persist across restarts.

Place this file in the SAME folder as all other DL_PC files.
"""

import os
import sys
import json

# =========================================================
# Resolve config.json path correctly in both modes:
#   .py script  → same folder as the script
#   .exe frozen → same folder as the EXE  (sys.executable)
#
# This is the core fix for "settings reset on reopen".
# PyInstaller sets sys.frozen = True when running as EXE.
# =========================================================
def _config_path() -> str:
    if getattr(sys, "frozen", False):
        # Running as PyInstaller EXE
        # sys.executable = C:\DL_PC\dist\dl_dashboard.exe
        base = os.path.dirname(sys.executable)
    else:
        # Running as .py script
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


_DEFAULTS = {
    "paths": {
        "log_directory":    r"C:\logs",
        "log_register_dir": r"C:\DL_PC\log_register",
    },
    "network": {
        "main_pc_ip":       "192.168.1.100",
        "main_pc_port":     9999,
        "ping_timeout_sec": 1,
    },
    "monitor": {
        "poll_interval_sec":  30,
        "max_records_window": 60,
        "dl_count":           20,
    },
    "thresholds": {
        "warn_at_fails":  2,
        "block_at_fails": 3,
    },
    "dashboard": {
        "refresh_interval_ms":  10000,
        "window_width":         1100,
        "window_height":        680,
        "no_data_stop_minutes": 60,
    },
}


def get_config() -> dict:
    """
    Load config.json from beside the EXE/script and return as dict.
    Any missing key falls back to _DEFAULTS — never crashes.
    Called fresh each time so runtime edits are picked up immediately
    without restarting any process.
    """
    path = _config_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Deep-merge: fill any missing keys from defaults
            for section, values in _DEFAULTS.items():
                if section not in data:
                    data[section] = values
                else:
                    for k, v in values.items():
                        data[section].setdefault(k, v)
            return data
    except Exception as e:
        print(f"[config_loader] Failed to load {path}: {e} — using defaults")
    return dict(_DEFAULTS)


def save_config(cfg: dict) -> bool:
    """
    Write updated config back to config.json beside the EXE/script.
    Returns True on success, False on failure.

    Always writes to sys.executable folder when frozen so changes
    survive EXE restarts.
    """
    path = _config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        print(f"[config_loader] Saved → {path}")
        return True
    except Exception as e:
        print(f"[config_loader] Failed to save {path}: {e}")
        return False
