"""
log_manager.py  —  Main PC  (SHARED)
Provides day-wise log file setup and auto-cleanup.

Features:
  - One log file per day: main_pc_popup_2026-06-01.log
  - Auto-deletes log files older than 7 days (configurable)
  - DailyFileHandler switches file at midnight automatically

Usage:
    from log_manager import get_logger, get_daily_logger, cleanup_old_logs
    logger = get_daily_logger("main_pc_popup")
"""

import os
import logging
import glob
from datetime import datetime, timedelta

from config_loader import get_config


def _log_dir() -> str:
    return get_config()["paths"]["log_dir"]

def _retention_days() -> int:
    return int(get_config().get("log_retention_days", 7))


def cleanup_old_logs(log_dir: str = None, retention_days: int = None) -> None:
    log_dir        = log_dir        or _log_dir()
    retention_days = retention_days or _retention_days()
    cutoff         = datetime.now() - timedelta(days=retention_days)

    if not os.path.isdir(log_dir):
        return

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
        print(f"[log_manager] Deleted {deleted} log file(s) older than {retention_days} days")


class DailyFileHandler(logging.Handler):
    """
    Logging handler that writes to a new file each day.
    Switches file automatically at midnight — no restart needed.
    """

    def __init__(self, name: str, log_dir: str = None):
        super().__init__()
        self.name_base     = name
        self.log_dir       = log_dir or _log_dir()
        self._current_date = None
        self._file_handler = None
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        self._open_today()

    def _open_today(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date:
            return
        if self._file_handler:
            self._file_handler.close()
        os.makedirs(self.log_dir, exist_ok=True)
        log_file = os.path.join(
            self.log_dir, f"{self.name_base}_{today}.log"
        )
        self._file_handler = logging.FileHandler(log_file, encoding="utf-8")
        self._file_handler.setFormatter(self.formatter)
        self._current_date = today

    def emit(self, record: logging.LogRecord) -> None:
        self._open_today()
        try:
            self._file_handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._file_handler:
            self._file_handler.close()
        super().close()


def get_logger(name: str, log_dir: str = None) -> logging.Logger:
    """Single-day logger — for short-lived processes."""
    log_dir  = log_dir or _log_dir()
    os.makedirs(log_dir, exist_ok=True)
    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"{name}_{today}.log")
    logger   = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh  = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh  = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def get_daily_logger(name: str, log_dir: str = None) -> logging.Logger:
    """
    Day-aware logger for long-running processes.
    Switches to new file at midnight automatically.
    """
    log_dir = log_dir or _log_dir()
    logger  = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.addHandler(DailyFileHandler(name, log_dir))
    sh = logging.StreamHandler()
    sh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(sh)
    return logger
