"""
config_loader.py  —  Main PC (SHARED)
Single source of truth for config.json.
All other files import get_config() instead of hardcoding values.
"""

import os
import json

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

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
    """
    Load config.json and return as dict.
    Any missing key falls back to _DEFAULTS — never crashes.
    """
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for section, values in _DEFAULTS.items():
                if section not in data:
                    data[section] = values
                else:
                    for k, v in values.items():
                        data[section].setdefault(k, v)
            return data
    except Exception as e:
        print(f"[config_loader] Failed to load config.json: {e} — using defaults")
    return _DEFAULTS
