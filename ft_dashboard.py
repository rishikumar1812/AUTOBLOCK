"""
ft_dashboard.py  —  FT PC
Small single-card dashboard for one FT PC.
Shows this FT's own status, fail stats, and Main PC connection.
Has config editor to set ft_number, ft_side, setup_type, main_pc_ip.

Run:  python ft_dashboard.py
"""

import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox
from datetime import datetime

from ft_config_loader import (
    get_config, save_config,
    ft_label, ft_number, ft_side,
    main_pc_ip, main_pc_port, refresh_ms, heartbeat_sec,
    warn_at_fail, block_at_fail, log_dir
)
from ft_network_sender import send_hello
from ft_process_file   import scan_and_check

# =========================================================
# Colors — same scheme as DL dashboard
# =========================================================
BG_MAIN    = "#0d1117"
BG_CARD    = "#161b22"
BG_HEADER  = "#1c2128"
BG_POPUP   = "#161b22"
COL_RUN    = "#3fb950"
COL_WARN   = "#d29922"
COL_BLOCK  = "#f85149"
COL_STOP   = "#6e7681"
COL_CHECK  = "#58a6ff"
COL_CONN   = "#3fb950"
COL_DISC   = "#f85149"
COL_TEXT   = "#c9d1d9"
COL_MUTED  = "#6e7681"
COL_WHITE  = "#ffffff"
COL_BORDER = "#30363d"


def _status_color(status: str) -> str:
    return {
        "RUNNING": COL_RUN,
        "WARNING": COL_WARN,
        "BLOCKED": COL_BLOCK,
        "STOPPED": COL_STOP,
    }.get(status, COL_STOP)


# =========================================================
# FT Config Editor popup
# =========================================================
class FTConfigEditor(tk.Toplevel):
    def __init__(self, parent, on_save):
        super().__init__(parent)
        self.title("FT PC Configuration")
        self.configure(bg=BG_POPUP)
        self.resizable(False, False)
        self.grab_set()
        self.on_save = on_save

        f_title = tkfont.Font(family="Consolas", size=12, weight="bold")
        f_label = tkfont.Font(family="Consolas", size=10)
        f_note  = tkfont.Font(family="Consolas", size=8)

        tk.Label(self, text="FT PC Configuration",
                 font=f_title, bg=BG_POPUP,
                 fg=COL_WHITE).pack(pady=(16, 2))
        tk.Label(self,
                 text="These settings are fixed once the FT PC is set up.",
                 font=f_note, bg=BG_POPUP, fg=COL_MUTED).pack(pady=(0, 12))

        form = tk.Frame(self, bg=BG_POPUP, padx=24, pady=8)
        form.pack()

        cfg = get_config()
        self._vars = {}

        # ── Text entry fields (excluding ft_side) ─────────
        text_fields = [
            ("FT Number (1-4)", "ft_num",   str(cfg["ft"]["ft_number"])),
            ("Main PC IP",      "main_ip",  cfg["network"]["main_pc_ip"]),
            ("Main PC Port",    "main_port",str(cfg["network"]["main_pc_port"])),
            ("Log Directory",   "log_dir",  cfg["paths"]["log_dir"]),
            ("Warn at fails ≥", "warn",     str(cfg["thresholds"]["warn_at_fail"])),
            ("Block at fails ≥","block",    str(cfg["thresholds"]["block_at_fails"])),
        ]

        for r, (label, key, default) in enumerate(text_fields):
            tk.Label(form, text=label, font=f_label,
                     bg=BG_POPUP, fg=COL_TEXT,
                     anchor="w", width=18).grid(
                row=r, column=0, sticky="w", pady=5)
            var = tk.StringVar(value=default)
            tk.Entry(form, textvariable=var, width=24,
                     font=f_label, bg=BG_HEADER, fg=COL_WHITE,
                     insertbackground=COL_WHITE,
                     relief=tk.FLAT).grid(row=r, column=1, padx=8, sticky="w")
            self._vars[key] = var

        # ── FT Side — radio buttons ────────────────────────
        next_row = len(text_fields)
        tk.Label(form, text="FT Side", font=f_label,
                 bg=BG_POPUP, fg=COL_TEXT,
                 anchor="w", width=18).grid(
            row=next_row, column=0, sticky="w", pady=5)

        radio_frame = tk.Frame(form, bg=BG_POPUP)
        radio_frame.grid(row=next_row, column=1, sticky="w", padx=8)

        self._side_var = tk.StringVar(value=cfg["ft"]["ft_side"])

        for side_val, side_text in [("front", "Front Rack"), ("rear", "Rear Rack")]:
            rb = tk.Radiobutton(
                radio_frame,
                text=side_text,
                value=side_val,
                variable=self._side_var,
                font=f_label,
                bg=BG_POPUP,
                fg=COL_WHITE,
                selectcolor=BG_HEADER,
                activebackground=BG_POPUP,
                activeforeground=COL_WHITE,
                cursor="hand2",
            )
            rb.pack(side=tk.LEFT, padx=(0, 16))

        btn_row = tk.Frame(self, bg=BG_POPUP)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="Save", command=self._save,
                  bg=COL_RUN, fg=BG_MAIN, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=BG_HEADER, fg=COL_TEXT, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _save(self):
        try:
            ft_num = int(self._vars["ft_num"].get())
            side   = self._side_var.get().strip().lower()
            ip     = self._vars["main_ip"].get().strip()
            port   = int(self._vars["main_port"].get())
            ldir   = self._vars["log_dir"].get().strip()
            warn   = int(self._vars["warn"].get())
            block  = int(self._vars["block"].get())

            if ft_num < 1 or ft_num > 4:
                raise ValueError("FT Number must be 1–4")
            if not ip:
                raise ValueError("Main PC IP cannot be empty")
            if warn >= block:
                raise ValueError("Warn must be less than Block")
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e), parent=self)
            return

        cfg = get_config()
        cfg["ft"]["ft_number"]              = ft_num
        cfg["ft"]["ft_side"]               = side
        cfg["network"]["main_pc_ip"]       = ip
        cfg["network"]["main_pc_port"]     = port
        cfg["paths"]["log_dir"]            = ldir
        cfg["thresholds"]["warn_at_fail"]  = warn
        cfg["thresholds"]["block_at_fails"]= block
        save_config(cfg)
        self.on_save()
        self.destroy()


# =========================================================
# Main FT Dashboard
# =========================================================
class FTDashboard:
    def __init__(self, root: tk.Tk):
        self.root       = root
        self.root.title(
            f"FT Monitor — {ft_label()} {ft_side().capitalize()} Rack"
        )
        self.root.configure(bg=BG_MAIN)
        self.root.resizable(False, False)

        self._after_id    = None
        self._hb_after_id = None
        self._conn        = False
        self._last_stats  = _empty_stats_dict()
        self._dot_count   = 0
        self._dot_anim_id = None

        self.f_title  = tkfont.Font(family="Consolas", size=13, weight="bold")
        self.f_name   = tkfont.Font(family="Consolas", size=11, weight="bold")
        self.f_val    = tkfont.Font(family="Consolas", size=10)
        self.f_status = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_sub    = tkfont.Font(family="Consolas", size=8)
        self.f_conn   = tkfont.Font(family="Consolas", size=9,  weight="bold")

        self._build()
        self._start_handshake()
        self.refresh()

    # ── Build UI ──────────────────────────────────────────
    def _build(self):
        # Header
        hdr = tk.Frame(self.root, bg=BG_HEADER, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(
            hdr,
            text=f"FT Monitor  •  {ft_label()} {ft_side().capitalize()} Rack",
            font=self.f_title, bg=BG_HEADER,
            fg=COL_WHITE).pack(side=tk.LEFT, padx=16)
        tk.Button(hdr, text="⚙ Config",
                  command=self._open_config,
                  bg=BG_HEADER, fg=COL_TEXT,
                  font=self.f_sub, relief=tk.FLAT,
                  padx=10, pady=4,
                  cursor="hand2").pack(side=tk.RIGHT, padx=12)

        # Connection bar
        conn = tk.Frame(self.root, bg=BG_MAIN, pady=6)
        conn.pack(fill=tk.X, padx=16)
        conn_row = tk.Frame(conn, bg=BG_MAIN)
        conn_row.pack(fill=tk.X)
        self.lbl_conn_dot = tk.Label(
            conn_row, text="●", font=self.f_conn,
            bg=BG_MAIN, fg=COL_CHECK)
        self.lbl_conn_dot.pack(side=tk.LEFT, padx=(0, 6))
        self.lbl_conn_status = tk.Label(
            conn_row, text="Checking connection...",
            font=self.f_conn, bg=BG_MAIN, fg=COL_CHECK)
        self.lbl_conn_status.pack(side=tk.LEFT)
        self.lbl_conn_detail = tk.Label(
            conn, text=f"Main PC: {main_pc_ip()}:{main_pc_port()}",
            font=self.f_sub, bg=BG_MAIN, fg=COL_MUTED)
        self.lbl_conn_detail.pack(anchor="w", padx=18)

        tk.Frame(self.root, bg=COL_BORDER, height=1).pack(
            fill=tk.X, padx=16, pady=4)

        # Single FT card
        card_outer = tk.Frame(self.root, bg=BG_MAIN, padx=16, pady=8)
        card_outer.pack(fill=tk.BOTH, expand=True)

        card = tk.Frame(card_outer, bg=BG_CARD, padx=16, pady=12,
                        highlightbackground=COL_BORDER,
                        highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True)

        # Card header row
        top = tk.Frame(card, bg=BG_CARD)
        top.pack(fill=tk.X)
        tk.Label(top, text=ft_label(), font=self.f_name,
                 bg=BG_CARD, fg=COL_WHITE).pack(side=tk.LEFT)
        self.lbl_status = tk.Label(
            top, text="RUNNING", font=self.f_status,
            bg=COL_RUN, fg=BG_MAIN, padx=8, pady=2)
        self.lbl_status.pack(side=tk.RIGHT)

        # Function label
        from ft_config_loader import ft_function_name
        tk.Label(card,
                 text=f"→ {ft_function_name()}  ({ft_side().capitalize()} Rack)",
                 font=self.f_sub, bg=BG_CARD, fg=COL_MUTED).pack(
            anchor="w", pady=(2, 6))

        tk.Frame(card, bg=COL_BORDER, height=1).pack(fill=tk.X, pady=4)

        # Stats
        self.lbl_fails = tk.Label(card, text="0",
                                   font=self.f_name, bg=BG_CARD, fg=COL_RUN)
        self.lbl_fails.pack()
        tk.Label(card, text="fails this cycle", font=self.f_sub,
                 bg=BG_CARD, fg=COL_MUTED).pack()

        self.lbl_rate = tk.Label(card, text="0.00%",
                                  font=self.f_val, bg=BG_CARD, fg=COL_TEXT)
        self.lbl_rate.pack()
        tk.Label(card, text="fail rate", font=self.f_sub,
                 bg=BG_CARD, fg=COL_MUTED).pack()

        self.lbl_last = tk.Label(card, text="last stop: —",
                                  font=self.f_sub, bg=BG_CARD, fg=COL_MUTED)
        self.lbl_last.pack(pady=(6, 0))

        self.lbl_blocked = tk.Label(card, text="",
                                     font=self.f_sub, bg=BG_CARD, fg=COL_BLOCK)
        self.lbl_blocked.pack(pady=(2, 0))

        self.lbl_last60 = tk.Label(card, text="",
                                    font=self.f_sub, bg=BG_CARD, fg=COL_WARN)
        self.lbl_last60.pack(pady=(1, 0))

        tk.Frame(card, bg=COL_BORDER, height=1).pack(fill=tk.X, pady=8)

        # Files + last update
        self.lbl_files = tk.Label(card, text="files today: 0",
                                   font=self.f_sub, bg=BG_CARD, fg=COL_MUTED)
        self.lbl_files.pack(anchor="w")
        self.lbl_time = tk.Label(card, text="",
                                  font=self.f_sub, bg=BG_CARD, fg=COL_MUTED)
        self.lbl_time.pack(anchor="w")

        # Refresh button
        tk.Button(self.root, text="⟳  Refresh Now",
                  command=self.refresh,
                  bg=BG_HEADER, fg=COL_WHITE, font=self.f_sub,
                  relief=tk.FLAT, padx=12, pady=6,
                  cursor="hand2").pack(pady=(0, 12))

        # Footer
        tk.Label(self.root,
                 text=f"Log: {log_dir()}  |  Warn≥{warn_at_fail()}  Block≥{block_at_fail()}",
                 font=self.f_sub, bg=BG_HEADER,
                 fg=COL_MUTED, pady=4).pack(fill=tk.X, side=tk.BOTTOM)

    # ── Connection ────────────────────────────────────────
    def _start_handshake(self):
        self._start_dot_animation()
        threading.Thread(target=self._run_handshake, daemon=True).start()

    def _run_handshake(self):
        ok = send_hello()
        self.root.after(0, lambda: self._on_handshake(ok))

    def _on_handshake(self, ok: bool):
        self._stop_dot_animation()
        self._conn = ok
        if ok:
            self.lbl_conn_dot.config(fg=COL_CONN)
            self.lbl_conn_status.config(
                text=f"Connected  •  {main_pc_ip()}:{main_pc_port()}",
                fg=COL_CONN)
        else:
            self.lbl_conn_dot.config(fg=COL_DISC)
            self.lbl_conn_status.config(
                text="Disconnected  •  Main PC unreachable",
                fg=COL_DISC)
        self._schedule_heartbeat()

    def _schedule_heartbeat(self):
        if self._hb_after_id:
            self.root.after_cancel(self._hb_after_id)
        self._hb_after_id = self.root.after(
            heartbeat_sec() * 1000, self._start_handshake)

    def _start_dot_animation(self):
        self._stop_dot_animation()
        self._animate_dots()

    def _animate_dots(self):
        dots = "." * (self._dot_count % 4)
        self.lbl_conn_status.config(
            text=f"Checking connection{dots}", fg=COL_CHECK)
        self.lbl_conn_dot.config(fg=COL_CHECK)
        self._dot_count += 1
        self._dot_anim_id = self.root.after(500, self._animate_dots)

    def _stop_dot_animation(self):
        if self._dot_anim_id:
            self.root.after_cancel(self._dot_anim_id)
            self._dot_anim_id = None

    # ── Refresh ───────────────────────────────────────────
    def refresh(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            stats = scan_and_check()
        except Exception as e:
            print(f"[ft_dashboard] Refresh error: {e}")
            stats = _empty_stats_dict()
        self.root.after(0, lambda: self._update_ui(stats))

    def _update_ui(self, stats: dict):
        self._last_stats = stats
        status = stats["status"]
        color  = _status_color(status)

        self.lbl_status.config(text=status, bg=color,
                                fg=BG_MAIN if status != "BLOCKED" else COL_WHITE)
        self.lbl_fails.config(text=str(stats["fails"]), fg=color)
        self.lbl_rate.config(text=f"{stats['rate']:.2f}%")
        self.lbl_last.config(text=f"last stop: {stats['last_stop']}")
        self.lbl_files.config(
            text=f"files today: {stats.get('files_today', 0)}")
        self.lbl_time.config(
            text=f"updated: {datetime.now().strftime('%H:%M:%S')}")

        if status == "BLOCKED":
            bmin = stats.get("blocked_since_min", 0)
            dur  = (f"{bmin//60}h {bmin%60:02d}m"
                    if bmin >= 60 else f"{bmin}min")
            self.lbl_blocked.config(text=f"⛔ BLOCKED {dur}", fg=COL_BLOCK)
            f60  = stats.get("fails_60", 0)
            r60  = stats.get("rate_60", 0.0)
            self.lbl_last60.config(
                text=f"last 60: {f60} fails  {r60:.1f}%", fg=COL_WARN)
        else:
            self.lbl_blocked.config(text="")
            self.lbl_last60.config(text="")

        # Card border color
        from ft_config_loader import ft_function_name as ffn
        self._after_id = self.root.after(refresh_ms(), self.refresh)

    # ── Config ────────────────────────────────────────────
    def _open_config(self):
        FTConfigEditor(self.root, on_save=self._on_config_saved)

    def _on_config_saved(self):
        self.root.title(f"FT Monitor — {ft_label()}")
        self._start_handshake()
        self.refresh()


def _empty_stats_dict() -> dict:
    return {
        "label": ft_label(), "status": "STOPPED",
        "total": 0, "fails": 0, "rate": 0.0,
        "fails_60": 0, "rate_60": 0.0,
        "last_stop": "—", "last_data": "—",
        "blocked_since_min": 0, "files_today": 0,
    }


# =========================================================
# Entry point
# =========================================================
if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("380x520")
    root.minsize(360, 480)
    FTDashboard(root)
    root.mainloop()
