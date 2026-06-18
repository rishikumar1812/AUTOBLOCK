"""
log_manager.py  —  DL PC  (SHARED)
Provides day-wise log file setup and auto-cleanup.

Features:
  - One log file per day: process_files_2026-06-01.log
  - Auto-deletes log files older than 7 days (configurable)
  - Single get_logger() call replaces all FileHandler setup
  - Called at startup of each file — cleanup runs once per process

Usage:
    from log_manager import get_logger
    logger = get_logger("process_files")
"""

import os
import logging
import glob
from datetime import datetime, timedelta

from config_loader import get_config


# =========================================================
# Config accessors
# =========================================================
def _log_dir() -> str:
    return get_config()["paths"]["log_register_dir"]

def _retention_days() -> int:
    return int(get_config().get("log_retention_days", 7))


# =========================================================
# Auto-delete old log files
# Runs once per process startup — scans log_dir for any
# .log files older than retention_days and removes them.
# =========================================================
def cleanup_old_logs(log_dir: str = None, retention_days: int = None) -> None:
    log_dir       = log_dir       or _log_dir()
    retention_days = retention_days or _retention_days()
    cutoff        = datetime.now() - timedelta(days=retention_days)

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


# =========================================================
# Get a day-wise logger
#
# Creates log file named:  {name}_YYYY-MM-DD.log
# e.g. process_files_2026-06-01.log
#
# If logger already has handlers (already set up) it returns
# the existing one without adding duplicates.
# =========================================================
def get_logger(name: str, log_dir: str = None) -> logging.Logger:
    """
    Returns a configured logger writing to a day-wise file.

    Args:
        name:    base name  e.g. "process_files", "register_logger"
        log_dir: override log directory (uses config if not given)

    Returns:
        logging.Logger ready to use
    """
    log_dir = log_dir or _log_dir()
    os.makedirs(log_dir, exist_ok=True)

    today    = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"{name}_{today}.log")

    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler — day-wise file
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# =========================================================
# Day-aware FileHandler
# Automatically switches to a new file at midnight without
# restarting the process. Used by long-running processes
# like process_files.py that run all day.
# =========================================================
class DailyFileHandler(logging.Handler):
    """
    Logging handler that writes to a new file each day.
    Checks date on every emit — switches file at midnight.
    Old files are kept (cleanup_old_logs handles deletion).
    """

    def __init__(self, name: str, log_dir: str = None):
        super().__init__()
        self.name_base = name
        self.log_dir   = log_dir or _log_dir()
        self._current_date = None
        self._file_handler = None
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        self._open_today()

    def _open_today(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date:
            return  # already writing to today's file

        # Close old handler
        if self._file_handler:
            self._file_handler.close()

        os.makedirs(self.log_dir, exist_ok=True)
        log_file = os.path.join(
            self.log_dir, f"{self.name_base}_{today}.log"
        )
        self._file_handler  = logging.FileHandler(log_file, encoding="utf-8")
        self._file_handler.setFormatter(self.formatter)
        self._current_date  = today

    def emit(self, record: logging.LogRecord) -> None:
        # Check date on every log write — switches file at midnight
        self._open_today()
        try:
            self._file_handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._file_handler:
            self._file_handler.close()
        super().close()


# =========================================================
# get_daily_logger — uses DailyFileHandler for long-running
# processes that must survive past midnight
# =========================================================
def get_daily_logger(name: str, log_dir: str = None) -> logging.Logger:
    """
    Like get_logger() but uses DailyFileHandler so the file
    switches automatically at midnight without restart.
    Use this for process_files.py (runs all day via scheduler).
    Use get_logger() for dashboard and register_logger (short runs).
    """
    log_dir = log_dir or _log_dir()
    logger  = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Day-aware file handler
    logger.addHandler(DailyFileHandler(name, log_dir))

    # Console handler
    sh = logging.StreamHandler()
    sh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(sh)

    return logger
