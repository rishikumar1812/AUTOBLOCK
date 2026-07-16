"""
main_pc_popup.py  —  Main PC  (ENTRY POINT)

TCP commands handled:
  {"command": "HELLO"}  — handshake from DL PC, replies ACK,
                           tray shows Connected + timestamp
  {"command": "STOP",  "dl": "DL03"}  — triggers automation

Connection bar in tray:
  Checking... → Connected (green) / Disconnected (red)
  Updates every time a HELLO arrives or connection drops.
"""

import os
import sys
import json
import time
import queue
import socket
import logging
import threading
import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime

from config_loader import get_config
from ini_editor import uncheck_dl, uncheck_ft
from inline_automation import run_stop_sequence
from log_cleanup import cleanup_old_logs, DailyFileHandler


# =========================================================
# Config accessors
# =========================================================
def _listen_host()    -> str: return get_config()["listener"]["host"]
def _listen_port()    -> int: return int(get_config()["listener"]["port"])
def _ft_listen_port() -> int: return int(get_config().get("ft_listener", {}).get("port", 8998))
def _log_dir()        -> str: return get_config()["paths"]["log_dir"]
def _exe_name()       -> str: return get_config()["app"]["exe_name"]


# =========================================================
# Logging
# =========================================================
def _setup_logger() -> logging.Logger:
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)

    # Delete log files older than 7 days — runs once at startup
    cleanup_old_logs(log_dir, retention_days=7)

    logger = logging.getLogger("main_pc_popup")
    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # DailyFileHandler rolls to a new file at midnight automatically.
        # This tray app runs 24/7 in the background (root.mainloop()),
        # so the old fixed-date filename never updated itself — this
        # handler checks the date on every emit() call instead.
        fh = DailyFileHandler("main_pc_popup", log_dir, retention_days=7)
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    return logger

# Logger initialised at module level so imported modules can use it
# If log dir doesn't exist yet, fall back to console-only logging
try:
    logger = _setup_logger()
except Exception as _log_err:
    logger = logging.getLogger("main_pc_popup")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    logger.warning(f"[main] Log file setup failed: {_log_err} — using console only")


# =========================================================
# Shared state
# =========================================================
_task_queue  : queue.Queue = queue.Queue()
_popup_queue : list        = []
_state_lock  = threading.Lock()
_queue_lock  = threading.Lock()

# DL state tracking — 3 possible states per DL:
#   "processing" — signal received, automation running
#   "stopped"    — automation confirmed success
#   "error"      — automation failed, manual intervention needed
#
# Structure: {dl_name: {"state": str, "ts": str}}
_dl_states : dict = {}

# Connection state — updated when HELLO arrives
_conn_state = {
    "connected":    False,
    "last_hello":   None,   # datetime of last HELLO
    "dl_pc_addr":   None,   # IP of DL PC
}
_conn_lock = threading.Lock()

# FT connection state — one entry per FT PC number (1-8)
# {ft_number: {"connected": bool, "last_hello": datetime, "addr": str, "ft_side": str}}
_ft_conn_states : dict = {}
_ft_conn_lock = threading.Lock()


# =========================================================
# Colors
# =========================================================
BG_MAIN    = "#0d1117"
BG_CARD    = "#161b22"
BG_HEADER  = "#1c2128"
COL_BLOCK  = "#f85149"
COL_OK     = "#3fb950"
COL_DISC   = "#f85149"   # disconnected — same red as BLOCK
COL_WARN   = "#d29922"
COL_CHECK  = "#58a6ff"
COL_TEXT   = "#c9d1d9"
COL_MUTED  = "#6e7681"
COL_WHITE  = "#ffffff"
COL_BORDER = "#30363d"


# =========================================================
# Sequential task queue worker
# =========================================================
def _task_display_name(task_key: str) -> str:
    """
    Human-readable name for tray/toast display.
    DL task:  "DL06"                    → "DL06"
    FT task:  "FT_F1_front_Function 1"  → "F1 (Front — Function 1)"
              "FT_R3_rear_Function 3"   → "R3 (Rear — Function 3)"
    """
    if task_key.upper().startswith("FT_"):
        try:
            # "FT_F1_front_Function 1" → parts[1]="F1"
            parts    = task_key.split("_", 3)
            ft_id    = parts[1]                      # "F1"
            rack     = parts[2].capitalize()         # "Front"
            func     = parts[3] if len(parts) > 3 else ""
            return f"{ft_id} ({rack} — {func})"
        except Exception:
            return task_key
    return task_key


def _queue_worker() -> None:
    logger.info("[queue] Worker started")
    while True:
        dl_name = _task_queue.get()
        if dl_name is None:
            break
        display = _task_display_name(dl_name)
        try:
            logger.info(f"[queue] Processing stop for {display}")
            success = run_stop_sequence(dl_name)

            ts = datetime.now().strftime("%H:%M:%S")
            if success:
                with _state_lock:
                    _dl_states[dl_name] = {"state": "stopped", "ts": ts}
                with _queue_lock:
                    _popup_queue.append({
                        "type":    "STOP",
                        "dl_name": display,
                        "ts":      ts,
                    })
                logger.info(f"[queue] {display} — stopped OK at {ts}")
            else:
                with _state_lock:
                    _dl_states[dl_name] = {"state": "error", "ts": ts}
                with _queue_lock:
                    _popup_queue.append({
                        "type":    "ERROR",
                        "dl_name": display,
                        "ts":      ts,
                    })
                logger.error(
                    f"[queue] {display} — automation FAILED at {ts}. "
                    f"Manual intervention required."
                )
        except Exception as e:
            logger.error(f"[queue] {display} — unexpected error: {e}")
            ts = datetime.now().strftime("%H:%M:%S")
            with _state_lock:
                _dl_states[dl_name] = {"state": "error", "ts": ts}
            with _queue_lock:
                _popup_queue.append({
                    "type":    "ERROR",
                    "dl_name": display,
                    "ts":      ts,
                })
        finally:
            _task_queue.task_done()


# =========================================================
# Send response
# =========================================================
def _send_response(conn: socket.socket, status: str, message: str = "") -> None:
    try:
        conn.sendall(
            json.dumps({"status": status, "message": message}).encode("utf-8")
        )
    except Exception as e:
        logger.error(f"[listener] Failed to send response: {e}")


# =========================================================
# Handle TCP connection
# Commands: HELLO, STOP
# =========================================================
def _handle_connection(conn: socket.socket, addr: tuple) -> None:
    try:
        raw     = conn.recv(1024)
        data    = json.loads(raw.decode("utf-8"))
        command = data.get("command", "").upper()

        logger.info(f"[listener] {addr[0]}:{addr[1]} → {command}")

        # ── HELLO handshake ───────────────────────────────
        if command == "HELLO":
            # Reply ACK immediately
            _send_response(conn, "ACK", "Connected")

            # Update connection state
            with _conn_lock:
                _conn_state["connected"]  = True
                _conn_state["last_hello"] = datetime.now()
                _conn_state["dl_pc_addr"] = addr[0]

            # Toast only on FIRST connect or reconnect, not every heartbeat
            with _conn_lock:
                was_connected = _conn_state.get("connected", False)
            if not was_connected:
                with _queue_lock:
                    _popup_queue.append({
                        "type": "HELLO",
                        "addr": addr[0],
                        "ts":   datetime.now().strftime("%H:%M:%S"),
                    })
            logger.info(f"[listener] HELLO from {addr[0]} — ACK sent, Connected")

        # ── STOP command ──────────────────────────────────
        elif command == "STOP":
            dl_name = data.get("dl", "").upper()

            if not dl_name or not dl_name.startswith("DL"):
                _send_response(conn, "ERROR", f"Invalid DL: {dl_name}")
                return

            # Duplicate check — skip if already processing
            with _state_lock:
                existing = _dl_states.get(dl_name, {}).get("state")
            if existing == "processing":
                logger.info(
                    f"[listener] {dl_name} already processing — "
                    f"duplicate signal ignored"
                )
                _send_response(conn, "OK",
                               f"Already processing: {dl_name}")
                return

            # Acknowledge before automation
            _send_response(conn, "OK", f"Stop queued for {dl_name}")

            # Mark as processing immediately so operator sees it in tray
            ts = datetime.now().strftime("%H:%M:%S")
            with _state_lock:
                _dl_states[dl_name] = {"state": "processing", "ts": ts}

            # Queue automation task
            _task_queue.put(dl_name)
            logger.info(f"[listener] {dl_name} queued for automation")

        else:
            _send_response(conn, "ERROR", f"Unknown command: {command}")

    except json.JSONDecodeError as e:
        logger.error(f"[listener] Bad JSON from {addr}: {e}")
        _send_response(conn, "ERROR", "Invalid JSON")
    except Exception as e:
        logger.error(f"[listener] Error from {addr}: {e}")
    finally:
        conn.close()


# =========================================================
# TCP listener
# =========================================================
def _start_tcp_listener() -> None:
    host = _listen_host()
    port = _listen_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(20)
        logger.info(f"[listener] Listening on {host}:{port}")
        while True:
            try:
                conn, addr = server.accept()
                threading.Thread(
                    target=_handle_connection,
                    args=(conn, addr),
                    daemon=True,
                ).start()
            except Exception as e:
                logger.error(f"[listener] Accept error: {e}")
                time.sleep(1)


# =========================================================
# FT mapping: ft_number + ft_side + setup_type → Function label
# Matches InLine_Pro HMI screen labels exactly.
# 8-setup: Front=FT1-4, Rear=FT5-8
# 6-setup: Front=FT1-3, Rear=FT4-6
# =========================================================
# ft_id → (rack, function_label)
_FT_ID_MAP = {
    "F1": ("front", "Function 1"),
    "F2": ("front", "Function 2"),
    "F3": ("front", "Function 3"),
    "F4": ("front", "Function 4"),
    "R1": ("rear",  "Function 1"),
    "R2": ("rear",  "Function 2"),
    "R3": ("rear",  "Function 3"),
    "R4": ("rear",  "Function 4"),
}


def _ft_id_to_rack_function(ft_id: str) -> tuple:
    """
    Map ft_id to (rack, function_label).
    e.g. 'F1' → ('front', 'Function 1')
         'R3' → ('rear',  'Function 3')
    """
    return _FT_ID_MAP.get(ft_id.upper(), ("front", "Function 1"))


def _handle_ft_connection(conn: socket.socket, addr: tuple) -> None:
    """Handle a single connection from an FT PC on port 8998."""
    try:
        raw     = conn.recv(1024)
        data    = json.loads(raw.decode("utf-8"))
        command = data.get("command", "").upper()
        ft_id   = data.get("ft_id", "").upper()

        logger.info(f"[ft_listener] {addr[0]}:{addr[1]} → {command} {ft_id}")

        if command == "HELLO":
            _send_response(conn, "ACK", "Connected")
            # Key is ft_id string e.g. "F1", "R3"
            with _ft_conn_lock:
                _ft_conn_states[ft_id] = {
                    "connected":  True,
                    "last_hello": datetime.now(),
                    "addr":       addr[0],
                }
            logger.info(
                f"[ft_listener] HELLO from {ft_id} at {addr[0]} — ACK sent")

        elif command == "STOP":
            if not ft_id or ft_id not in _FT_ID_MAP:
                _send_response(conn, "ERROR",
                               f"Invalid ft_id: {ft_id!r}")
                return

            rack, function_label = _ft_id_to_rack_function(ft_id)
            task_key = f"FT_{ft_id}_{rack}_{function_label}"

            # Duplicate check
            with _state_lock:
                existing = _dl_states.get(task_key, {}).get("state")
            if existing == "processing":
                logger.info(
                    f"[ft_listener] {task_key} already processing — "
                    f"duplicate ignored")
                _send_response(conn, "OK",
                               f"Already processing: {task_key}")
                return

            _send_response(conn, "OK", f"FT stop queued: {task_key}")
            ts = datetime.now().strftime("%H:%M:%S")
            with _state_lock:
                _dl_states[task_key] = {"state": "processing", "ts": ts}
            _task_queue.put(task_key)
            logger.info(f"[ft_listener] {task_key} queued")

        else:
            _send_response(conn, "ERROR", f"Unknown command: {command}")

    except json.JSONDecodeError as e:
        logger.error(f"[ft_listener] Bad JSON from {addr}: {e}")
        _send_response(conn, "ERROR", "Invalid JSON")
    except Exception as e:
        logger.error(f"[ft_listener] Error from {addr}: {e}")
    finally:
        conn.close()


def _start_ft_tcp_listener() -> None:
    """Second TCP listener on port 8998 — dedicated to FT PCs."""
    host = _listen_host()
    port = _ft_listen_port()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(20)
            logger.info(f"[ft_listener] FT listener on {host}:{port}")
            while True:
                try:
                    conn, addr = server.accept()
                    threading.Thread(
                        target=_handle_ft_connection,
                        args=(conn, addr),
                        daemon=True,
                    ).start()
                except Exception as e:
                    logger.error(f"[ft_listener] Accept error: {e}")
                    time.sleep(1)
    except Exception as e:
        logger.error(f"[ft_listener] Failed to start on port {port}: {e}")


# =========================================================
# Toast popup
# =========================================================
def show_toast(root: tk.Tk, item: dict) -> None:
    toast = tk.Toplevel(root)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    toast.attributes("-alpha", 0.96)
    toast.configure(bg=BG_CARD)

    kind      = item["type"]
    dl_name   = item.get("dl_name", "")
    is_ft     = dl_name.upper().startswith("FT")
    if kind == "STOP":
        bar_color = COL_BLOCK
        icon      = "⛔"
        title     = "FT STOPPED" if is_ft else "DL STOPPED"
        body_text = f"{dl_name}  automation completed"
    elif kind == "ERROR":
        bar_color = COL_WARN
        icon      = "⚠"
        title     = "AUTOMATION FAILED"
        body_text = f"{dl_name}  manual intervention required"
    else:   # HELLO
        bar_color = COL_OK
        icon      = "🔗"
        title     = "DL PC CONNECTED"
        body_text = f"DL PC  {item.get('addr', '')}  is connected"

    sw   = root.winfo_screenwidth()
    w, h = 340, 115
    toast.geometry(f"{w}x{h}+{sw - w - 24}+60")

    tk.Frame(toast, bg=bar_color, width=6).pack(side=tk.LEFT, fill=tk.Y)

    body = tk.Frame(toast, bg=BG_CARD, padx=14, pady=12)
    body.pack(fill=tk.BOTH, expand=True)

    f_big = tkfont.Font(family="Consolas", size=12, weight="bold")
    f_med = tkfont.Font(family="Consolas", size=10)
    f_sml = tkfont.Font(family="Consolas", size=8)

    tk.Label(body, text=f"{icon}  {title}",
             font=f_big, bg=BG_CARD, fg=bar_color).pack(anchor="w")
    tk.Label(body, text=body_text,
             font=f_med, bg=BG_CARD, fg=COL_TEXT).pack(anchor="w", pady=(3, 0))
    tk.Label(body,
             text=f"{item['ts']}   •   click to dismiss",
             font=f_sml, bg=BG_CARD, fg=COL_MUTED).pack(anchor="w", pady=(4, 0))

    def dismiss(e=None):
        try:
            toast.destroy()
        except tk.TclError:
            pass

    def fade_out(alpha: float = 0.96) -> None:
        alpha -= 0.05
        if alpha <= 0:
            dismiss()
            return
        try:
            toast.attributes("-alpha", alpha)
            toast.after(120, lambda: fade_out(alpha))
        except tk.TclError:
            pass

    toast.bind("<Button-1>", dismiss)
    for w in toast.winfo_children():
        w.bind("<Button-1>", dismiss)
        for ww in w.winfo_children():
            ww.bind("<Button-1>", dismiss)

    toast.after(4500, fade_out)


# =========================================================
# Tray window
# =========================================================
class TrayWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DL & FT Monitor — Main PC")
        self.root.configure(bg=BG_MAIN)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        # Position bottom-right corner with safe margin from edges
        # Use update_idletasks first so winfo values are accurate
        w = 300
        h = 480
        x = 8                    # bottom-left, 8px from left edge
        y = sh - h - 48          # 48px above taskbar
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.f_title = tkfont.Font(family="Consolas", size=10, weight="bold")
        self.f_conn  = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_body  = tkfont.Font(family="Consolas", size=9)
        self.f_small = tkfont.Font(family="Consolas", size=8)

        self._dot_count   = 0
        self._dot_anim_id = None

        self._build()
        self._poll_popup_queue()
        self._refresh()

    def _build(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=BG_HEADER, pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="DL & FT Monitor  •  Main PC",
                 font=self.f_title, bg=BG_HEADER,
                 fg=COL_WHITE).pack(padx=10)

        # ── Connection bar ────────────────────────────────
        conn_bar = tk.Frame(self.root, bg=BG_MAIN, pady=6)
        conn_bar.pack(fill=tk.X, padx=10)

        conn_row = tk.Frame(conn_bar, bg=BG_MAIN)
        conn_row.pack(fill=tk.X)

        self.lbl_conn_dot = tk.Label(
            conn_row, text="●", font=self.f_conn,
        