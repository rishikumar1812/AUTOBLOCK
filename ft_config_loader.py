"""
ft_config_loader.py  —  FT PC
Single source of truth for ft config.json.
EXE-safe: reads from beside the EXE when frozen.
"""

import os
import sys
import json

_DEFAULTS = {
    "ft": {
        "ft_number":  1,
        "ft_side":    "front",
        "setup_type": 8,
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
def setup_type()   -> int: return int(get_config()["ft"]["setup_type"])
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
    Human-readable label for this FT, e.g. 'FT1 Front'.
    Used in dashboard title, log messages, and network payload.
    """
    return f"FT{ft_number()} {ft_side().capitalize()}"


def ft_function_name() -> str:
    """
    The Function label this FT maps to on the InLine_Pro HMI screen.

    Mapping (confirmed from HMI screenshot):
      Front Rack: FT1=Function1, FT2=Function2, FT3=Function3, FT4=Function4
      Rear Rack:  FT5=Function1, FT6=Function2, FT7=Function3, FT8=Function4
                  (for 8-setup)
      6-setup:
      Front Rack: FT1=Function1, FT2=Function2, FT3=Function3
      Rear Rack:  FT4=Function1, FT5=Function2, FT6=Function3

    Returns e.g. 'Function 1' matching the exact HMI label text.
    """
    num  = ft_number()
    side = ft_side()
    stype = setup_type()

    if stype == 8:
        # Front: FT1-FT4, Rear: FT5-FT8
        fn = num if side == "front" else num - 4
    else:
        # 6-setup — Front: FT1-FT3, Rear: FT4-FT6
        fn = num if side == "front" else num - 3

    return f"Function {fn}"