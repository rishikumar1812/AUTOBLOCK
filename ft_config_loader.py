"""
ft_config_loader.py  —  FT PC
Single source of truth for ft_config.json.
EXE-safe: reads from beside the EXE when frozen.

ft_number: 1-4 (same range for both front and rear)
ft_side:   "front" or "rear"
Function mapping is direct — ft_number == function number.
"""

import os
import sys
import json

_DEFAULTS = {
    "ft": {
        "ft_number": 1,
        "ft_side":   "front",
    },
    "network": {
        "main_pc_ip":       "192.168.0.21",
        "main_pc_port":     8998,
        "ping_timeout_sec": 1,
    },
    "paths": {
        "log_dir":     "C:\\FT\\logs",
        "log_reg_dir": "C:\\FT\\log_register",
    },
    "monitor": {
        "poll_interval_sec": 30,
        "max_record_window": 60,
    },
    "thresholds": {
        "warn_at_fail":  2,
        "block_at_fails": 4,
    },
    "dashboard": {
        "refresh_interval_ms": 10000,
        "heartbeat_sec":       1800,
    },
}


def _config_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "ft_config.json")


def get_config() -> dict:
    path = _config_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for section, values in _DEFAULTS.items():
                if section not in data:
                    data[section] = dict(values)
                elif isinstance(values, dict):
                    for k, v in values.items():
                        data[section].setdefault(k, v)
            return data
    except Exception as e:
        print(f"[ft_config_loader] Failed to load {path}: {e} — using defaults")
    return {k: dict(v) for k, v in _DEFAULTS.items()}


def save_config(cfg: dict) -> bool:
    path = _config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        return True
    except Exception as e:
        print(f"[ft_config_loader] Failed to save: {e}")
        return False


# ── Convenience accessors ──────────────────────────────────
def ft_number()    -> int: return int(get_config()["ft"]["ft_number"])
def ft_side()      -> str: return get_config()["ft"]["ft_side"].lower()
def main_pc_ip()   -> str: return get_config()["network"]["main_pc_ip"]
def main_pc_port() -> int: return int(get_config()["network"]["main_pc_port"])
def log_dir()      -> str: return get_config()["paths"]["log_dir"]
def log_reg_dir()  -> str: return get_config()["paths"]["log_reg_dir"]
def poll_interval()-> int: return int(get_config()["monitor"]["poll_interval_sec"])
def max_records()  -> int: return int(get_config()["monitor"]["max_record_window"])
def warn_at_fail() -> int: return int(get_config()["thresholds"]["warn_at_fail"])
def block_at_fail()-> int: return int(get_config()["thresholds"]["block_at_fails"])
def refresh_ms()   -> int: return int(get_config()["dashboard"]["refresh_interval_ms"])
def heartbeat_sec()-> int: return int(get_config()["dashboard"]["heartbeat_sec"])


def ft_label() -> str:
    """
    Human-readable label: 'FT1' or 'FT3' — exactly what's in config.
    Used in dashboard title, card header, log messages, network payload.
    """
    return f"FT{ft_number()}"


def ft_function_name() -> str:
    """
    The Function label this FT maps to on the InLine_Pro HMI screen.
    ft_number is 1-4 and maps DIRECTLY to Function 1-4.
    ft_side decides Front Rack or Rear Rack.

    Examples:
        ft_number=1, ft_side="front" → "Function 1" (Front Rack)
        ft_number=3, ft_side="rear"  → "Function 3" (Rear Rack)
        ft_number=4, ft_side="front" → "Function 4" (Front Rack)
    """
    return f"Function {ft_number()}"
