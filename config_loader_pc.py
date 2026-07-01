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
        "data_ini": "C:\\InLine_Pro\\Data.ini",
        "log_dir": "C:\\MainPC\\logs",
    },
    "app": {
        "exe_name": "InLine_Pro.exe",
        "window_title": "InLine_Pro_",
    },
    "automation": {
        "step_wait_sec": 1,
        "max_wait_sec": 30,
        "retry_attempts": 3,
    },
    "listener": {
        "host": "0.0.0.0",
        "port": 8999,
    },
    "dashboard": {
        "hello_toast_minutes": 30,
    },
    "building_check": {
        # Whether the wait-for-"Wait" gate runs at all before STOP
        # automation. Set false to fall back to old behavior
        # (uncheck immediately, no occupancy check) in an emergency.
        "enabled": True,

        # x-position (left, in screen px) of the Building-N LABEL
        # text controls, confirmed via inspect_inline.py / diagnostic
        # runs against InLine_Pro_Ver 3.1.8.01.
        "front_label_left": 143,
        "rear_label_left": 436,

        # x-position of the paired status VALUE Edit control,
        # read via .get_value() (NOT window_text() — confirmed
        # these Edit controls only expose text via get_value()).
        "front_value_left": 200,
        "rear_value_left": 496,

        # How close two controls' rectangle().top must be (in px)
        # to be considered "same row" / paired together.
        "row_top_tolerance_px": 5,

        # The exact text (after stripping internal spaces, since
        # the app renders e.g. "W a i t" with letter-spacing) that
        # means "building is empty, safe to uncheck/stop".
        "ready_text": "Wait",

        # How often to re-read the value while waiting (seconds).
        "poll_interval_sec": 5,

        # Give up after this many seconds and alert the operator
        # instead of proceeding, if the building never clears.
        "max_wait_sec": 600,
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
                elif isinstance(values, dict):
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
