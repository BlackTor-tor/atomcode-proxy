"""系统托盘模块：提供无控制台窗口的托盘图标交互。"""
from __future__ import annotations

import logging
import os
import sys
import webbrowser
from typing import Callable

log = logging.getLogger("atomcode_proxy.tray")


def _create_tray_icon_image():
    """使用 Pillow 生成一个简单的绿色圆形图标。"""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 绘制绿色圆形
    draw.ellipse([4, 4, size - 4, size - 4], fill="#22c55e", outline="#16a34a", width=2)
    # 在中间绘制白色 "A" 字母简化为白色圆点
    draw.ellipse([size // 2 - 8, size // 2 - 8, size // 2 + 8, size // 2 + 8], fill="white")
    return img


class SystemTray:
    """Windows 系统托盘图标管理器。"""

    def __init__(self, host: str, port: int, log_path: str | None = None):
        self.host = host
        self.port = port
        self.log_path = log_path
        self._icon = None
        self._on_stop: Callable | None = None

    def run(self, callback_on_stop: Callable) -> None:
        """启动系统托盘（阻塞主线程）。

        如果 pystray 初始化失败，回退到控制台模式。
        """
        self._on_stop = callback_on_stop

        try:
            import pystray
            from pystray import MenuItem as Item
        except ImportError:
            log.warning("pystray 未安装，回退到控制台模式")
            self._fallback_console_mode()
            return

        try:
            icon_image = _create_tray_icon_image()

            menu_items = [
                Item("打开状态页面", self._open_status_page),
                Item("显示日志", self._open_log_file),
                pystray.Menu.SEPARATOR,
                Item("退出", self._quit),
            ]

            self._icon = pystray.Icon(
                name="atomcode-proxy",
                icon=icon_image,
                title=f"atomcode-proxy 运行中 - {self.host}:{self.port}",
                menu=pystray.Menu(*menu_items),
            )

            log.info("系统托盘已启动")
            self._icon.run()

        except Exception as exc:
            log.warning("系统托盘初始化失败: %s，回退到控制台模式", exc)
            self._fallback_console_mode()

    def _open_status_page(self, icon=None, item=None) -> None:
        """打开浏览器访问状态页面。"""
        url = f"http://{self.host}:{self.port}/"
        log.info("打开状态页面: %s", url)
        webbrowser.open(url)

    def _open_log_file(self, icon=None, item=None) -> None:
        """用系统默认程序打开日志文件。"""
        if self.log_path and os.path.isfile(self.log_path):
            log.info("打开日志文件: %s", self.log_path)
            if sys.platform == "win32":
                os.startfile(self.log_path)
            else:
                log.warning("显示日志功能仅支持 Windows")
        else:
            log.warning("日志文件不存在: %s", self.log_path)

    def _quit(self, icon=None, item=None) -> None:
        """退出程序。"""
        log.info("用户点击退出")
        if self._icon:
            self._icon.stop()
        if self._on_stop:
            self._on_stop()

    def _fallback_console_mode(self) -> None:
        """控制台模式降级方案。"""
        print("=" * 56)
        print(f"  atomcode-proxy 运行中 - http://{self.host}:{self.port}")
        print("  按 Ctrl+C 停止服务")
        print("=" * 56)
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if self._on_stop:
                self._on_stop()
