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
from tray_utils import SingleInstance, TrayIconManager, hide_console
from datetime import datetime

from config_loader import get_config,cleanup_days
from ini_editor import uncheck_dl,uncheck_ft
from inline_automation import run_stop_sequence, _is_ft_task
from log_cleanup import cleanup_old_logs, DailyFileHandler


# =========================================================
# Config accessors
# =========================================================
def _listen_host()    -> str: return get_config()["listener"]["host"]
def _listen_port()    -> int: return int(get_config()["listener"]["port"])
def _ft_listen_port() -> int: return int(get_config().get("ft_listener", {}).get("port", 8998))
def _log_dir()        -> str: return get_config()["paths"]["log_dir"]
def _exe_name()       -> str: return get_config()["app"]["exe_name"]
def _hello_timeout_minutes() -> int:
    return int(get_config()["dashboard"].get("hello_timeout_minutes", 16))


# =========================================================
# Logging
# =========================================================
def _setup_logger() -> logging.Logger:
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)

    # Delete log files older than 7 days — runs once at startup
    cleanup_old_logs(log_dir, retention_days=cleanup_days())

    logger = logging.getLogger("main_pc_popup")
    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # DailyFileHandler rolls to a new file at midnight automatically.
        # This tray app runs 24/7 in the background (root.mainloop()),
        # so the old fixed-date filename never updated itself — this
        # handler checks the date on every emit() call instead.
        fh = DailyFileHandler("main_pc_popup", log_dir, retention_days=cleanup_days())
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    return logger

try:
    logger=_setup_logger()
except Exception as _log_err:
    logger=logging.getLogger("main_pc_popup")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        sh=logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s[%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    logger.warning(f"[main] Log file setup failed: {_log_err} - using console only")


# =========================================================
# Connection-status logger — HELLO / connect / disconnect events
# only. Kept separate from the general main_pc_popup log so an
# operator can see connectivity history at a glance.
# File: connection_status_YYYY-MM-DD.log
# =========================================================
def _setup_connection_logger() -> logging.Logger:
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)

    lg = logging.getLogger("connection_status")
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        fh = DailyFileHandler("connection_status", log_dir, retention_days=cleanup_days())
        lg.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(sh)
    return lg


try:
    conn_logger = _setup_connection_logger()
except Exception as _log_err:
    conn_logger = logging.getLogger("connection_status")
    if not conn_logger.handlers:
        conn_logger.setLevel(logging.INFO)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s[%(levelname)s] %(message)s"))
        conn_logger.addHandler(sh)
    logger.warning(f"[main] connection_status log setup failed: {_log_err} - using console only")


# =========================================================
# Process logger — automation steps only (STOP received, queued,
# processing, stopped OK, failed / manual intervention needed).
# Kept separate from the general main_pc_popup log.
# File: Process_YYYY-MM-DD.log
# =========================================================
def _setup_process_logger() -> logging.Logger:
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)

    lg = logging.getLogger("Process")
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        fh = DailyFileHandler("Process", log_dir, retention_days=cleanup_days())
        lg.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(sh)
    return lg


try:
    process_logger = _setup_process_logger()
except Exception as _log_err:
    process_logger = logging.getLogger("Process")
    if not process_logger.handlers:
        process_logger.setLevel(logging.INFO)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s[%(levelname)s] %(message)s"))
        process_logger.addHandler(sh)
    logger.warning(f"[main] Process log setup failed: {_log_err} - using console only")

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


def _task_display_name(task_key:str)->str:
    if task_key.upper().startswith("FT_"):
        try:
            parts=task_key.split("_",3)
            ft_id=parts[1]
            rack=parts[2].capitalize()
            func=parts[3] if len(parts)>3 else ""
            return f"FT_{ft_id}_{rack}_{function_label}"
        except Exception:
            return task_key
    return task_key
# =========================================================
# Sequential task queue worker
# =========================================================
def _queue_worker() -> None:
    process_logger.info("[queue] Worker started")
    while True:
        dl_name = _task_queue.get()
        if dl_name is None:
            break
        display=_task_display_name(dl_name)
        try:
            process_logger.info(f"[queue] Processing stop for {dl_name}")
            success = run_stop_sequence(dl_name)

            now = datetime.now()
            ts = now.strftime("%d-%m-%Y %H:%M:%S")
            if success:
                # Automation confirmed — mark as stopped
                with _state_lock:
                    _dl_states[dl_name] = {"state": "stopped", "ts": ts, "ts_dt": now}

                # Queue toast
                with _queue_lock:
                    _popup_queue.append({
                        "type":    "STOP",
                        "dl_name": display,
                        "source":  "FT" if _is_ft_task(dl_name) else "DL",
                        "ts":      ts,
                        "ts_dt":   now,
                    })

                process_logger.info(f"[queue] {display} — stopped OK at {ts}")
            else:
                # Automation failed — mark as error, operator must act
                with _state_lock:
                    _dl_states[dl_name] = {"state": "error", "ts": ts, "ts_dt": now}

                # Queue error toast
                with _queue_lock:
                    _popup_queue.append({
                        "type":    "ERROR",
                        "dl_name": display,
                        "source":  "FT" if _is_ft_task(dl_name) else "DL",
                        "ts":      ts,
                        "ts_dt":   now,
                    })

                process_logger.error(
                    f"[queue] {display} — automation FAILED at {ts}. "
                    f"Manual intervention required."
                )
        except Exception as e:
            process_logger.error(f"[queue] {display} — unexpected error: {e}")
            now = datetime.now()
            ts = now.strftime("%d-%m-%Y %H:%M:%S")
            with _state_lock:
                _dl_states[dl_name]={"state":"error","ts":ts,"ts_dt":now}
            with _queue_lock:
                _popup_queue.append({
                    "type": "ERROR",
                    "dl_name": display,
                    "ts":      ts,
                    "ts_dt":   now,
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

            with _conn_lock:
                was_connected=_conn_state["connected"]
            # Update connection state
            with _conn_lock:
                _conn_state["connected"]  = True
                _conn_state["last_hello"] = datetime.now()
                _conn_state["dl_pc_addr"] = addr[0]

            # Queue toast
            if not was_connected:
                with _queue_lock:
                    _popup_queue.append({
                        "type": "HELLO",
                        "addr": addr[0],
                        "ts":   datetime.now().strftime("%H:%M:%S"),
                    })

            conn_logger.info(f"[listener] HELLO from {addr[0]} — ACK sent, Connected")

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
                process_logger.info(
                    f"[listener] {dl_name} already processing — "
                    f"duplicate signal ignored"
                )
                _send_response(conn, "OK",
                               f"Already processing: {dl_name}")
                return

            # Acknowledge before automation
            _send_response(conn, "OK", f"Stop queued for {dl_name}")

            # Mark as processing immediately so operator sees it in tray
            now = datetime.now()
            ts = now.strftime("%d-%m-%Y %H:%M:%S")
            with _state_lock:
                _dl_states[dl_name] = {"state": "processing", "ts": ts, "ts_dt": now}

            # Queue automation task
            _task_queue.put(dl_name)
            process_logger.info(f"[listener] {dl_name} queued for automation")

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

_FT_ID_MAP = {
    "F1": ("front", 1, "Function 1"),
    "F2": ("front", 2, "Function 2"),
    "F3": ("front", 3, "Function 3"),
    "F4": ("front", 4, "Function 4"),
    "R1": ("rear",  1, "Function 1"),
    "R2": ("rear",  2, "Function 2"),
    "R3": ("rear",  3, "Function 3"),
    "R4": ("rear",  4, "Function 4"),
}

def _ft_id_to_rack_function(ft_id: str)-> tuple:
    rack, _num, function_label = _FT_ID_MAP.get(
        ft_id.upper(), ("front", 1, "Function 1")
    )
    return rack, function_label

def _handle_ft_connection(conn: socket.socket, addr: tuple) -> None:
    """Handle a single connection from an FT PC on port 8998."""
    try:
        raw     = conn.recv(1024)
        data    = json.loads(raw.decode("utf-8"))
        command = data.get("command", "").upper()
        ft_id   = data.get("ft_id","").upper()

        logger.info(f"[ft_listener] {addr[0]}:{addr[1]} → {command} {ft_id}")

        if command == "HELLO":
            _send_response(conn, "ACK", "Connected")
            if ft_id in _FT_ID_MAP:
                rack, _num, _label = _FT_ID_MAP[ft_id]
                with _ft_conn_lock:
                    _ft_conn_states[ft_id] = {
                        "connected":  True,
                        "last_hello": datetime.now(),
                        "addr":       addr[0],
                        "ft_side":    rack,
                    }
                conn_logger.info(f"[ft_listener] HELLO from FT{ft_id} at {addr[0]} — ACK sent")
            else:
                conn_logger.warning(f"[ft_listener] HELLO with unknown ft_id={ft_id!r} from {addr[0]}")

        elif command == "STOP":
            if not ft_id or ft_id not in _FT_ID_MAP:
                _send_response(conn, "ERROR", f"Invalid ft_id: {ft_id!r}")
                return

            rack,function_label = _ft_id_to_rack_function(ft_id)

            task_key = f"FT_{ft_id}_{rack}_{function_label}"

            # Duplicate check — skip if already queued or processing
            with _state_lock:
                existing = _dl_states.get(task_key, {}).get("state")
            if existing in ("processing",):
                process_logger.info(
                    f"[ft_listener] {task_key} already processing — "
                    f"duplicate signal ignored")
                _send_response(conn, "OK",
                               f"Already processing: {task_key}")
                return

            _send_response(conn, "OK", f"FT stop queued: {task_key}")

            now = datetime.now()
            ts = now.strftime("%d-%m-%Y %H:%M:%S")
            with _state_lock:
                _dl_states[task_key] = {"state": "processing", "ts": ts, "ts_dt": now}

            _task_queue.put(task_key)
            process_logger.info(f"[ft_listener] {task_key} queued for automation")

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
    source    = item.get("source", "DL")   # "DL" or "FT"
    if kind == "STOP":
        bar_color = COL_BLOCK
        icon      = "⛔"
        title     = "FT STOPPED" if source == "FT" else "DL STOPPED"
        body_text = f"{dl_name}  automation completed"
    elif kind == "ERROR":
        bar_color = COL_WARN
        icon      = "⚠"
        title     = f"{source} AUTOMATION FAILED"
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
    f_med = tkfont.Font(family="Consolas", size=10, weight="bold")
    f_sml = tkfont.Font(family="Consolas", size=8,  weight="bold")

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


def _pop_next_popup_item() -> dict:
    """
    Pop the next item to display from _popup_queue.

    STOP/ERROR history items are shown newest-first: among all
    pending STOP/ERROR entries, the one with the latest ts_dt is
    popped and displayed first, regardless of DL/FT name, source, or
    queue position. This affects ONLY the display/pop order — no
    STOP/ERROR event is ever dropped or collapsed, even multiple
    events from the same device.

    HELLO/connection items are left completely untouched: if there
    is no pending STOP/ERROR item, the oldest queued item (FIFO,
    exactly as before) is popped — so HELLO ordering/behavior is
    unchanged.
    """
    stop_error_indices = [
        i for i, item in enumerate(_popup_queue)
        if item.get("type") in ("STOP", "ERROR")
    ]
    if stop_error_indices:
        newest_idx = max(
            stop_error_indices,
            key=lambda i: _popup_queue[i].get("ts_dt", datetime.min),
        )
        return _popup_queue.pop(newest_idx)
    return _popup_queue.pop(0)


# =========================================================
# Tray window
# =========================================================
class TrayWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DL  & FT Monitor — Main PC")
        self.root.configure(bg=BG_MAIN)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # Fixed static size — cannot be resized.
        # CONTENT_W is the original inner layout width (unchanged —
        # every existing .place(x=...) coordinate still lines up).
        # WINDOW_W adds room for the new outer scrollbar so it doesn't
        # clip any existing content.
        self.CONTENT_W = 320
        WINDOW_W, H = 320 + 16, 500
        x = 8
        y = max(0, sh - H - 48)
        self.root.geometry(f"{WINDOW_W}x{H}+{x}+{y}")
        self.root.maxsize(WINDOW_W, H)
        self.root.minsize(WINDOW_W, H)

        self.f_title = tkfont.Font(family="Consolas", size=10, weight="bold")
        self.f_conn  = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_body  = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_small = tkfont.Font(family="Consolas", size=8,  weight="bold")

        self._dot_count   = 0
        self._dot_anim_id = None

        self._build()
        self._poll_popup_queue()
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        W = self.CONTENT_W  # must match original inner layout width

        # ── Outer scrollable wrapper ───────────────────────
        # Whole-window scrollbar: everything below is placed onto
        # self.content (same x/y coordinates as before) instead of
        # self.root directly, so all existing .place() calls keep
        # working unchanged while the whole popup becomes scrollable.
        outer_canvas = tk.Canvas(self.root, bg=BG_MAIN, highlightthickness=0)
        outer_scrollbar = tk.Scrollbar(self.root, orient="vertical",
                                       command=outer_canvas.yview, width=10)

        def _update_outer_scrollbar(*args):
            lo, hi = map(float, args)
            if lo <= 0.0 and hi >= 1.0:
                outer_scrollbar.pack_forget()
            else:
                outer_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        outer_canvas.configure(yscrollcommand=_update_outer_scrollbar)
        outer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.content = tk.Frame(outer_canvas, bg=BG_MAIN, width=W, height=500)
        self._content_window_id = outer_canvas.create_window(
            (0, 0), window=self.content, anchor="nw")

        def _on_content_resize(e):
            outer_canvas.configure(scrollregion=outer_canvas.bbox("all"))
        self.content.bind("<Configure>", _on_content_resize)

        def _on_outer_canvas_resize(e):
            # Keep content pinned to the canvas's own width, not the
            # window's — avoids the content stretching under the
            # scrollbar column.
            pass
        outer_canvas.bind("<Configure>", _on_outer_canvas_resize)

        def _on_root_mousewheel(e):
            outer_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        outer_canvas.bind_all("<MouseWheel>", _on_root_mousewheel)
        self._outer_canvas = outer_canvas

        # ── Header y=0 h=34 ───────────────────────────────
        hdr = tk.Frame(self.content, bg=BG_HEADER, width=W, height=34)
        hdr.place(x=0, y=0)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="DL & FT Monitor  *  Main PC",
                 font=self.f_title, bg=BG_HEADER,
                 fg=COL_WHITE).pack(expand=True)

        # ── Connection y=34 h=42 ──────────────────────────
        conn = tk.Frame(self.content, bg=BG_CARD, width=W, height=42)
        conn.place(x=0, y=34)
        conn.pack_propagate(False)
        self.lbl_conn_dot = tk.Label(conn, text="*",
                                      font=self.f_conn,
                                      bg=BG_CARD, fg=COL_CHECK)
        self.lbl_conn_dot.place(x=8, y=4)
        self.lbl_conn_status = tk.Label(conn, text="Waiting for DL PC...",
                                         font=self.f_conn,
                                         bg=BG_CARD, fg=COL_CHECK)
        self.lbl_conn_status.place(x=26, y=4)
        self.lbl_conn_since = tk.Label(conn, text="",
                                        font=self.f_small,
                                        bg=BG_CARD, fg=COL_MUTED)
        self.lbl_conn_since.place(x=26, y=22)
        self._start_dot_animation()

        # ── Separator y=76 ────────────────────────────────
        tk.Frame(self.content, bg=COL_BORDER, width=W, height=1).place(x=0, y=76)

        # ── Status rows y=77 h=66 ─────────────────────────
        status = tk.Frame(self.content, bg=BG_CARD, width=W, height=66)
        status.place(x=0, y=77)
        status.pack_propagate(False)

        def _srow(label, attr, val, color, y):
            tk.Label(status, text=label, font=self.f_small,
                     bg=BG_CARD, fg=COL_MUTED,
                     width=11, anchor="w").place(x=8, y=y)
            lbl = tk.Label(status, text=val,
                           font=self.f_small, bg=BG_CARD, fg=color)
            lbl.place(x=100, y=y)
            setattr(self, attr, lbl)

        _srow("Ports",
              "lbl_listener",
              f"* DL:{_listen_port()}  FT:{_ft_listen_port()}",
              COL_OK, 4)
        _srow("Queue",      "lbl_queue", "0 pending",   COL_TEXT,  26)
        _srow("InLine_Pro", "lbl_app",   "checking...", COL_MUTED, 48)

        # ── Separator y=143 ───────────────────────────────
        tk.Frame(self.content, bg=COL_BORDER, width=W, height=1).place(x=0, y=143)

        # ── FT dots y=144 h=50 ────────────────────────────
        ft_frame = tk.Frame(self.content, bg=BG_CARD, width=W, height=50)
        ft_frame.place(x=0, y=144)
        ft_frame.pack_propagate(False)
        self.ft_dots = {}

        tk.Label(ft_frame, text="Front:", font=self.f_small,
                 bg=BG_CARD, fg=COL_MUTED).place(x=8, y=4)
        tk.Label(ft_frame, text="Rear:",  font=self.f_small,
                 bg=BG_CARD, fg=COL_MUTED).place(x=8, y=26)

        # Start dots at x=70 to leave clear gap after label
        for i, key in enumerate(["F1","F2","F3","F4"]):
            dot = tk.Label(ft_frame, text=key, font=self.f_small,
                           bg=BG_CARD, fg=COL_MUTED, padx=2)
            dot.place(x=70 + i*56, y=4)
            self.ft_dots[key] = dot
        for i, key in enumerate(["R1","R2","R3","R4"]):
            dot = tk.Label(ft_frame, text=key, font=self.f_small,
                           bg=BG_CARD, fg=COL_MUTED, padx=2)
            dot.place(x=70 + i*56, y=26)
            self.ft_dots[key] = dot

        # ── Separator y=194 ───────────────────────────────
        tk.Frame(self.content, bg=COL_BORDER, width=W, height=1).place(x=0, y=194)

        # ── Activity label y=196 ──────────────────────────
        tk.Label(self.content, text="Activity today  (DL + FT)",
                 font=self.f_small, bg=BG_MAIN,
                 fg=COL_MUTED).place(x=10, y=198)

        # ── Scrollable list y=216 h=200 ───────────────────
        list_outer = tk.Frame(self.content, bg=BG_MAIN,
                              width=W-12, height=200)
        list_outer.place(x=6, y=216)
        list_outer.pack_propagate(False)

        canvas = tk.Canvas(list_outer, bg=BG_MAIN, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_outer, orient="vertical",
                                 command=canvas.yview,
                                 width=6)

        def _update_scrollbar(*args):
            lo, hi = map(float, args)
            if lo <= 0.0 and hi >= 1.0:
                scrollbar.pack_forget()
            else:
                scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        canvas.configure(yscrollcommand=_update_scrollbar)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.list_frame = tk.Frame(canvas, bg=BG_MAIN)
        self._list_canvas_id = canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw")

        def _on_frame_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(self._list_canvas_id,
                              width=canvas.winfo_width())

        self.list_frame.bind("<Configure>", _on_frame_resize)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(
                        self._list_canvas_id, width=e.width))

        def _on_mousewheel(e):
            # Local bind (not bind_all) so this only fires while the
            # mouse is actually over the activity list, and returns
            # "break" so the event doesn't also reach the outer
            # window-level scrollbar's bind_all handler above
            # (avoids double-scrolling both lists at once).
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"
        canvas.bind("<MouseWheel>", _on_mousewheel)
        self._list_canvas = canvas

        # ── Separator y=420 ───────────────────────────────
        tk.Frame(self.content, bg=COL_BORDER, width=W, height=1).place(x=0, y=420)

        # ── Footer y=421 — always at fixed position ────────
        self.lbl_time = tk.Label(self.content, text="",
                                  font=self.f_small,
                                  bg=BG_MAIN, fg=COL_MUTED)
        self.lbl_time.place(x=0, y=428, width=W, anchor="nw")

        self.btn_clear = tk.Button(self.content,
                                    text="Clear blocked",
                                    command=self._clear_blocked,
                                    bg=BG_HEADER, fg=COL_MUTED,
                                    font=self.f_small, relief=tk.RAISED,
                                    padx=12, pady=4,
                                    cursor="hand2", state=tk.DISABLED)
        self.btn_clear.place(x=W//2, y=455, anchor="n")


    # ── Connection dot animation ──────────────────────────
    def _start_dot_animation(self):
        self._stop_dot_animation()
        self._animate_dots()

    def _animate_dots(self):
        dots = "." * (self._dot_count % 4)
        self.lbl_conn_status.config(text=f"Waiting for DL PC {dots}")
        self._dot_count += 1
        self._dot_anim_id = self.root.after(500, self._animate_dots)

    def _stop_dot_animation(self):
        if self._dot_anim_id:
            self.root.after_cancel(self._dot_anim_id)
            self._dot_anim_id = None

    # ── Refresh ───────────────────────────────────────────
    def _refresh(self) -> None:
        # Wrapped so ONE bad tick (e.g. a malformed _dl_states entry,
        # a Tk widget error) can never permanently freeze the tray.
        # Previously, any exception here skipped the self.root.after()
        # reschedule at the end — since the console is hidden
        # (hide_console()), that failure was invisible, and the tray
        # would silently stop refreshing forever, frozen on whatever
        # was last rendered (which could look like a stuck/"unordered"
        # Activity list, when really it just stopped updating).
        try:
            self._update_conn_status()
            self._update_ft_dots()
            self._refresh_blocked()

            qsize = _task_queue.qsize()
            self.lbl_queue.config(
                text=f"{qsize} pending",
                fg=COL_WARN if qsize > 0 else COL_TEXT,
            )
            self._check_app_status()
            self.lbl_time.config(
                text=f"Updated: {datetime.now().strftime('%H:%M:%S')}"
            )
        except Exception as e:
            logger.error(f"[tray] _refresh() error: {e}")
        finally:
            self.root.after(5000, self._refresh)

    def _update_conn_status(self) -> None:
        with _conn_lock:
            connected  = _conn_state["connected"]
            last_hello = _conn_state["last_hello"]
            dl_addr    = _conn_state["dl_pc_addr"]

        # A HELLO must be seen at least once every hello_timeout_minutes
        # (default 16) or DL PC is treated as disconnected — previously
        # "connected" was set True on the first HELLO and never re-checked,
        # so the tray kept showing "Connected" forever even after DL PC
        # actually went offline.
        stale = False
        if connected and last_hello:
            age_min = (datetime.now() - last_hello).total_seconds() / 60
            if age_min > _hello_timeout_minutes():
                stale = True
                with _conn_lock:
                    _conn_state["connected"] = False
                conn_logger.warning(
                    f"[conn] DL PC HELLO stale — no HELLO for "
                    f"{age_min:.1f}min (limit {_hello_timeout_minutes()}min) "
                    f"→ Disconnected"
                )

        if connected and last_hello and not stale:
            self._stop_dot_animation()
            self.lbl_conn_dot.config(fg=COL_OK)
            self.lbl_conn_status.config(
                text=f"Connected  •  {dl_addr}",
                fg=COL_OK,
            )
            self.lbl_conn_since.config(
                text=f"since {last_hello.strftime('%H:%M:%S')}",
                fg=COL_MUTED,
            )
        elif last_hello:
            # Was connected before, HELLO went stale — show Disconnected
            # (red), not the "waiting for first HELLO" animation.
            self._stop_dot_animation()
            self.lbl_conn_dot.config(fg=COL_DISC)
            self.lbl_conn_status.config(
                text=f"Disconnected  •  {dl_addr}",
                fg=COL_DISC,
            )
            self.lbl_conn_since.config(
                text=f"last seen {last_hello.strftime('%H:%M:%S')}",
                fg=COL_MUTED,
            )
        else:
            # Never connected yet
            self.lbl_conn_dot.config(fg=COL_CHECK)
            self.lbl_conn_since.config(text="", fg=COL_MUTED)
            if self._dot_anim_id is None:
                self._start_dot_animation()

    def _refresh_blocked(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        with _state_lock:
            states = dict(_dl_states)

        if  states:
            self.btn_clear.config(
                state=tk.NORMAL,
                fg=COL_TEXT,
                cursor="hand2")
        else:
            self.btn_clear.config(
                state=tk.DISABLED,
                fg=COL_MUTED,
                cursor="arrow"
            )
        if not states:
            tk.Label(self.list_frame,text="No activity yet",
                     font=self.f_small,bg=BG_MAIN,
                     fg=COL_MUTED).pack(pady=6)
            return

        # Count each state for summary header
        n_proc  = sum(1 for v in states.values() if v["state"] == "processing")
        n_stop  = sum(1 for v in states.values() if v["state"] == "stopped")
        n_err   = sum(1 for v in states.values() if v["state"] == "error")

        summary = []
        if n_proc: summary.append(f"{n_proc} processing")
        if n_stop: summary.append(f"{n_stop} stopped")
        if n_err:  summary.append(f"{n_err} error")

        tk.Label(
            self.list_frame,
            text="  •  ".join(summary),
            font=self.f_small, bg=BG_MAIN,
            fg=COL_WARN if n_err else COL_BLOCK,
        ).pack(anchor="w", pady=(0, 4))

        # State colors and labels
        STATE_COLOR = {
            "processing": COL_CHECK,   # blue
            "stopped":    COL_BLOCK,   # red
            "error":      COL_WARN,    # yellow
        }
        STATE_LABEL = {
            "processing": "Processing...",
            "stopped":    "Stopped",
            "error":      "⚠ Error — manual needed",
        }

        # Sort by when the entry was last updated — newest first,
        # so the most recently stopped/processing DL or FT shows at top
        # instead of being grouped by DL/FT number.
        def _sort_key(item):
            _dl, info = item
            return info.get("ts_dt") or datetime.min

        for dl, info in sorted(states.items(), key=_sort_key, reverse=True):
            try:
                state     = info["state"]
                ts        = info["ts"]
                color     = STATE_COLOR.get(state, COL_MUTED)
                label_txt = STATE_LABEL.get(state, state)
                display   = _task_display_name(dl)

                # Split "DD-MM-YYYY HH:MM:SS" into separate date/time
                # parts so date lines up with the DL/FT number and time
                # lines up with the state label, instead of one combined
                # timestamp.
                date_part, _, time_part = ts.partition(" ")
                if not time_part:
                    # Fallback for any legacy/short ts value (time-only)
                    date_part, time_part = "", ts

                row = tk.Frame(self.list_frame, bg=BG_CARD,
                               pady=4, padx=8,
                               highlightbackground=color,
                               highlightthickness=1)
                row.pack(fill=tk.X, pady=2)

                # Line 1: DL/FT name on left, DATE on right
                line1 = tk.Frame(row, bg=BG_CARD)
                line1.pack(fill=tk.X)
                tk.Label(line1, text=display,
                         font=self.f_body, bg=BG_CARD,
                         fg=color, anchor="w").pack(side=tk.LEFT)
                tk.Label(line1, text=date_part,
                         font=self.f_small, bg=BG_CARD,
                         fg=COL_MUTED).pack(side=tk.RIGHT)

                # Line 2: state label (Stopped / manual intervention
                # needed) on left, TIME on right
                line2 = tk.Frame(row, bg=BG_CARD)
                line2.pack(fill=tk.X)
                tk.Label(line2, text=label_txt,
                         font=self.f_small, bg=BG_CARD,
                         fg=color, anchor="w").pack(side=tk.LEFT)
                tk.Label(line2, text=time_part,
                         font=self.f_small, bg=BG_CARD,
                         fg=COL_MUTED).pack(side=tk.RIGHT)
            except Exception as e:
                # Skip just this one malformed entry — a single bad
                # row must not abort rendering (and therefore sorting)
                # of the rest of the list.
                logger.error(f"[tray] Activity row error for {dl!r}: {e}")
                continue

    def _update_ft_dots(self) -> None:
        """Update FT dot colors — green=connected, grey=not connected, dim=beyond setup count."""
        with _ft_conn_lock:
            states = dict(_ft_conn_states)

        for ft_id_key, dot in self.ft_dots.items():
            info = states.get(ft_id_key, {})
            if info.get("connected") and info.get("last_hello"):
                try:
                    secs  = (datetime.now() - info['last_hello']).total_seconds()
                    alive = secs < 3660   # ~1 hour grace (heartbeat=1800s)
                except Exception:
                    alive = False
                dot.config(fg=COL_OK if alive else COL_DISC)
            else:
                dot.config(fg=COL_MUTED)

    def _check_app_status(self) -> None:
        import subprocess
        exe = _exe_name()
        try:
            si = None
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                capture_output=True, text=True, timeout=3,
                startupinfo=si, creationflags=creationflags,
            )
            if exe.lower() in out.stdout.lower():
                self.lbl_app.config(text="● running", fg=COL_OK)
            else:
                self.lbl_app.config(text="● not found", fg=COL_BLOCK)
        except Exception:
            self.lbl_app.config(text="● unknown", fg=COL_MUTED)

    def _poll_popup_queue(self) -> None:
        with _queue_lock:
            while _popup_queue:
                show_toast(self.root, _pop_next_popup_item())
        self.root.after(500, self._poll_popup_queue)

    def _on_close(self) -> None:
        """
        Intercept window close button (X).
        Hides the window instead of destroying it so the
        TCP listeners and queue worker keep running in background.
        Double-click tray icon or run again to restore.
        """
        self.root.withdraw()
        logger.info("[tray] Window hidden — listeners still running. "
                    "Run again or double-click icon to restore.")

    def _restore(self) -> None:
        """Restore hidden window."""
        self.root.deiconify()
        self.root.lift()

    def _clear_blocked(self) -> None:
        with _state_lock:
            _dl_states.clear()
        logger.info("[tray] DL state list cleared by operator")
        self._refresh_blocked()
        # Scroll list back to top after clearing
        try:
            self._list_canvas.yview_moveto(0)
        except Exception:
            pass


# =========================================================
# Spy helper
# =========================================================
def _spy_controls() -> None:
    from pywinauto import Application
    title = get_config()["app"]["window_title"]
    print(f"\nConnecting to: '{title}' ...")
    try:
        app = Application(backend="uia").connect(title=title, timeout=10)
        win = app.window(title=title)
        print("\n=== Main Window Controls ===")
        win.print_control_identifiers()
        print("\nOpen any dialog then press Enter ...")
        input()
        dlg = app.top_window()
        print("\n=== Top Dialog Controls ===")
        dlg.print_control_identifiers()
    except Exception as e:
        print(f"Error: {e}")


# =========================================================
# =========================================================
# Entry point
# =========================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "spy":
        _spy_controls()
        sys.exit(0)

    hide_console()

    # ── Single instance check ──────────────────────────────
    si = SingleInstance("MainPCPopup")
    if not si.acquire():
        si.signal_restore()
        sys.exit(0)

    # Tk root FIRST — required on Windows before starting threads
    root     = tk.Tk()
    tray_win = TrayWindow(root)

    threading.Thread(target=_queue_worker,          daemon=True).start()
    threading.Thread(target=_start_tcp_listener,     daemon=True).start()
    threading.Thread(target=_start_ft_tcp_listener,  daemon=True).start()

    logger.info(
        f"[main] Main PC popup started — "
        f"DL port={_listen_port()}, FT port={_ft_listen_port()}"
    )

    # ── System tray integration ────────────────────────────
    def _show():
        root.after(0, lambda: [root.deiconify(), root.lift(),
                               root.focus_force()])

    def _hide():
        root.after(0, root.withdraw)

    def _do_exit():
        si.release()
        sys_tray.stop()
        _task_queue.put(None)
        root.after(0, root.destroy)

    sys_tray = TrayIconManager(
        app_name="Main PC Monitor",
        on_show=_show,
        on_hide=_hide,
        on_exit=_do_exit,
    )
    sys_tray.start()

    # Close button hides to tray
    root.protocol("WM_DELETE_WINDOW", _hide)

    # Listen for restore signal from second instance
    si.start_listener(on_restore_callback=_show)

    try:
        root.mainloop()
    finally:
        si.release()
        sys_tray.stop()

    _task_queue.put(None)
