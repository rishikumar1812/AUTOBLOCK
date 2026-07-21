"""
tray_utils.py  —  SHARED (copy to all dashboard folders)

Provides:
  1. SingleInstance   — mutex + named pipe IPC so only one EXE runs
  2. TrayIconManager  — pystray system tray icon with right-click menu
  3. hide_console()   — hide console window for background processes

Works on Windows only (uses ctypes Win32 + pystray win32 backend).
On non-Windows platforms all operations silently no-op so code runs
without error during development on Mac/Linux.

Usage in any dashboard:

    from tray_utils import SingleInstance, TrayIconManager, hide_console

    hide_console()          # hide console if running as EXE

    si = SingleInstance("MyApp")
    if not si.acquire():
        si.signal_restore()  # tell running instance to show itself
        sys.exit(0)

    # build root / window ...

    def on_show():  root.deiconify(); root.lift()
    def on_hide():  root.withdraw()
    def on_exit():  tray.stop(); root.destroy()

    tray = TrayIconManager(
        app_name="My Dashboard",
        on_show=on_show,
        on_hide=on_hide,
        on_exit=on_exit,
    )
    tray.start()   # starts pystray in daemon thread

    root.protocol("WM_DELETE_WINDOW", on_hide)
    si.start_listener(on_show)  # listen for restore requests from 2nd instance

    root.mainloop()
    si.release()
"""

import os
import sys
import threading
import logging

IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger(__name__)


# =========================================================
# 1. Hide console window
# =========================================================
def hide_console() -> None:
    """Hide the console window when running as a PyInstaller EXE."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception as e:
        logger.debug(f"[tray_utils] hide_console failed: {e}")


# =========================================================
# 2. Single Instance — Windows named mutex + named pipe IPC
# =========================================================
class SingleInstance:
    """
    Ensures only one instance of the application runs at a time.

    Uses a Windows named mutex for detection.
    Uses a named pipe for IPC: the second instance sends "RESTORE"
    to the first instance which then shows its window.

    Example:
        si = SingleInstance("FTDashboard")
        if not si.acquire():
            si.signal_restore()
            sys.exit(0)
        # ... run app ...
        si.start_listener(callback_to_show_window)
        # ... mainloop ...
        si.release()
    """

    def __init__(self, app_name: str):
        self.app_name  = app_name
        self._mutex    = None
        self._pipe_name = f"\\\\.\\pipe\\{app_name}_restore"
        self._listener  = None
        self._running   = False

    def acquire(self) -> bool:
        """
        Try to acquire the single-instance mutex.
        Returns True if this is the first instance.
        Returns False if another instance is already running.
        """
        if not IS_WINDOWS:
            return True   # non-Windows: always allow

        try:
            import ctypes
            self._mutex = ctypes.windll.kernel32.CreateMutexW(
                None, False, f"Global\\{self.app_name}_SingleInstance")
            last_error  = ctypes.windll.kernel32.GetLastError()
            # ERROR_ALREADY_EXISTS = 183
            if last_error == 183:
                logger.info(
                    f"[tray_utils] {self.app_name} already running")
                return False
            return True
        except Exception as e:
            logger.warning(f"[tray_utils] Mutex acquire failed: {e}")
            return True   # assume first instance on error

    def release(self) -> None:
        """Release the mutex on exit."""
        self._running = False
        if not IS_WINDOWS or self._mutex is None:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None
        except Exception:
            pass

    def signal_restore(self) -> None:
        """
        Called by the SECOND instance to tell the first to show itself.
        Connects to the named pipe and sends "RESTORE".
        """
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            # Open pipe with a short timeout — first instance may be starting up
            pipe = ctypes.windll.kernel32.CreateFileW(
                self._pipe_name,
                0x40000000,   # GENERIC_WRITE
                0, None,
                3,            # OPEN_EXISTING
                0, None)

            if pipe == ctypes.c_void_p(-1).value:
                logger.warning(
                    f"[tray_utils] Could not connect to "
                    f"{self.app_name} pipe — window may be starting up")
                # Fallback: try to bring window to front by title
                self._bring_to_front_by_title()
                return

            msg   = b"RESTORE"
            written = ctypes.c_ulong(0)
            ctypes.windll.kernel32.WriteFile(
                pipe, msg, len(msg),
                ctypes.byref(written), None)
            ctypes.windll.kernel32.CloseHandle(pipe)
            logger.info(f"[tray_utils] Sent RESTORE to {self.app_name}")
        except Exception as e:
            logger.warning(f"[tray_utils] signal_restore failed: {e}")
            self._bring_to_front_by_title()

    def _bring_to_front_by_title(self) -> None:
        """Fallback: find the existing window by partial title match."""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, None)
            # Walk all windows looking for one whose title contains app_name
            results = []

            def enum_callback(hwnd, _):
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    if self.app_name.lower() in buf.value.lower():
                        results.append(hwnd)
                return True

            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            ctypes.windll.user32.EnumWindows(
                EnumWindowsProc(enum_callback), 0)

            for hwnd in results:
                # SW_RESTORE = 9, SW_SHOW = 5
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as e:
            logger.debug(f"[tray_utils] bring_to_front failed: {e}")

    def start_listener(self, on_restore_callback) -> None:
        """
        Start a named pipe server in a daemon thread.
        When "RESTORE" arrives, calls on_restore_callback().
        Must be called after acquire() returns True.
        """
        if not IS_WINDOWS:
            return

        self._running = True

        def _listen():
            import ctypes
            PIPE_ACCESS_INBOUND    = 0x00000001
            PIPE_TYPE_BYTE         = 0x00000000
            PIPE_WAIT              = 0x00000000
            INVALID_HANDLE_VALUE   = ctypes.c_void_p(-1).value

            while self._running:
                try:
                    pipe = ctypes.windll.kernel32.CreateNamedPipeW(
                        self._pipe_name,
                        PIPE_ACCESS_INBOUND,
                        PIPE_TYPE_BYTE | PIPE_WAIT,
                        1,      # max instances
                        512, 512,
                        0, None)

                    if pipe == INVALID_HANDLE_VALUE:
                        import time; time.sleep(1)
                        continue

                    # Blocks here until a client connects
                    ctypes.windll.kernel32.ConnectNamedPipe(pipe, None)

                    buf   = ctypes.create_string_buffer(512)
                    read  = ctypes.c_ulong(0)
                    ctypes.windll.kernel32.ReadFile(
                        pipe, buf, 512,
                        ctypes.byref(read), None)
                    ctypes.windll.kernel32.DisconnectNamedPipe(pipe)
                    ctypes.windll.kernel32.CloseHandle(pipe)

                    msg = buf.raw[:read.value].decode("utf-8", errors="ignore")
                    if msg.strip() == "RESTORE":
                        logger.info(
                            f"[tray_utils] RESTORE received — "
                            f"bringing {self.app_name} to front")
                        try:
                            on_restore_callback()
                        except Exception as e:
                            logger.warning(
                                f"[tray_utils] on_restore callback: {e}")
                except Exception as e:
                    logger.debug(f"[tray_utils] pipe listener error: {e}")
                    import time; time.sleep(1)

        self._listener = threading.Thread(
            target=_listen, daemon=True, name=f"{self.app_name}_PipeListener")
        self._listener.start()


# =========================================================
# 3. Tray Icon Manager — pystray based
# =========================================================
class TrayIconManager:
    """
    System tray icon with right-click menu.

    Menu:
      Show Dashboard   — calls on_show()
      Hide Dashboard   — calls on_hide()
      ─────────────
      Exit             — calls on_exit()

    Double-click tray icon → on_show()
    First minimize → balloon notification (once only)

    Requires: pip install pystray pillow
    """

    def __init__(self, app_name: str, icon_path: str = None,
                 on_show=None, on_hide=None, on_exit=None):
        self.app_name   = app_name
        self.icon_path  = icon_path
        self.on_show    = on_show or (lambda: None)
        self.on_hide    = on_hide or (lambda: None)
        self.on_exit    = on_exit or (lambda: None)
        self._icon      = None
        self._shown_balloon = False

    def _make_icon_image(self):
        """
        Create a simple colored icon if no icon file provided.
        Uses PIL to draw a small square with the app initial.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            size = 64
            img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            # Dark background circle
            draw.ellipse([2, 2, size-2, size-2], fill=(26, 27, 38, 255))
            # Initial letter
            initial = self.app_name[0].upper() if self.app_name else "D"
            try:
                font = ImageFont.truetype("arial.ttf", 36)
            except Exception:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), initial, font=font)
            tw   = bbox[2] - bbox[0]
            th   = bbox[3] - bbox[1]
            draw.text(
                ((size - tw) // 2, (size - th) // 2),
                initial, fill=(201, 209, 217, 255), font=font)
            return img
        except Exception as e:
            logger.warning(f"[tray_utils] Icon creation failed: {e}")
            return None

    def _load_icon(self):
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                from PIL import Image
                return Image.open(self.icon_path)
            except Exception:
                pass
        return self._make_icon_image()

    def start(self) -> None:
        """Start the tray icon in a daemon thread."""
        if not IS_WINDOWS:
            return

        try:
            import pystray
            from pystray import MenuItem as Item, Menu

            img = self._load_icon()
            if img is None:
                logger.warning("[tray_utils] No icon — tray not started")
                return

            def _show(icon, item):
                self.on_show()

            def _hide(icon, item):
                self.on_hide()

            def _exit(icon, item):
                icon.stop()
                self.on_exit()

            def _double_click(icon):
                self.on_show()

            menu = Menu(
                Item("Show Dashboard", _show, default=True),
                Item("Hide Dashboard", _hide),
                Menu.SEPARATOR,
                Item("Exit",           _exit),
            )

            self._icon = pystray.Icon(
                self.app_name,
                img,
                self.app_name,
                menu)

            # Double-click → show
            self._icon.on_activate = lambda icon: self.on_show()

            t = threading.Thread(
                target=self._icon.run,
                daemon=True,
                name=f"{self.app_name}_TrayIcon")
            t.start()
            logger.info(f"[tray_utils] Tray icon started for {self.app_name}")

        except ImportError:
            logger.warning(
                "[tray_utils] pystray not installed — no tray icon. "
                "Run: pip install pystray pillow")
        except Exception as e:
            logger.warning(f"[tray_utils] Tray start failed: {e}")

    def stop(self) -> None:
        """Stop the tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def show_balloon(self, title: str, message: str,
                     once_only: bool = True) -> None:
        """Show a balloon notification. If once_only=True, shows only once."""
        if once_only and self._shown_balloon:
            return
        if not IS_WINDOWS or self._icon is None:
            return
        try:
            self._icon.notify(message, title)
            if once_only:
                self._shown_balloon = True
        except Exception as e:
            logger.debug(f"[tray_utils] Balloon failed: {e}")

    def update_tooltip(self, text: str) -> None:
        """Update the tray icon tooltip text."""
        if self._icon:
            try:
                self._icon.title = text
            except Exception:
                pass
