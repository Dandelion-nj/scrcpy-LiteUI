"""快投 —— scrcpy 图形化启动器（入口）。

本文件只做组装：申请单实例互斥、起本地 HTTP 服务、创建 pywebview 窗口、
接好托盘与退出收尾。具体实现按职责放在 kuaitou/ 包里：

    storage.py   路径常量 + ADS 存储 / 配置 / 日志
    device.py    adb / scrcpy 封装、设备状态、投屏、子进程管理
    discover.py  三级设备发现（局域网 / mDNS / 端口扫描）
    apps.py      应用列表扫描 + 图标库匹配
    system.py    扫码配对、诊断、托盘、开机自启
    web.py       HTTP 路由与 js_api
"""
import ctypes
import http.server
import os
import sys
import threading

import webview

from kuaitou import device, system, winbar
from kuaitou.storage import load_config

# 入口是组装点，直接引用各模块的私有名接线（对外暴露反而多一层无意义的包装）
from kuaitou.system import (
    _open_webview2_download,
    _report_fatal,
    _show_message,
    _tray_enabled,
    _tray_start,
    _webview2_available,
)
from kuaitou.web import Api, Handler

# ---------- 单实例保护 ----------
# 两个「快投」同时跑，会各自拉一份 adb、各自往 exe 的数据流里写配置和日志（互相覆盖），
# 还会抢同一块虚拟屏。所以同一时间只允许一个实例，重复启动就把已有窗口唤到前台。
_MUTEX_HANDLE = None
_MUTEX_NAME = "Local\\KuaitouSingleInstance"

def _focus_existing_window():
    """唤起已运行实例的主窗口。它可能正收在托盘里，所以用 SW_RESTORE 而不是 SW_SHOW。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "快投")
        if not hwnd:
            return False
        user32.AllowSetForegroundWindow(0xFFFFFFFF)   # ASFW_ANY：否则只能让它在任务栏闪一下
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False

def _acquire_single_instance():
    """抢占单实例互斥体。已经有实例在跑时返回 False。"""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        # ERROR_ALREADY_EXISTS：互斥体早就存在，说明另一个实例正在运行
        return ctypes.get_last_error() != 183
    except Exception:
        return True     # 互斥体本身出问题不该导致应用打不开

def _center_of_screen(w, h):
    """算出主窗口居中时的左上角坐标（拿不到屏幕尺寸就返回 (None, None)，交给系统默认摆放）。"""
    try:
        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if sw > 0 and sh > 0:
            return max(0, (sw - w) // 2), max(0, (sh - h) // 2)
    except Exception:
        pass
    return None, None

def _run():

    silent = "--silent" in sys.argv        # 开机自启：静默启动，只驻托盘不弹主界面

    # 已经在运行就直接唤起旧窗口后退出，别再拉一份 adb / 再写一份配置
    if not _acquire_single_instance():
        if not silent and not _focus_existing_window():
            _show_message("快投已经在运行了。", 0x40)
        return

    # 首次调用 adb 之前记下 adb 服务是否已在运行，退出时只关我们自己拉起来的那个
    device.note_adb_server_before_start()
    # 投屏窗口右侧功能栏：winbar 只画按钮，动作交给 device 去发 adb。
    # 在这里接线是因为 winbar 不能反向 import device（device 已经 import 了 winbar）。
    winbar.set_action_handler(device.winbar_action)

    if not _webview2_available():
        if _show_message("缺少 Microsoft Edge WebView2 运行时，界面无法显示。\n\n"
                         "点“是”打开官方下载页，安装后重新运行快投即可。"):
            _open_webview2_download()
        return

    # 端口交给系统分配：固定端口在别人的电脑上可能被其它程序占用
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # 主窗口出现在屏幕中间：pywebview 默认按系统给的默认位置放，第一次打开会在左上角附近
    win_w, win_h = 950, 850
    win_x, win_y = _center_of_screen(win_w, win_h)
    window = webview.create_window(
        '快投',
        f'http://127.0.0.1:{port}',
        width=win_w,
        height=win_h,
        x=win_x,
        y=win_y,
        min_size=(700, 600),
        hidden=silent,          # 静默启动时不显示主界面（托盘右键可随时打开）
        js_api=Api()            # 暴露原生「另存为」对话框给前端
    )
    system.set_window(window)
    # 标题栏跟界面主题同色，并去掉图标与标题文字（类似无边框）；窗口要等一会儿才出来，
    # 这里塞后台线程自己等，不阻塞启动。切换主题时由 /api/config 再调一次。
    winbar.style_main_window(load_config().get("theme", "light"))

    def on_closing():
        # 开了"缩小到托盘"（或本次是静默自启）且不是从托盘选的"退出应用"：点关闭只把
        # 窗口藏起来，应用继续在托盘待命（返回 False 取消这次关闭）。
        if (_tray_enabled() or silent) and not system.tray_exiting() and system.tray_ready():
            try:
                window.hide()
            except Exception:
                pass
            return False
        return True

    def on_closed():
        # 真正退出：收尾所有相关进程后硬退出。
        # 扫描用的线程池是非守护线程，解释器退出时会等它们跑完（最长 20 多秒），
        # 只靠 return 会出现"窗口关了、进程还在"的情况。
        system.tray_stop()
        device.shutdown_all()
        os._exit(0)

    window.events.closing += on_closing
    window.events.closed += on_closed
    if _tray_enabled() or silent:
        _tray_start()
        # 静默启动时如果托盘起不来（pystray 缺失等），窗口必须露出来，否则应用"消失"了
        if silent and not system.tray_ready():
            try:
                window.show()
            except Exception:
                pass
    try:
        webview.start(debug=False)
    finally:
        system.tray_stop()
        device.shutdown_all()
        os._exit(0)

if __name__ == '__main__':
    try:
        _run()
    except Exception as e:
        _report_fatal(e)
    os._exit(0)
