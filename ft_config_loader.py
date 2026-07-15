"""
ft_config_loader.py  —  FT PC
Single source of truth for ft_config.json.
EXE-safe: reads from beside the EXE when frozen.

ft_id values and their mapping:
  F1 → Front Rack, Function 1
  F2 → Front Rack, Function 2
  F3 → Front Rack, Function 3
  F4 → Front Rack, Function 4
  R1 → Rear Rack,  Function 1
  R2 → Rear Rack,  Function 2
  R3 → Rear Rack,  Function 3
  R4 → Rear Rack,  Function 4
"""

import os
import sys
import json

_DEFAULTS = {
    "ft": {
        "ft_id": "F1",
    },
    "network": {
        "main_pc_ip":   "192.168.0.21",
        "main_pc_port": 8998,
        "timeout_sec":  10,
    },
    "paths": {
        "log_dir":     "C:\\FT\\logs",
        "log_reg_dir": "C:\\FT\\log_register",
    },
    "monitor": {
        "poll_interval_sec": 30,
        "record_window":     60,
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

# ── ft_id → (rack, function_num, function_label) ──────────
_FT_MAP = {
    "F1": ("front", 1, "Function 1"),
    "F2": ("front", 2, "Function 2"),
    "F3": ("front", 3, "Function 3"),
    "F4": ("front", 4, "Function 4"),
    "R1": ("rear",  1, "Function 1"),
    "R2": ("rear",  2, "Function 2"),
    "R3": ("rear",  3, "Function 3"),
    "R4": ("rear",  4, "Function 4"),
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
def ft_id()         -> str: return get_config()["ft"]["ft_id"].upper()
def main_pc_ip()    -> str: return get_config()["network"]["main_pc_ip"]
def main_pc_port()  -> int: return int(get_config()["network"]["main_pc_port"])
def timeout_sec()   -> int: return int(get_config()["network"]["timeout_sec"])
def log_dir()       -> str: return get_config()["paths"]["log_dir"]
def log_reg_dir()   -> str: return get_config()["paths"]["log_reg_dir"]
def poll_interval() -> int: return int(get_config()["monitor"]["poll_interval_sec"])
def record_window() -> int: return int(get_config()["monitor"]["record_window"])
def warn_at_fail()  -> int: return int(get_config()["thresholds"]["warn_at_fail"])
def block_at_fail() -> int: return int(get_config()["thresholds"]["block_at_fails"])
def refresh_ms()    -> int: return int(get_config()["dashboard"]["refresh_interval_ms"])
def heartbeat_sec() -> int: return int(get_config()["dashboard"]["heartbeat_sec"])


def ft_mapping() -> tuple:
    """
    Returns (rack, function_num, function_label) for current ft_id.
    e.g. ft_id="F1" → ("front", 1, "Function 1")
         ft_id="R3" → ("rear",  3, "Function 3")
    Raises ValueError if ft_id is invalid.
    """
    fid = ft_id()
    if fid not in _FT_MAP:
        raise ValueError(
            f"Invalid ft_id '{fid}'. Must be F1-F4 or R1-R4."
        )
    return _FT_MAP[fid]


def ft_rack()          -> str: return ft_mapping()[0]
def ft_function_num()  -> int: return ft_mapping()[1]
def ft_function_label()-> str: return ft_mapping()[2]


def ft_display_label() -> str:
    """
    Human-readable label for UI and logs.
    e.g. "F1 (Front Rack — Function 1)"
    """
    fid  = ft_id()
    rack, fn, label = ft_mapping()
    rack_name = "Front Rack" if rack == "front" else "Rear Rack"
    return f"{fid} ({rack_name} — {label})"
