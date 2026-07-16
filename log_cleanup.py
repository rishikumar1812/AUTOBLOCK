"""
log_cleanup.py  —  SHARED
Copy to DL PC, Main PC, and FT PC folders — identical for all three.

Provides:
  1. cleanup_old_logs(log_dir, retention_days) — delete old *.log files
  2. DailyFileHandler — a logging.Handler that automatically switches
     to a new day-wise file at midnight, WITHOUT restarting the process.

Use DailyFileHandler instead of logging.FileHandler in any script
that runs 24/7 (ft_process_file.py, main_pc_popup.py, process_file.py).
The date in the filename updates itself every midnight — no restart needed.
"""

import os
import glob
import logging
from datetime import datetime, timedelta


def cleanup_old_logs(log_dir: str, retention_days: int = 7) -> None:
    """
    Deletes any *.log file in log_dir whose last-modified time
    is older than retention_days. Call this once at startup AND
    it is also called automatically every time DailyFileHandler
    rolls over to a new day, so 24/7 processes stay clean too.
    """
    if not os.path.isdir(log_dir):
        return

    cutoff  = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for fpath in glob.glob(os.path.join(log_dir, "*.log")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                os.remove(fpath)
                deleted += 1
        except Exception:
            pass

    if deleted:
        print(f"[log_cleanup] Deleted {deleted} log file(s) older than "
              f"{retention_days} days from {log_dir}")


class DailyFileHandler(logging.Handler):
    """
    Drop-in replacement for logging.FileHandler that writes to a
    NEW file every day automatically — no process restart needed.

    Filename pattern:  {name_base}_YYYY-MM-DD.log
    e.g. ft_process_F1_2026-06-21.log → ft_process_F1_2026-06-22.log

    On every log call it checks "has the date changed since I opened
    my current file?" If yes: close old file, run cleanup_old_logs(),
    open a new file for today.
    """

    def __init__(self, name_base: str, log_dir: str,
                 retention_days: int = 7):
        super().__init__()
        self.name_base      = name_base
        self.log_dir        = log_dir
        self.retention_days = retention_days
        self._current_date  = None
        self._file_handler  = None
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        self._roll_if_needed()

    def _roll_if_needed(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date:
            return   # still the same day — nothing to do

        # Date changed (or first call) — close old file, open new one
        if self._file_handler is not None:
            self._file_handler.close()

        os.makedirs(self.log_dir, exist_ok=True)
        log_file = os.path.join(
            self.log_dir, f"{self.name_base}_{today}.log")
        self._file_handler = logging.FileHandler(
            log_file, encoding="utf-8")
        self._file_handler.setFormatter(self.formatter)
        self._current_date = today

        # Run cleanup every time we roll to a new day
        cleanup_old_logs(self.log_dir, self.retention_days)

    def emit(self, record: logging.LogRecord) -> None:
        # Checked on EVERY log line — cheap string compare.
        self._roll_if_needed()
        try:
            self._file_handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._file_handler is not None:
            self._file_handler.close()
        super().close()