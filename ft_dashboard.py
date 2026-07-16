"""
ft_dashboard.py  —  FT PC
Small single-card dashboard showing this FT PC status.
Run:  python ft_dashboard.py
"""

import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox
from datetime import datetime

from ft_config_loader import (
    get_config, save_config,
    ft_id, ft_display_label, ft_rack, ft_function_label,
    warn_at_fail, block_at_fail, log_dir,
    refresh_ms, heartbeat_sec, main_pc_ip, main_pc_port,
    _FT_MAP
)
from ft_network_sender import send_hello
from ft_process_file   import scan_and_check, _empty_stats

# =========================================================
# Colors
# =========================================================
BG_MAIN   = "#0d1117"
BG_CARD   = "#161b22"
BG_HEADER = "#1c2128"
BG_POPUP  = "#161b22"
COL_RUN   = "#3fb950"
COL_WARN  = "#d29922"
COL_BLOCK = "#f85149"
COL_STOP  = "#6e7681"
COL_CHECK = "#58a6ff"
COL_CONN  = "#3fb950"
COL_DISC  = "#f85149"
COL_TEXT  = "#c9d1d9"
COL_MUTED = "#6e7681"
COL_WHITE = "#ffffff"
COL_BDR   = "#30363d"


def _status_color(status: str) -> str:
    return {
        "RUNNING": COL_RUN,
        "WARNING": COL_WARN,
        "BLOCKED": COL_BLOCK,
        "STOPPED": COL_STOP,
        "EMPTY":   COL_MUTED,
        "NO DIR":  COL_MUTED,
    }.get(status, COL_STOP)


# =========================================================
# Config Editor
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
                 text="Set ft_id once — identifies this PC permanently.",
                 font=f_note, bg=BG_POPUP,
                 fg=COL_MUTED).pack(pady=(0, 10))

        form = tk.Frame(self, bg=BG_POPUP, padx=24, pady=8)
        form.pack()

        cfg = get_config()
        self._vars = {}

        # ft_id dropdown
        tk.Label(form, text="FT ID", font=f_label,
                 bg=BG_POPUP, fg=COL_TEXT,
                 anchor="w", width=18).grid(
            row=0, column=0, sticky="w", pady=5)

        ft_ids = list(_FT_MAP.keys())
        self._ftid_var = tk.StringVar(master=self,
                                      value=cfg["ft"]["ft_id"])
        ft_menu = tk.OptionMenu(form, self._ftid_var, *ft_ids)
        ft_menu.config(bg=BG_HEADER, fg=COL_WHITE,
                       font=f_label, relief=tk.FLAT,
                       activebackground=BG_HEADER,
                       highlightthickness=0)
        ft_menu["menu"].config(bg=BG_HEADER, fg=COL_WHITE,
                               font=f_label)
        ft_menu.grid(row=0, column=1, sticky="w", padx=8)

        # Live preview
        self._preview = tk.Label(form, text="", font=f_note,
                                  bg=BG_POPUP, fg=COL_CHECK)
        self._preview.grid(row=1, column=0, columnspan=2,
                           sticky="w", pady=(0, 8))
        self._ftid_var.trace_add("write", self._update_preview)
        self._update_preview()

        # Text fields
        text_fields = [
            ("Main PC IP",       "main_ip",   cfg["network"]["main_pc_ip"]),
            ("Main PC Port",     "main_port", str(cfg["network"]["main_pc_port"])),
            ("Log Directory",    "log_dir",   cfg["paths"]["log_dir"]),
            ("Warn at fails ≥",  "warn",      str(cfg["thresholds"]["warn_at_fail"])),
            ("Block at fails ≥", "block",     str(cfg["thresholds"]["block_at_fails"])),
        ]
        for i, (label, key, default) in enumerate(text_fields):
            r = i + 2
            tk.Label(form, text=label, font=f_label,
                     bg=BG_POPUP, fg=COL_TEXT,
                     anchor="w", width=18).grid(
                row=r, column=0, sticky="w", pady=5)
            var = tk.StringVar(value=default)
            tk.Entry(form, textvariable=var, width=26,
                     font=f_label, bg=BG_HEADER, fg=COL_WHITE,
                     insertbackground=COL_WHITE,
                     relief=tk.FLAT).grid(
                row=r, column=1, padx=8, sticky="w")
            self._vars[key] = var

        # Buttons
        btn_row = tk.Frame(self, bg=BG_POPUP)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="Save",
                  command=self._save,
                  bg=COL_RUN, fg=BG_MAIN, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="Cancel",
                  command=self.destroy,
                  bg=BG_HEADER, fg=COL_TEXT, font=f_label,
                  relief=tk.FLAT, padx=16, pady=6,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)

        self.update_idletasks()
        px = parent.winfo_x() + \
             (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + \
             (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _update_preview(self, *_):
        fid = self._ftid_var.get()
        if fid in _FT_MAP:
            rack, fn, label = _FT_MAP[fid]
            rack_name = "Front Rack" if rack == "front" else "Rear Rack"
            self._preview.config(
                text=f"→ {rack_name} — {label}",
                fg=COL_CHECK)
        else:
            self._preview.config(
                text="→ Invalid FT ID", fg=COL_BLOCK)

    def _save(self):
        try:
            fid   = self._ftid_var.get().upper()
            ip    = self._vars["main_ip"].get().strip()
            port  = int(self._vars["main_port"].get())
            ldir  = self._vars["log_dir"].get().strip()
            warn  = int(self._vars["warn"].get())
            block = int(self._vars["block"].get())

            if fid not in _FT_MAP:
                raise ValueError(f"Invalid FT ID '{fid}'.")
            if not ip:
                raise ValueError("Main PC IP cannot be empty.")
            if warn >= block:
                raise ValueError("Warn must be less than Block.")
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e), parent=self)
            return

        cfg = get_config()
        cfg["ft"]["ft_id"]                  = fid
        cfg["network"]["main_pc_ip"]        = ip
        cfg["network"]["main_pc_port"]      = port
        cfg["paths"]["log_dir"]             = ldir
        cfg["thresholds"]["warn_at_fail"]   = warn
        cfg["thresholds"]["block_at_fails"] = block
        save_config(cfg)
        self.on_save()
        self.destroy()


# =========================================================
# Main FT Dashboard
# =========================================================
class FTDashboard:
    def __init__(self, root: tk.Tk):
        self.root        = root
        self._after_id   = None
        self._hb_id      = None
        self._conn       = False
        self._last_stats = _empty_stats()
        self._dot_count  = 0
        self._dot_id     = None

        self.f_title    = tkfont.Font(family="Consolas", size=13, weight="bold")
        self.f_name     = tkfont.Font(family="Consolas", size=12, weight="bold")
        self.f_status   = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_bold     = tkfont.Font(family="Consolas", size=9,  weight="bold")
        self.f_sub      = tkfont.Font(family="Consolas", size=9)
        self.f_note     = tkfont.Font(family="Consolas", size=8)

        self.root.configure(bg=BG_MAIN)
        self.root.resizable(False, False)
        self._rebuild_ui()
        self._start_handshake()
        self.refresh()

    # ── Full UI rebuild — called on init and after config save ──
    def _rebuild_ui(self):
        """Destroy and recreate all widgets so config changes reflect immediately."""
        for w in self.root.winfo_children():
            w.destroy()

        # Cancel any pending animation
        if self._dot_id:
            try: self.root.after_cancel(self._dot_id)
            except Exception: pass
            self._dot_id = None

        fid       = ft_id()
        label     = ft_display_label()
        rack_name = "Front Rack" if ft_rack() == "front" else "Rear Rack"
        func      = ft_function_label()

        self.root.title(f"FT Monitor — {label}")

        # ── Header ────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG_HEADER, pady=10)
        hdr.pack(fill=tk.X)

        tk.Label(hdr, text=f"FT Monitor  •  {label}",
                 font=self.f_title, bg=BG_HEADER,
                 fg=COL_WHITE).pack(side=tk.LEFT, padx=14)

        tk.Button(hdr, text="⚙  Config",
                  command=self._open_config,
                  bg="#21262d", fg=COL_TEXT,
                  font=self.f_note, relief=tk.FLAT,
                  padx=10, pady=4,
                  bd=1, highlightbackground=COL_BDR,
                  cursor="hand2").pack(side=tk.RIGHT, padx=10)

        # ── Connection bar ────────────────────────────────
        conn = tk.Frame(self.root, bg=BG_MAIN, pady=6)
        conn.pack(fill=tk.X, padx=14)

        crow = tk.Frame(conn, bg=BG_MAIN)
        crow.pack(fill=tk.X)

        self.lbl_dot = tk.Label(crow, text="●",
                                 font=self.f_status,
                                 bg=BG_MAIN, fg=COL_CHECK)
        self.lbl_dot.pack(side=tk.LEFT, padx=(0, 6))

        self.lbl_conn = tk.Label(crow, text="Checking...",
                                  font=self.f_bold,
                                  bg=BG_MAIN, fg=COL_CHECK)
        self.lbl_conn.pack(side=tk.LEFT)

        tk.Label(conn,
                 text=f"Main PC: {main_pc_ip()}:{main_pc_port()}",
                 font=self.f_note, bg=BG_MAIN,
                 fg=COL_MUTED).pack(anchor="w", padx=18)

        tk.Frame(self.root, bg=COL_BDR, height=1).pack(
            fill=tk.X, padx=14, pady=(4, 0))

        # ── Main card ─────────────────────────────────────
        card = tk.Frame(self.root, bg=BG_CARD,
                        padx=16, pady=12,
                        highlightbackground=COL_BDR,
                        highlightthickness=1)
        card.pack(fill=tk.X, padx=14, pady=10)

        # Card header: FT ID + status badge
        top = tk.Frame(card, bg=BG_CARD)
        top.pack(fill=tk.X, pady=(0, 2))

        tk.Label(top, text=fid,
                 font=self.f_name, bg=BG_CARD,
                 fg=COL_WHITE).pack(side=tk.LEFT)

        self.lbl_status = tk.Label(top, text="—",
                                    font=self.f_status,
                                    bg=COL_STOP, fg=COL_WHITE,
                                    padx=8, pady=3)
        self.lbl_status.pack(side=tk.RIGHT)

        # Mapping subtitle
        tk.Label(card,
                 text=f"→ {rack_name}   {func}",
                 font=self.f_note, bg=BG_CARD,
                 fg=COL_MUTED).pack(anchor="w", pady=(0, 8))

        tk.Frame(card, bg=COL_BDR, height=1).pack(
            fill=tk.X, pady=(0, 6))

        # Stat rows helper
        def _row(label_text, attr, fg=COL_TEXT, top_pad=2):
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill=tk.X, pady=(top_pad, 2))
            tk.Label(row, text=label_text,
                     font=self.f_bold,
                     bg=BG_CARD, fg=COL_MUTED,
                     width=11, anchor="w").pack(side=tk.LEFT)
            lbl = tk.Label(row, text="—",
                           font=self.f_bold,
                           bg=BG_CARD, fg=fg)
            lbl.pack(side=tk.LEFT)
            setattr(self, attr, lbl)

        _row("Fails:",     "lbl_fails",     COL_RUN)
        _row("Rate:",      "lbl_rate",      COL_TEXT)
        _row("Last 60:",   "lbl_fails60",   COL_TEXT)
        _row("Rate 60:",   "lbl_rate60",    COL_TEXT)

        tk.Frame(card, bg=COL_BDR, height=1).pack(
            fill=tk.X, pady=(6, 4))

        _row("Last Stop:", "lbl_last_stop", COL_MUTED)
        _row("Last Data:", "lbl_last_data", COL_MUTED)
        _row("Updated:",   "lbl_updated",   COL_MUTED)

        # Blocked banner
        self.lbl_blocked = tk.Label(card, text="",
                                     font=self.f_bold,
                                     bg=BG_CARD, fg=COL_BLOCK,
                                     anchor="w")
        self.lbl_blocked.pack(fill=tk.X, pady=(6, 0))

        # ── Refresh button ────────────────────────────────
        tk.Button(self.root,
                  text="⟳   Refresh Now",
                  command=self.refresh,
                  bg="#21262d", fg=COL_WHITE,
                  font=self.f_bold, relief=tk.FLAT,
                  padx=14, pady=6,
                  bd=1, highlightbackground=COL_BDR,
                  cursor="hand2").pack(pady=(0, 10))

        # ── Footer ────────────────────────────────────────
        tk.Label(self.root,
                 text=f"Log: {log_dir()}   Warn≥{warn_at_fail()}  Block≥{block_at_fail()}",
                 font=self.f_note, bg=BG_HEADER,
                 fg=COL_MUTED, pady=4).pack(
            fill=tk.X, side=tk.BOTTOM)

        # Restart dot animation after rebuild
        self._start_dots()

    # ── Update UI with latest stats ───────────────────────
    def _update_ui(self, stats: dict):
        self._last_stats = stats
        status = stats["status"]
        color  = _status_color(status)

        self.lbl_status.config(
            text=status, bg=color,
            fg=BG_MAIN if status in ("RUNNING", "WARNING", "STOPPED")
            else COL_WHITE)

        self.lbl_fails.config(
            text=str(stats["fails"]), fg=color)
        self.lbl_rate.config(
            text=f"{stats['rate']:.2f}%", fg=color)

        f60       = stats.get("fails_60", 0)
        r60       = stats.get("rate_60", 0.0)
        f60_color = (COL_BLOCK if f60 >= block_at_fail()
                     else COL_WARN  if f60 >= warn_at_fail()
                     else COL_RUN)
        self.lbl_fails60.config(text=str(f60), fg=f60_color)
        self.lbl_rate60.config(text=f"{r60:.2f}%", fg=f60_color)

        self.lbl_last_stop.config(text=stats.get("last_stop", "—"))
        self.lbl_last_data.config(text=stats.get("last_data", "—"))
        self.lbl_updated.config(
            text=datetime.now().strftime("%H:%M:%S"))

        if status == "BLOCKED":
            bmin = stats.get("blocked_min", 0)
            dur  = (f"{bmin//60}h {bmin%60:02d}m"
                    if bmin >= 60 else f"{bmin}min")
            self.lbl_blocked.config(
                text=f"⛔   BLOCKED   {dur}", fg=COL_BLOCK)
        else:
            self.lbl_blocked.config(text="")

        self._after_id = self.root.after(refresh_ms(), self.refresh)

    # ── Connection ────────────────────────────────────────
    def _start_handshake(self):
        self._start_dots()
        threading.Thread(target=self._run_hello, daemon=True).start()

    def _run_hello(self):
        ok = send_hello()
        self.root.after(0, lambda: self._on_hello(ok))

    def _on_hello(self, ok: bool):
        self._stop_dots()
        self._conn = ok
        try:
            if ok:
                self.lbl_dot.config(fg=COL_CONN)
                self.lbl_conn.config(
                    text=f"Connected  •  {main_pc_ip()}:{main_pc_port()}",
                    fg=COL_CONN)
            else:
                self.lbl_dot.config(fg=COL_DISC)
                self.lbl_conn.config(
                    text="Disconnected  •  Main PC unreachable",
                    fg=COL_DISC)
        except tk.TclError:
            pass   # widget may have been destroyed during rebuild
        if self._hb_id:
            self.root.after_cancel(self._hb_id)
        self._hb_id = self.root.after(
            heartbeat_sec() * 1000, self._start_handshake)

    def _start_dots(self):
        self._stop_dots()
        self._animate_dots()

    def _animate_dots(self):
        dots = "." * (self._dot_count % 4)
        try:
            self.lbl_conn.config(
                text=f"Checking{dots}", fg=COL_CHECK)
            self.lbl_dot.config(fg=COL_CHECK)
        except tk.TclError:
            return
        self._dot_count += 1
        self._dot_id = self.root.after(500, self._animate_dots)

    def _stop_dots(self):
        if self._dot_id:
            try: self.root.after_cancel(self._dot_id)
            except Exception: pass
            self._dot_id = None

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
            stats = _empty_stats()
        self.root.after(0, lambda: self._update_ui(stats))

    # ── Config ────────────────────────────────────────────
    def _open_config(self):
        FTConfigEditor(self.root, on_save=self._on_config_saved)

    def _on_config_saved(self):
        """Rebuild entire UI so new ft_id reflects everywhere instantly."""
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._rebuild_ui()
        self._start_handshake()
        self.refresh()


# =========================================================
# Entry point
# =========================================================
if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("420x560")
    root.minsize(380, 480)
    FTDashboard(root)
    root.mainloop()
