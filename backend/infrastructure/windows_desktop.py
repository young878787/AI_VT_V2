"""Windows 桌面的單次唯讀操作；不輪詢、不訂閱、不保存畫面。"""
import asyncio
import ctypes
import io
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from domain.runtime_context import MAX_OPEN_APPS, result


class WindowsDesktop:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
        if self.user32 is None:
            return
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
        for dll, name, args, returns in (
            (self.user32, "GetForegroundWindow", [], wintypes.HWND),
            (self.user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user32, "EnumWindows", [self.callback_type, ctypes.c_ssize_t], wintypes.BOOL),
            (self.user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "IsIconic", [wintypes.HWND], wintypes.BOOL),
            (self.user32, "GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
            (self.user32, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user32, "OpenInputDesktop", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.user32, "CloseDesktop", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel32, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD,
              wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, returns

    def _available(self) -> dict | None:
        if self.user32 is None:
            return result("unsupported", reason="windows_required")
        desktop = self.user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return result("unavailable", reason="desktop_unavailable")
        self.user32.CloseDesktop(desktop)
        return None

    def _app(self, window) -> str | None:
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(window, ctypes.byref(pid)):
            return None
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return os.path.basename(buffer.value)[:80]
            return None
        finally:
            self.kernel32.CloseHandle(handle)

    def snapshot(self, include_title=False) -> dict:
        unavailable = self._available()
        if unavailable:
            return unavailable
        window = self.user32.GetForegroundWindow()
        app = self._app(window) if window else None
        foreground = {"status": "ok" if app else "unavailable", "app": app}
        if not app:
            foreground["reason"] = "no_foreground" if not window else "access_denied"
        if include_title and window and app:
            title = ctypes.create_unicode_buffer(161)
            self.user32.GetWindowTextW(window, title, len(title))
            foreground["title"] = title.value
        if window != self.user32.GetForegroundWindow() or (app and app != self._app(window)):
            foreground = {"status": "unavailable", "app": None, "reason": "changed_during_capture"}
        apps = set()

        @self.callback_type
        def visit(handle, _param):
            if self.user32.IsWindowVisible(handle) and not self.user32.GetWindowLongPtrW(handle, -20) & 0x80:
                name = self._app(handle)
                if name:
                    apps.add(name)
            return True

        if self.user32.EnumWindows(visit, 0):
            ordered = sorted(apps, key=lambda name: (name != foreground.get("app"), name.casefold()))
            opened = {"status": "ok", "apps": ordered[:MAX_OPEN_APPS], "truncated": len(ordered) > MAX_OPEN_APPS}
        else:
            opened = {"status": "error", "apps": [], "truncated": False, "reason": "enumeration_failed"}
        return result("ok", {"foreground": foreground, "open_apps": opened})

    def screenshot(self, target="foreground") -> dict:
        unavailable = self._available()
        if unavailable:
            return unavailable
        from PIL import ImageGrab
        window = self.user32.GetForegroundWindow() if target == "foreground" else None
        if target == "foreground" and (not window or self.user32.IsIconic(window)):
            return result("unavailable", reason="target_unavailable")
        image = ImageGrab.grab(window=window) if window else ImageGrab.grab(all_screens=True)
        if window and window != self.user32.GetForegroundWindow():
            return result("unavailable", reason="changed_during_capture")
        image = image.convert("RGB")
        if not image.width or not image.height or all(high == 0 for _low, high in image.getextrema()):
            return result("unavailable", reason="empty_capture")
        image.thumbnail((1600, 1600))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        payload = buffer.getvalue()
        if len(payload) > 2 * 1024 * 1024:
            return result("error", reason="image_too_large")
        receipt = result("ok", {"target": target, "width": image.width, "height": image.height,
                                "mime_type": "image/jpeg"})
        receipt["image"] = payload
        return receipt

    def computer_status(self) -> dict:
        if self.user32 is None:
            return result("unsupported", reason="windows_required")
        import psutil
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        battery = psutil.sensors_battery()
        return result("ok", {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram": {"used_bytes": memory.used, "total_bytes": memory.total},
            "system_disk": {"free_bytes": disk.free, "total_bytes": disk.total},
            "battery": None if battery is None else {"percent": battery.percent, "plugged_in": battery.power_plugged},
        })


class DesktopReader:
    """一個在途原生工作；取消／逾時只丟棄結果，不累積 thread。"""
    def __init__(self, desktop=None):
        self.desktop = desktop or WindowsDesktop()
        self._gate = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vt-desktop")

    async def read(self, operation: str, *, timeout: float = 0.3, **kwargs) -> dict:
        if not self._gate.acquire(blocking=False):
            return result("unavailable", reason="source_busy")
        try:
            future = self._executor.submit(self._invoke, operation, kwargs)
        except BaseException:
            self._gate.release()
            raise
        future.add_done_callback(lambda _future: self._gate.release())
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout)
        except TimeoutError:
            return result("unavailable", reason="timeout")

    def _invoke(self, operation, kwargs):
        try:
            return getattr(self.desktop, operation)(**kwargs)
        except Exception:
            return result("error", reason="desktop_read_failed")

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
