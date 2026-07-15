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

logger = _setup_logger()


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
    DL task:  "DL06"           → "DL06"
    FT task:  "FT1_FRONT_Function 1" → "FT1 Front — Function 1"
    """
    if task_key.upper().startswith("FT"):
        try:
            parts = task_key.split("_", 2)
            ft    = parts[0]                        # "FT1"
            side  = parts[1].capitalize()           # "Front"
            func  = parts[2] if len(parts) > 2 else ""  # "Function 1"
            return f"{ft} {side} — {func}"
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
def _ft_to_function(ft_number: int, ft_side: str) -> str:
    """
    ft_number is 1-4 and maps DIRECTLY to Function 1-4.
    ft_side decides Front Rack or Rear Rack only.
    No setup_type needed — the number IS the function number.
    """
    return f"Function {ft_number}"


def _handle_ft_connection(conn: socket.socket, addr: tuple) -> None:
    """Handle a single connection from an FT PC on port 8998."""
    try:
        raw     = conn.recv(1024)
        data    = json.loads(raw.decode("utf-8"))
        command = data.get("command", "").upper()
        ft_num  = int(data.get("ft_number", 0))
        ft_side = data.get("ft_side", "front").lower()

        logger.info(f"[ft_listener] {addr[0]}:{addr[1]} → {command} "
                    f"FT{ft_num} {ft_side}")

        if command == "HELLO":
            _send_response(conn, "ACK", "Connected")
            # Key is (ft_num, ft_side) so FT1 Front and FT1 Rear
            # are stored independently and don't overwrite each other
            with _ft_conn_lock:
                _ft_conn_states[(ft_num, ft_side)] = {
                    "connected":  True,
                    "last_hello": datetime.now(),
                    "addr":       addr[0],
                    "ft_side":    ft_side,
                }
            logger.info(f"[ft_listener] HELLO from FT{ft_num} "
                        f"({ft_side}) at {addr[0]} — ACK sent")

        elif command == "STOP":
            if not ft_num:
                _send_response(conn, "ERROR", "Missing ft_number")
                return

            function_label = (data.get("function") or
                             _ft_to_function(ft_num, ft_side))

            task_key = f"FT{ft_num}_{ft_side.upper()}_{function_label}"

            # Duplicate check — skip if already queued or processing
            with _state_lock:
                existing = _dl_states.get(task_key, {}).get("state")
            if existing in ("processing",):
                logger.info(
                    f"[ft_listener] {task_key} already processing — "
                    f"duplicate signal ignored")
                _send_response(conn, "OK",
                               f"Already processing: {task_key}")
                return

            _send_response(conn, "OK", f"FT stop queued: {task_key}")

            ts = datetime.now().strftime("%H:%M:%S")
            with _state_lock:
                _dl_states[task_key] = {"state": "processing", "ts": ts}

            _task_queue.put(task_key)
            logger.info(f"[ft_listener] {task_key} queued for automation")

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
        self.root.geometry(f"300x420+{sw - 316}+{sh - 460}")

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
            bg=BG_MAIN, fg=COL_CHECK,
        )
        self.lbl_conn_dot.pack(side=tk.LEFT, padx=(0, 4))

        self.lbl_conn_status = tk.Label(
            conn_row, text="Checking...",
            font=self.f_conn, bg=BG_MAIN, fg=COL_CHECK,
        )
        self.lbl_conn_status.pack(side=tk.LEFT)

        self.lbl_conn_since = tk.Label(
            conn_bar, text="",
            font=self.f_small, bg=BG_MAIN, fg=COL_MUTED,
        )
        self.lbl_conn_since.pack(anchor="w", padx=18)

        # Start dot animation immediately
        self._start_dot_animation()

        tk.Frame(self.root, bg="#30363d", height=1).pack(fill=tk.X, pady=4)

        # Listener + queue + app status
        for label_text, attr_name, default, default_color in [
            ("DL port",    "lbl_listener", f"● {_listen_port()}", COL_OK),
            ("FT port",    "lbl_ft_port",  f"● {_ft_listen_port()}", COL_OK),
            ("Queue",      "lbl_queue",    "0 pending",           COL_TEXT),
            ("InLine_Pro", "lbl_app",      "checking...",         COL_MUTED),
        ]:
            row = tk.Frame(self.root, bg=BG_MAIN)
            row.pack(fill=tk.X, padx=10, pady=1)
            tk.Label(row, text=label_text, font=self.f_small,
                     bg=BG_MAIN, fg=COL_MUTED, width=10,
                     anchor="w").pack(side=tk.LEFT)
            lbl = tk.Label(row, text=default, font=self.f_small,
                           bg=BG_MAIN, fg=default_color)
            lbl.pack(side=tk.LEFT, padx=4)
            setattr(self, attr_name, lbl)

        tk.Frame(self.root, bg=COL_BORDER, height=1).pack(fill=tk.X, pady=4)

        # ── FT PC connection dots ─────────────────────────
        # FT dots — two rows: Front FT1-4 and Rear FT1-4
        ft_block = tk.Frame(self.root, bg=BG_MAIN, padx=10)
        ft_block.pack(fill=tk.X, pady=(2, 2))
        self.ft_dots = {}   # key: (ft_num, side) e.g. (1,"front")
        for side_label, side_key in [("Front:", "front"), ("Rear: ", "rear")]:
            row = tk.Frame(ft_block, bg=BG_MAIN)
            row.pack(fill=tk.X)
            tk.Label(row, text=side_label, font=self.f_small,
                     bg=BG_MAIN, fg=COL_MUTED,
                     width=6, anchor="w").pack(side=tk.LEFT)
            for n in range(1, 5):
                dot = tk.Label(row, text=f"FT{n}",
                               font=self.f_small, bg=BG_MAIN,
                               fg=COL_MUTED, padx=3)
                dot.pack(side=tk.LEFT)
                self.ft_dots[(n, side_key)] = dot

        tk.Frame(self.root, bg=COL_BORDER, height=1).pack(fill=tk.X, pady=4)

        # Blocked list header
        tk.Label(self.root, text="Activity today (DL + FT)",
                 font=self.f_small, bg=BG_MAIN,
                 fg=COL_MUTED).pack(anchor="w", padx=10)

        # ── Scrollable list with fixed height ─────────────
        # Canvas + scrollbar so the list never pushes the
        # "Clear blocked" button off-screen regardless of
        # how many DL rows accumulate.
        list_outer = tk.Frame(self.root, bg=BG_MAIN,
                              height=180)   # fixed max height
        list_outer.pack(fill=tk.X, padx=10, pady=4)
        list_outer.pack_propagate(False)    # enforce fixed height

        canvas = tk.Canvas(list_outer, bg=BG_MAIN,
                           highlightthickness=0)
        scrollbar = tk.Scrollbar(list_outer, orient="vertical",
                                 command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.list_frame = tk.Frame(canvas, bg=BG_MAIN)
        self._list_canvas_id = canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw")

        def _on_frame_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(self._list_canvas_id, width=canvas.winfo_width())

        self.list_frame.bind("<Configure>", _on_frame_resize)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(
                        self._list_canvas_id, width=e.width))

        # Mouse wheel scroll
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._list_canvas = canvas   # saved for scroll-to-top on clear

        # ── Footer — always visible, packed LAST at bottom ──
        tk.Frame(self.root, bg=COL_BORDER, height=1).pack(fill=tk.X)

        ft = tk.Frame(self.root, bg=BG_MAIN, pady=6)
        ft.pack(fill=tk.X, side=tk.BOTTOM)   # anchor to bottom
        self.lbl_time = tk.Label(ft, text="", font=self.f_small,
                                  bg=BG_MAIN, fg=COL_MUTED)
        self.lbl_time.pack()
        tk.Button(ft, text="Clear blocked",
                  command=self._clear_blocked,
                  bg=BG_HEADER, fg=COL_TEXT,
                  font=self.f_small, relief=tk.FLAT,
                  padx=10, pady=3, cursor="hand2").pack(pady=(2, 0))

    # ── Connection dot animation ──────────────────────────
    def _start_dot_animation(self):
        self._stop_dot_animation()
        self._animate_dots()

    def _animate_dots(self):
        dots = "." * (self._dot_count % 4)
        self.lbl_conn_status.config(text=f"Checking{dots}")
        self._dot_count += 1
        self._dot_anim_id = self.root.after(500, self._animate_dots)

    def _stop_dot_animation(self):
        if self._dot_anim_id:
            self.root.after_cancel(self._dot_anim_id)
            self._dot_anim_id = None

    # ── Refresh ───────────────────────────────────────────
    def _refresh(self) -> None:
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
        self.root.after(5000, self._refresh)

    def _update_conn_status(self) -> None:
        with _conn_lock:
            connected  = _conn_state["connected"]
            last_hello = _conn_state["last_hello"]
            dl_addr    = _conn_state["dl_pc_addr"]

        if connected and last_hello:
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
        else:
            # Still waiting for first HELLO
            self.lbl_conn_dot.config(fg=COL_CHECK)
            self.lbl_conn_since.config(text="", fg=COL_MUTED)
            if self._dot_anim_id is None:
                self._start_dot_animation()

    def _refresh_blocked(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        with _state_lock:
            states = dict(_dl_states)

        if not states:
            tk.Label(self.list_frame, text="No activity yet",
                     font=self.f_small, bg=BG_MAIN,
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

        for dl, info in sorted(states.items()):
            state     = info["state"]
            ts        = info["ts"]
            color     = STATE_COLOR.get(state, COL_MUTED)
            label_txt = STATE_LABEL.get(state, state)
            display   = _task_display_name(dl)

            row = tk.Frame(self.list_frame, bg=BG_CARD,
                           pady=3, padx=8,
                           highlightbackground=color,
                           highlightthickness=1)
            row.pack(fill=tk.X, pady=2)

            tk.Label(row, text=display, font=self.f_body,
                     bg=BG_CARD, fg=color,
                     anchor="w").pack(side=tk.LEFT)

            tk.Label(row, text=label_txt,
                     font=self.f_small, bg=BG_CARD,
                     fg=color).pack(side=tk.LEFT, padx=(4, 8))

            tk.Label(row, text=ts,
                     font=self.f_small, bg=BG_CARD,
                     fg=COL_MUTED).pack(side=tk.RIGHT)

    def _update_ft_dots(self) -> None:
        """
        Update FT dot colors.
        ft_dots key is (ft_number, side) e.g. (1, "front").
        _ft_conn_states key is ft_num (int); side stored in info["ft_side"].
        Green = connected recently, Red = not connected.
        """
        with _ft_conn_lock:
            states = dict(_ft_conn_states)

        for (n, side), dot in self.ft_dots.items():
            # Direct key lookup now that key is (ft_num, ft_side)
            info = states.get((n, side), {})
            if info.get("connected") and info.get("last_hello"):
                last = info["last_hello"]
                try:
                    secs  = (datetime.now() - last).total_seconds()
                    alive = secs < 3660   # ~1hr grace
                except Exception:
                    alive = False
                dot.config(fg=COL_OK if alive else COL_DISC)
            else:
                dot.config(fg=COL_MUTED)

    def _check_app_status(self) -> None:
        import subprocess
        exe = _exe_name()
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                capture_output=True, text=True, timeout=3,
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
                show_toast(self.root, _popup_queue.pop(0))
        self.root.after(500, self._poll_popup_queue)

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
# Entry point
# =========================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "spy":
        _spy_controls()
        sys.exit(0)

    threading.Thread(target=_queue_worker,         daemon=True).start()
    threading.Thread(target=_start_tcp_listener,    daemon=True).start()
    threading.Thread(target=_start_ft_tcp_listener, daemon=True).start()

    logger.info(
        f"[main] Main PC popup started — "
        f"DL port={_listen_port()}, FT port={_ft_listen_port()}"
    )

    root = tk.Tk()
    TrayWindow(root)
    root.mainloop()

    _task_queue.put(None)