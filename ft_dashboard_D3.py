"""
ft_dashboard.py  —  FT PC

Direct port of DL_PC/dl_dashboard.py — same header, connection bar,
summary bar, card grid, detail popup, threshold editor, IP editor,
footer and tray behaviour. The only differences:

  - Cards are FT stations (F1..F4 / R1..R4) from setup_type instead
    of DL01..DL20 from dl_count.
  - Stats are read from the per-station JSON that ft_process_file.py
    writes (the FT invariant: the dashboard never touches CSVs or
    calculates anything itself).
  - ONE HELLO is sent for the whole PC covering every station.

Connection bar:
  - On startup sends HELLO to Main PC FT port
  - Main PC replies ACK → both show Connected
  - Bar shows: Checking... → Connected / Disconnected
  - Stays connected until a failed heartbeat detects drop
  - Auto-retries every heartbeat_sec when disconnected
"""

import sys
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox
from datetime import datetime
from tray_utils_D3 import SingleInstance, TrayIconManager, hide_console

from ft_config_loader_D3 import (
    get_config, save_config,
    warn_at_fail, block_at_fail, log_dir,
    refresh_ms, heartbeat_sec, main_pc_ip, main_pc_port,
    no_data_minutes, ft_stations, station_display, setup_type,
)
from ft_network_sender_D3 import send_hello_all
from ft_process_file_D3 import load_stats, _empty_stats

# =========================================================
# Config accessors — mirror DL's naming
# =========================================================
def LOG_DIRECTORY()   -> str: return log_dir()
def MAIN_PC_IP()      -> str: return main_pc_ip()
def MAIN_PC_PORT()    -> int: return main_pc_port()
def WARN_AT_FAILS()   -> int: return warn_at_fail()
def BLOCK_AT_FAILS()  -> int: return block_at_fail()
def REFRESH_MS()      -> int: return refresh_ms()
def NO_DATA_MINUTES() -> int: return no_data_minutes()
def HEARTBEAT_SEC()   -> int: return heartbeat_sec()
def FT_COUNT()        -> int: return len(ft_stations())

# Layout is derived from the station count so the dashboard resizes
# itself when setup_type changes between 6 and 8:
#   8 stations -> 4 columns (F1-F4 top row, R1-R4 bottom row)
#   6 stations -> 3 columns (F1-F3 top row, R1-R3 bottom row)
# Front rack always fills the top row, rear rack the bottom row.
CARD_W   = 195   # px budget per card column
MARGIN_W = 120   # px of chrome either side
BASE_H   = 620   # height for the standard 2-row layout


def GRID_COLS() -> int:
    return max(1, FT_COUNT() // 2)


def WINDOW_W() -> int:
    return MARGIN_W + GRID_COLS() * CARD_W


def WINDOW_H() -> int:
    rows = max(1, -(-FT_COUNT() // GRID_COLS()))   # ceil division
    return BASE_H if rows <= 2 else BASE_H + (rows - 2) * 150

# =========================================================
# Colors — identical to DL dashboard
# =========================================================
BG_MAIN    = "#0d1117"
BG_CARD    = "#161b22"
BG_HEADER  = "#1c2128"
BG_POPUP   = "#161b22"
BG_CONNBAR = "#0d1117"
COL_RUN    = "#3fb950"
COL_WARN   = "#d29922"
COL_BLOCK  = "#f85149"
COL_STOP   = "#6e7681"
COL_CHECK  = "#58a6ff"   # blue — Checking
COL_CONN   = "#3fb950"   # green — Connected
COL_DISC   = "#f85149"   # red — Disconnected
COL_TEXT   = "#c9d1d9"
COL_MUTED  = "#6e7681"
COL_WHITE  = "#ffffff"
COL_ACCT   = "#58a6ff"
COL_BORDER = "#30363d"

# =========================================================
# Connection states
# =========================================================
CONN_CHECKING     = "checking"
CONN_CONNECTED    = "connected"
CONN_DISCONNECTED = "disconnected"


# =========================================================
# HELLO/ACK handshake — TCP
# Sends ONE HELLO covering every station on this FT PC.
# Expects {"status": "ACK"} back.
# Returns (state, detail_message)
# =========================================================
def do_handshake() -> tuple:
    ip   = MAIN_PC_IP()
    port = MAIN_PC_PORT()
    try:
        if send_hello_all(ft_stations()):
            return CONN_CONNECTED, f"Connected to {ip}:{port}"
        return CONN_DISCONNECTED, f"No ACK from {ip}:{port}"
    except Exception as e:
        return CONN_DISCONNECTED, str(e)


# =========================================================
# Status color helper
# =========================================================
def _status_color(status: str) -> str:
    return {
        "RUNNING": COL_RUN,
        "WARNING": COL_WARN,
        "BLOCKED": COL_BLOCK,
        "STOPPED": COL_STOP,
    }.get(status, COL_STOP)


# =========================================================
# Collect FT stats — reads ONLY the JSON written per station
# by ft_process_file.py. No CSV processing, no calculation.
# Returns the same dict shape the DL dashboard cards expect.
# =========================================================
def compute_ft_stats() -> list:
    stats = []
    for station in ft_stations():
        try:
            s = load_stats(station)
        except Exception:
            s = _empty_stats(station)
        stats.append({
            "name":          station,
            "display":       station_display(station),
            "total":         s.get("total", 0),
            "fails":         s.get("fails", 0),
            "rate":          s.get("rate", 0.0),
            "status":        s.get("status", "STOPPED"),
            "last_stop":     s.get("last_stop", "—"),
            "last_data":     s.get("last_data", "—"),
            "minutes_since": s.get("minutes_since", 0),
            "fails_60":      s.get("fails_60", 0),
            "rate_60":       s.get("rate_60", 0.0),
            "blocked_min":   s.get("blocked_min", 0),
            "blocked_since": s.get("blocked_since", "—"),
            "stops_today":   s.get("stops_today", 0),
        })
    return stats


# =========================================================
# Popups
# =========================================================
class DetailPopup(tk.Toplevel):
    def __init__(self, parent, stat: dict):
        super().__init__(parent)
        self.title(f"{stat['name']} — Detail")
        self.configure(bg=BG_POPUP)
        self.resizable(False, False)
        self.grab_set()

        f_title = tkfont.Font(family="Consolas", size=14, weight="bold")
        f_label = tkfont.Font(family="Consolas", size=10)
        f_value = tkfont.Font(family="Consolas", size=12, weight="bold")
        f_small = tkfont.Font(family="Consolas", size=9)

        status = stat["status"]
        color  = _status_color(status)

        hdr = tk.Frame(self, bg=color, pady=10, padx=20)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text=stat["name"], font=f_title,
                 bg=color, fg=BG_MAIN).pack(side=tk.LEFT)
        tk.Label(hdr, text=status, font=f_title,
                 bg=color, fg=BG_MAIN).pack(side=tk.RIGHT)

        body = tk.Frame(self, bg=BG_POPUP, padx=24, pady=20)
        body.pack(fill=tk.BOTH, expand=True)

        rows = [
            ("Station",         stat.get("display", stat["name"]), COL_TEXT),
            ("Status",          status,                            color),
            ("Fail Count",      str(stat["fails"]),                color),
            ("Fail Rate",       f"{stat['rate']:.2f}%",            COL_TEXT),
            ("Total Records",   str(stat["total"]),                COL_TEXT),
            ("Last-60 Fails",   str(stat.get("fails_60", 0)),      COL_TEXT),
            ("Last-60 Rate",    f"{stat.get('rate_60', 0.0):.2f}%", COL_TEXT),
            ("Last Stop",       stat["last_stop"],                 COL_WARN),
            ("Blocked Since",   stat.get("blocked_since", "—"),    COL_BLOCK),
            ("Blocked (min)",   str(stat.get("blocked_min", 0)),   COL_BLOCK),
            ("Stops Today",     str(stat.get("stops_today", 0)),   COL_WARN),
            ("Last Data",       stat.get("last_data", "—"),        COL_TEXT),
            ("Warn Threshold",  f"≥ {WARN_AT_FAILS()} fails",     COL_WARN),
            ("Block Threshold", f"≥ {BLOCK_AT_FAILS()} fails",    COL_BLOCK),
        ]

        for r, (label, value, val_color) in enumerate(rows):
            tk.Label(body, text=label, font=f_label,
                     bg=BG_POPUP, fg=COL_MUTED,
                     anchor="w", width=18).grid(row=r, column=0, sticky="w", pady=3)
            tk.Label(body, text=value, font=f_value,
                     bg=BG_POPUP, fg=val_color,
                     anchor="w").grid(row=r, column=1, sticky="w", padx=12, pady=3)

        tk.Frame(body, bg=COL_BORDER, height=1).grid(
            row=len(rows), column=0, columnspan=2, sticky="ew", pady=8)
        tk.Label(body,
                 text=f"Snapshot  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}",
                 font=f_small, bg=BG_POPUP, fg=COL_MUTED).grid(
                 row=len(rows)+1, column=0, columnspan=2, sticky="w")

        tk.Button(self, text="Close", command=self.destroy,
                  bg=BG_HEADER, fg=COL_TEXT, font=f_label,
                  relief=tk.FLAT, padx=20, pady=8, cursor="hand2").pack(pady=(0, 16))

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")


class ThresholdEditor(tk.Toplevel):
    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.title("Edit Fail Thresholds")
        self.configure(bg=BG_POPUP)
        self.resizable(False, False)
        self.grab_set()
        self.on_save = on_save

        f_label = tkfont.Font(family="Consolas", size=10)
        f_title = tkfont.Font(family="Consolas", size=12, weight="bold")

        tk.Label(self, text="Fail Thresholds", font=f_title,
                 bg=BG_POPUP, fg=COL_WHITE).pack(pady=(16, 4))
        tk.Label(self, text="Saved to ft_config.json immediately.",
                 font=tkfont.Font(family="Consolas", size=8),
                 bg=BG_POPUP, fg=COL_MUTED).pack(pady=(0, 12))

        form = tk.Frame(self, bg=BG_POPUP, padx=24, pady=8)
        form.pack()

        tk.Label(form, text="Warn at fails  ≥", font=f_label,
                 bg=BG_POPUP, fg=COL_WARN, anchor="w", width=20
                 ).grid(row=0, column=0, sticky="w", pady=6)
        self.warn_var = tk.StringVar(value=str(WARN_AT_FAILS()))
        tk.Entry(form, textvariable=self.warn_var, width=6,
                 font=f_label, bg=BG_HEADER, fg=COL_WHITE,
                 insertbackground=COL_WHITE, relief=tk.FLAT
                 ).grid(row=0, column=1, padx=8)

        tk.Label(form, text="Block at fails ≥", font=f_label,
                 bg=BG_POPUP, fg=COL_BLOCK, anchor="w", width=20
                 ).grid(row=1, column=0, sticky="w", pady=6)
        self.block_var = tk.StringVar(value=str(BLOCK_AT_FAILS()))
        tk.Entry(form, textvariable=self.block_var, width=6,
                 font=f_label, bg=BG_HEADER, fg=COL_WHITE,
                 insertbackground=COL_WHITE, relief=tk.FLAT
                 ).grid(row=1, column=1, padx=8)

        tk.Label(form, text="Setup type (6 or 8)", font=f_label,
                 bg=BG_POPUP, fg=COL_TEXT, anchor="w", width=20
                 ).grid(row=2, column=0, sticky="w", pady=6)
        self.setup_var = tk.StringVar(value=str(setup_type()))
        setup_menu = tk.OptionMenu(form, self.setup_var, "6", "8")
        setup_menu.config(bg=BG_HEADER, fg=COL_WHITE, font=f_label,
                          relief=tk.FLAT, activebackground=BG_HEADER,
                          highlightthickness=0)
        setup_menu["menu"].config(bg=BG_HEADER, fg=COL_WHITE, font=f_label)
        setup_menu.grid(row=2, column=1, padx=8, sticky="w")

        btn_row = tk.Frame(self, bg=BG_POPUP)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="Save", command=self._save,
                  bg=COL_RUN, fg=BG_MAIN, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=BG_HEADER, fg=COL_TEXT, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=8)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        try:
            warn  = int(self.warn_var.get())
            block = int(self.block_var.get())
            stype = self.setup_var.get().strip()
            if warn < 1 or block < 1:
                raise ValueError("Must be ≥ 1")
            if warn >= block:
                raise ValueError("Warn must be less than Block")
            if stype not in ("6", "8"):
                raise ValueError("Setup type must be 6 or 8")
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e), parent=self)
            return
        cfg = get_config()
        cfg["thresholds"]["warn_at_fail"]   = warn
        cfg["thresholds"]["block_at_fails"] = block
        cfg["setup_type"]                   = stype
        save_config(cfg)
        self.on_save()
        self.destroy()


class IPEditor(tk.Toplevel):
    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.title("Edit Main PC IP")
        self.configure(bg=BG_POPUP)
        self.resizable(False, False)
        self.grab_set()
        self.on_save = on_save

        f_label = tkfont.Font(family="Consolas", size=10)
        f_title = tkfont.Font(family="Consolas", size=12, weight="bold")

        tk.Label(self, text="Main PC IP Address", font=f_title,
                 bg=BG_POPUP, fg=COL_WHITE).pack(pady=(16, 4))

        form = tk.Frame(self, bg=BG_POPUP, padx=24, pady=8)
        form.pack()
        tk.Label(form, text="IP Address", font=f_label,
                 bg=BG_POPUP, fg=COL_TEXT, anchor="w", width=12
                 ).grid(row=0, column=0, sticky="w", pady=6)
        self.ip_var = tk.StringVar(value=MAIN_PC_IP())
        tk.Entry(form, textvariable=self.ip_var, width=20,
                 font=f_label, bg=BG_HEADER, fg=COL_WHITE,
                 insertbackground=COL_WHITE, relief=tk.FLAT
                 ).grid(row=0, column=1, padx=8)

        btn_row = tk.Frame(self, bg=BG_POPUP)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="Save", command=self._save,
                  bg=COL_RUN, fg=BG_MAIN, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=BG_HEADER, fg=COL_TEXT, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=8)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("Invalid", "IP cannot be empty.", parent=self)
            return
        cfg = get_config()
        cfg["network"]["main_pc_ip"] = ip
        save_config(cfg)
        self.on_save()
        self.destroy()


# =========================================================
# Main Dashboard
# =========================================================
class FTDashboard:
    def __init__(self, root: tk.Tk):
        self.root          = root
        self.root.title("FT Monitor Dashboard")
        self.root.configure(bg=BG_MAIN)
        self.root.resizable(True, True)
        self._after_id     = None
        self._last_stats   = []
        self._conn_state   = CONN_CHECKING
        self._conn_detail  = ""
        self._hb_after_id  = None   # heartbeat timer id

        self.f_title  = tkfont.Font(family="Consolas", size=15, weight="bold")
        self.f_name   = tkfont.Font(family="Consolas", size=11, weight="bold")
        self.f_val    = tkfont.Font(family="Consolas", size=10)
        self.f_status = tkfont.Font(family="Consolas", size=8,  weight="bold")
        self.f_sub    = tkfont.Font(family="Consolas", size=8)
        self.f_time   = tkfont.Font(family="Consolas", size=8)
        self.f_conn   = tkfont.Font(family="Consolas", size=9,  weight="bold")

        self._build_header()
        self._build_conn_bar()
        self._build_summary_bar()
        self._build_grid()
        self._build_footer()

        # Start handshake immediately in background
        self._start_handshake()
        self.refresh()

    # ── Header ────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=BG_HEADER, pady=10)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text="FT Monitor Dashboard",
                 font=self.f_title, bg=BG_HEADER,
                 fg=COL_WHITE).pack(side=tk.LEFT, padx=20)

        ip_block = tk.Frame(hdr, bg=BG_HEADER)
        ip_block.pack(side=tk.RIGHT, padx=16)

        tk.Label(ip_block, text="MAIN PC", font=self.f_sub,
                 bg=BG_HEADER, fg=COL_MUTED).pack(anchor="e")

        ip_row = tk.Frame(ip_block, bg=BG_HEADER)
        ip_row.pack(anchor="e")

        self.lbl_ip = tk.Label(ip_row, text=MAIN_PC_IP(),
                                font=self.f_name, bg=BG_HEADER,
                                fg=COL_ACCT, cursor="hand2")
        self.lbl_ip.pack(side=tk.LEFT)
        self.lbl_ip.bind("<Button-1>", lambda e: self._open_ip_editor())

        tk.Label(ip_block, text="click to edit", font=self.f_sub,
                 bg=BG_HEADER, fg=COL_MUTED).pack(anchor="e")

        self.lbl_time = tk.Label(hdr, text="", font=self.f_time,
                                  bg=BG_HEADER, fg=COL_MUTED)
        self.lbl_time.pack(side=tk.RIGHT, padx=20)

    # ── Connection status bar ─────────────────────────────
    def _build_conn_bar(self):
        """
        Full-width bar below header showing connection state.
        Checking... → animated dots
        Connected   → green
        Disconnected → red with retry button
        """
        self.conn_bar = tk.Frame(self.root, bg=BG_CONNBAR, pady=5)
        self.conn_bar.pack(fill=tk.X)

        left = tk.Frame(self.conn_bar, bg=BG_CONNBAR)
        left.pack(side=tk.LEFT, padx=16)

        self.lbl_conn_dot = tk.Label(
            left, text="●", font=self.f_conn,
            bg=BG_CONNBAR, fg=COL_CHECK,
        )
        self.lbl_conn_dot.pack(side=tk.LEFT, padx=(0, 6))

        self.lbl_conn_status = tk.Label(
            left, text="Checking connection...",
            font=self.f_conn, bg=BG_CONNBAR, fg=COL_CHECK,
        )
        self.lbl_conn_status.pack(side=tk.LEFT)

        right = tk.Frame(self.conn_bar, bg=BG_CONNBAR)
        right.pack(side=tk.RIGHT, padx=16)

        self.lbl_conn_detail = tk.Label(
            right, text="", font=self.f_sub,
            bg=BG_CONNBAR, fg=COL_MUTED,
        )
        self.lbl_conn_detail.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_retry = tk.Button(
            right, text="Retry",
            command=self._start_handshake,
            bg=BG_HEADER, fg=COL_TEXT,
            font=self.f_sub, relief=tk.FLAT,
            padx=10, pady=2, cursor="hand2",
        )
        self.btn_retry.pack(side=tk.LEFT)
        self.btn_retry.pack_forget()

        self._dot_count   = 0
        self._dot_anim_id = None

    def _start_dot_animation(self):
        self._stop_dot_animation()
        self._animate_dots()

    def _animate_dots(self):
        dots = "." * (self._dot_count % 4)
        try:
            self.lbl_conn_status.config(text=f"Checking connection{dots}")
        except tk.TclError:
            return
        self._dot_count += 1
        self._dot_anim_id = self.root.after(500, self._animate_dots)

    def _stop_dot_animation(self):
        if self._dot_anim_id:
            try: self.root.after_cancel(self._dot_anim_id)
            except Exception: pass
            self._dot_anim_id = None

    def _update_conn_bar(self, state: str, detail: str = ""):
        """Update connection bar UI — always called on main thread."""
        self._conn_state  = state
        self._conn_detail = detail

        if state == CONN_CHECKING:
            self._start_dot_animation()
            self.lbl_conn_dot.config(fg=COL_CHECK)
            self.lbl_conn_status.config(fg=COL_CHECK)
            self.conn_bar.config(bg=BG_CONNBAR)
            self.lbl_conn_detail.config(text="", fg=COL_MUTED)
            self.btn_retry.pack_forget()

        elif state == CONN_CONNECTED:
            self._stop_dot_animation()
            self.lbl_conn_dot.config(fg=COL_CONN)
            self.lbl_conn_status.config(
                text=f"Connected  •  Main PC {MAIN_PC_IP()}",
                fg=COL_CONN,
            )
            self.conn_bar.config(bg=BG_CONNBAR)
            self.lbl_conn_detail.config(
                text=f"since {datetime.now().strftime('%H:%M:%S')}",
                fg=COL_MUTED,
            )
            self.btn_retry.pack_forget()
            self._schedule_heartbeat()

        elif state == CONN_DISCONNECTED:
            self._stop_dot_animation()
            self.lbl_conn_dot.config(fg=COL_DISC)
            self.lbl_conn_status.config(
                text="Disconnected  •  Main PC unreachable",
                fg=COL_DISC,
            )
            self.conn_bar.config(bg=BG_CONNBAR)
            self.lbl_conn_detail.config(text=detail, fg=COL_MUTED)
            self.btn_retry.pack(side=tk.LEFT)
            self._schedule_heartbeat()

    # ── Handshake ─────────────────────────────────────────
    def _start_handshake(self):
        """Kick off HELLO handshake in background thread."""
        self._update_conn_bar(CONN_CHECKING)
        threading.Thread(target=self._run_handshake, daemon=True).start()

    def _run_handshake(self):
        state, detail = do_handshake()
        self.root.after(0, lambda: self._update_conn_bar(state, detail))

    # ── Heartbeat — detects drops after initial connect ───
    def _schedule_heartbeat(self):
        if self._hb_after_id:
            self.root.after_cancel(self._hb_after_id)
        interval = HEARTBEAT_SEC() * 1000
        self._hb_after_id = self.root.after(interval, self._heartbeat_tick)

    def _heartbeat_tick(self):
        threading.Thread(target=self._run_heartbeat, daemon=True).start()

    def _run_heartbeat(self):
        state, detail = do_handshake()
        def _update():
            if state != self._conn_state:
                self._update_conn_bar(state, detail)
            else:
                self._schedule_heartbeat()
        self.root.after(0, _update)

    # ── Summary bar ───────────────────────────────────────
    def _build_summary_bar(self):
        bar = tk.Frame(self.root, bg=BG_MAIN, pady=8)
        bar.pack(fill=tk.X, padx=16)

        def summary_card(label, color):
            f = tk.Frame(bar, bg=BG_CARD, padx=18, pady=8,
                         highlightbackground=COL_BORDER, highlightthickness=1)
            f.pack(side=tk.LEFT, padx=6)
            n = tk.Label(f, text="0", font=self.f_name, bg=BG_CARD, fg=color)
            n.pack()
            tk.Label(f, text=label, font=self.f_sub,
                     bg=BG_CARD, fg=COL_MUTED).pack()
            return n

        self.sum_run   = summary_card("RUNNING",  COL_RUN)
        self.sum_warn  = summary_card("WARNING",  COL_WARN)
        self.sum_block = summary_card("BLOCKED",  COL_BLOCK)
        self.sum_stop  = summary_card("STOPPED",  COL_STOP)

        btn_frame = tk.Frame(bar, bg=BG_MAIN)
        btn_frame.pack(side=tk.RIGHT, padx=6)

        tk.Button(btn_frame, text="⚙  Thresholds",
                  command=self._open_threshold_editor,
                  bg=BG_HEADER, fg=COL_TEXT, font=self.f_sub,
                  relief=tk.FLAT, padx=12, pady=6, cursor="hand2",
                  activebackground=COL_BORDER
                  ).pack(side=tk.LEFT, padx=4)

        tk.Button(btn_frame, text="⟳  Refresh Now",
                  command=self.refresh,
                  bg=BG_HEADER, fg=COL_WHITE, font=self.f_sub,
                  relief=tk.FLAT, padx=12, pady=6, cursor="hand2",
                  activebackground=COL_BORDER
                  ).pack(side=tk.LEFT, padx=4)

    # ── Card grid ─────────────────────────────────────────
    def _build_grid(self):
        self.grid_outer = tk.Frame(self.root, bg=BG_MAIN)
        self.grid_outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        self.grid_frame = tk.Frame(self.grid_outer, bg=BG_MAIN)
        self.grid_frame.pack(fill=tk.BOTH, expand=True)
        self._populate_grid()

    def _populate_grid(self):
        """(Re)create the card grid using the current station count."""
        cols = GRID_COLS()
        # clear any stale column weights from a previous layout
        for c in range(12):
            try:
                self.grid_frame.columnconfigure(c, weight=0, minsize=0)
            except tk.TclError:
                pass
        for c in range(cols):
            self.grid_frame.columnconfigure(c, weight=1)

        self.cards = []
        for i, station in enumerate(ft_stations()):
            card = self._make_card(self.grid_frame,
                                   i // cols, i % cols,
                                   station, i)
            self.cards.append(card)

    def _rebuild_grid(self):
        """Rebuild cards and resize the window after setup_type changes."""
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self._populate_grid()
        self._apply_window_size()

    def _apply_window_size(self):
        """Resize the window to fit the current station count."""
        w, h = WINDOW_W(), WINDOW_H()
        try:
            self.root.minsize(w, h)
            self.root.geometry(f"{w}x{h}")
        except tk.TclError:
            pass

    def _make_card(self, parent, row, col, ft_name, index):
        frame = tk.Frame(parent, bg=BG_CARD, padx=10, pady=8,
                         highlightbackground=COL_BORDER,
                         highlightthickness=1, cursor="hand2")
        frame.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
        frame.bind("<Button-1>", lambda e, idx=index: self._open_detail(idx))

        top = tk.Frame(frame, bg=BG_CARD)
        top.pack(fill=tk.X)
        top.bind("<Button-1>", lambda e, idx=index: self._open_detail(idx))

        tk.Label(top, text=ft_name, font=self.f_name,
                 bg=BG_CARD, fg=COL_WHITE,
                 cursor="hand2").pack(side=tk.LEFT)

        lbl_status = tk.Label(top, text="RUNNING", font=self.f_status,
                               bg=COL_RUN, fg=BG_MAIN, padx=6, pady=1)
        lbl_status.pack(side=tk.RIGHT)

        tk.Frame(frame, bg=COL_BORDER, height=1).pack(fill=tk.X, pady=4)

        lbl_fail = tk.Label(frame, text="0", font=self.f_name,
                             bg=BG_CARD, fg=COL_RUN, cursor="hand2")
        lbl_fail.pack()
        tk.Label(frame, text="fails this cycle", font=self.f_sub,
                 bg=BG_CARD, fg=COL_MUTED).pack()

        lbl_rate = tk.Label(frame, text="0.00%", font=self.f_val,
                             bg=BG_CARD, fg=COL_TEXT)
        lbl_rate.pack()
        tk.Label(frame, text="fail rate", font=self.f_sub,
                 bg=BG_CARD, fg=COL_MUTED).pack()

        lbl_last = tk.Label(frame, text="last stop: —", font=self.f_sub,
                             bg=BG_CARD, fg=COL_MUTED)
        lbl_last.pack(pady=(4, 0))

        for w in [lbl_fail, lbl_rate, lbl_last]:
            w.bind("<Button-1>",
                   lambda e, idx=index: self._open_detail(idx))

        return {"frame": frame, "status": lbl_status,
                "fail": lbl_fail, "rate": lbl_rate, "last": lbl_last}

    # ── Footer ────────────────────────────────────────────
    def _build_footer(self):
        self.footer_lbl = tk.Label(
            self.root, text=self._footer_text(),
            font=self.f_sub, bg=BG_HEADER, fg=COL_MUTED, pady=4,
        )
        self.footer_lbl.pack(fill=tk.X, side=tk.BOTTOM)

    def _footer_text(self) -> str:
        return (
            f"Log dir: {LOG_DIRECTORY()}   |   "
            f"Setup: {setup_type()} ({FT_COUNT()} stations)   |   "
            f"Refresh: {REFRESH_MS()//1000}s   |   "
            f"Warn ≥{WARN_AT_FAILS()}   |   "
            f"Block ≥{BLOCK_AT_FAILS()}   |   "
            f"No-data STOPPED after {NO_DATA_MINUTES()}min"
        )

    # ── Popups ────────────────────────────────────────────
    def _open_detail(self, index: int):
        if not self._last_stats or index >= len(self._last_stats):
            return
        DetailPopup(self.root, self._last_stats[index])

    def _open_threshold_editor(self):
        ThresholdEditor(self.root, on_save=self._on_config_changed)

    def _open_ip_editor(self):
        IPEditor(self.root, on_save=self._on_ip_changed)

    def _on_config_changed(self):
        # setup_type may have changed — rebuild the card grid and resize
        if len(self.cards) != FT_COUNT():
            self._rebuild_grid()
        self.footer_lbl.config(text=self._footer_text())
        self.refresh()

    def _on_ip_changed(self):
        self.lbl_ip.config(text=MAIN_PC_IP())
        self.footer_lbl.config(text=self._footer_text())
        self._start_handshake()

    # ── Dashboard refresh ─────────────────────────────────
    def refresh(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    def _fetch_and_update(self):
        try:
            stats = compute_ft_stats()
            self.root.after(0, lambda: self._update_ui(stats))
        except Exception as e:
            print(f"[ft_dashboard] Refresh error: {e}")
            self._after_id = self.root.after(REFRESH_MS(), self.refresh)

    def _update_ui(self, stats: list):
        self._last_stats = stats
        run_c = warn_c = block_c = stop_c = 0

        for i, s in enumerate(stats):
            if i >= len(self.cards):
                break
            card   = self.cards[i]
            status = s["status"]
            color  = _status_color(status)

            if   status == "RUNNING": run_c   += 1
            elif status == "WARNING": warn_c  += 1
            elif status == "BLOCKED": block_c += 1
            elif status == "STOPPED": stop_c  += 1

            card["status"].config(
                text=status, bg=color,
                fg=BG_MAIN if status != "BLOCKED" else COL_WHITE,
            )
            card["fail"].config(text=str(s["fails"]), fg=color)
            card["rate"].config(text=f"{s['rate']:.2f}%")

            if status == "BLOCKED":
                bs = s.get("blocked_since", "—")
                bm = s.get("blocked_min", 0)
                card["last"].config(
                    text=f"blocked since {bs} ({bm}min)",
                    fg=COL_BLOCK,
                )
            elif status == "STOPPED" and s.get("minutes_since", 0) > 0:
                card["last"].config(
                    text=f"no data {s['minutes_since']}min ago",
                    fg=COL_STOP,
                )
            else:
                card["last"].config(
                    text=f"last stop: {s['last_stop']}",
                    fg=COL_MUTED,
                )

            card["frame"].config(
                highlightbackground=color if status != "RUNNING" else COL_BORDER,
                highlightthickness=1,
            )

        self.sum_run.config(text=str(run_c))
        self.sum_warn.config(text=str(warn_c))
        self.sum_block.config(text=str(block_c))
        self.sum_stop.config(text=str(stop_c))

        self.lbl_time.config(
            text=f"Last updated: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}"
        )
        self._after_id = self.root.after(REFRESH_MS(), self.refresh)


# =========================================================
# Entry point
# =========================================================
if __name__ == "__main__":
    hide_console()

    # ── Single instance check ──────────────────────────────
    si = SingleInstance("FTDashboard")
    if not si.acquire():
        si.signal_restore()
        sys.exit(0)

    cfg  = get_config()
    root = tk.Tk()
    root.geometry(f"{WINDOW_W()}x{WINDOW_H()}")
    root.minsize(WINDOW_W(), WINDOW_H())
    FTDashboard(root)

    # ── System tray integration ────────────────────────────
    def _show():
        root.after(0, lambda: [root.deiconify(), root.lift(),
                               root.focus_force()])

    def _hide():
        root.after(0, root.withdraw)

    def _exit():
        si.release()
        tray.stop()
        root.after(0, root.destroy)

    tray = TrayIconManager(
        app_name="FT Dashboard",
        on_show=_show,
        on_hide=_hide,
        on_exit=_exit,
    )
    tray.start()

    def _on_close():
        root.withdraw()
        tray.show_balloon(
            "FT Dashboard",
            "Dashboard is still running in the system tray.",
            once_only=True)

    root.protocol("WM_DELETE_WINDOW", _on_close)
    si.start_listener(on_restore_callback=_show)

    try:
        root.mainloop()
    finally:
        si.release()
        tray.stop()
