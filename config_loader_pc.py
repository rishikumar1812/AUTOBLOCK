"""
config_loader.py  —  Main PC (SHARED)
Single source of truth for config.json.

EXE-safe: uses sys.executable when frozen so config.json is always
read/written from beside the EXE, not the temp _MEIPASS folder.
Changes persist across restarts.
"""

import os
import sys
import json


def _config_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "config.json")


_DEFAULTS = {
    "paths": {
        "data_ini": r"C:\InLine_Pro\Data.ini",
        "log_dir":  r"C:\MainPC\logs",
    },
    "app": {
        "exe_name":     "InLine_Pro.exe",
        "window_title": "InLine_Pro_Version 23",
    },
    "automation": {
        "step_wait_sec":  3,
        "max_wait_sec":   30,
        "retry_attempts": 3,
    },
    "listener": {
        "host": "0.0.0.0",
        "port": 9999,
    },
}


def get_config() -> dict:
    path = _config_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
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
    path = _config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        print(f"[config_loader] Saved → {path}")
        return True
    except Exception as e:
        print(f"[config_loader] Failed to save {path}: {e}")
        return False
