"""投屏窗口的增强：主窗口与投屏窗口的标题栏配色 + 投屏窗口右侧的功能栏。

分两块：

**主窗口标题栏染色**（`style_main_window`）：用 DWM 把主界面的标题栏染成跟主题同色，
并去掉图标与标题文字（看起来接近无边框）。这里是「重活只做一次」的调度：
「去图标 + 加无图标标记」要重算窗口边框、会把整块窗口连网页一起重画，所以只在窗口
第一次出现时做，之后换主题只改 DWM 的几个颜色。

**投屏窗口右侧功能栏**（`attach`）：在 scrcpy 窗口上叠一条竖排按钮——
置顶、音量加减、强制横屏，外加（仅镜像桌面）返回 / 桌面 / 多任务 / 通知栏 / 控制中心。
鼠标扫到窗口右边缘才展开，平时只留贴边一条细条，基本不挡画面。

叠加窗口用色键透明（WS_EX_LAYERED + LWA_COLORKEY）：底色画成键色，那块地方既不显示也
点不到，鼠标会穿过去落到下面的画面上——画面右键（scrcpy 的返回）之类的操作照样能用，
我们只吃自己按钮上的点击。代价是**细条那一片收不到鼠标消息**，所以「鼠标扫过来就展开」
只能靠定时器轮询 GetCursorPos 自己判，不能等 WM_MOUSEMOVE。

功能栏的按钮分两类：置顶是纯窗口操作，留在本模块；其余都要发 adb 命令，而 device 已经
import 了本模块（反过来会成环），所以由入口 launcher_server.py 用 `set_action_handler`
把 device.winbar_action 注入进来。

线程与 DPI：整块逻辑跑在自己的线程上，进线程先声明 PER_MONITOR_AWARE_V2——主进程不是
DPI 感知的，沿用主线程的坐标会被系统按缩放拉伸。该线程内一律物理像素。
"""


import ctypes
import math
import os
import shutil
import tempfile
import threading
import time
from ctypes import wintypes

from .storage import LAUNCH_LOG_STREAM, RES_DIR, save_config, storage_write

# ---------- 尺寸 / 配色（逻辑像素，实际按 DPI 缩放）----------
_GLYPH = 18                 # 图标本体的边长


def _rgb(r, g, b):
    """COLORREF 是 0x00BBGGRR，不是网页那套 RRGGBB。"""
    return r | (g << 8) | (b << 16)

_BG = _rgb(0x1a, 0x1d, 0x24)        # --card：与主界面一致的标题栏底色
_HOVER = _rgb(0x23, 0x27, 0x30)     # --card-hover：按钮悬停底色
_DIM = _rgb(0x8b, 0x90, 0xa0)       # --text-dim：按钮常态
_TEXT = _rgb(0xf5, 0xf6, 0xf8)      # --text：按钮悬停
_ACCENT = _rgb(0x3b, 0x82, 0xf6)    # --accent：置顶生效
_LINE = _rgb(0x2b, 0x2f, 0x3a)      # --border：标题栏下沿分隔线

# 色键：画成这个颜色的地方既透明又鼠标穿透。选洋红是因为它绝不会出现在我们的配色里。
_KEY = 0x00FF00FF

# ---------- Win32 常量 ----------
_WS_POPUP = 0x80000000
_WS_EX_TOOLWINDOW, _WS_EX_NOACTIVATE = 0x00000080, 0x08000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_DLGMODALFRAME = 0x00000001
_LWA_COLORKEY = 0x00000001
_SWP_NOSIZE, _SWP_NOMOVE = 0x0001, 0x0002
_SWP_NOZORDER, _SWP_NOACTIVATE = 0x0004, 0x0010
_SWP_FRAMECHANGED = 0x0020
_GWL_EXSTYLE = -20
_HWND_TOPMOST, _HWND_NOTOPMOST = -1, -2
_SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
_WM_PAINT, _WM_TIMER, _WM_CLOSE = 0x000F, 0x0113, 0x0010
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_WM_SETCURSOR, _WM_ERASEBKGND, _WM_NCDESTROY = 0x0020, 0x0014, 0x0082
_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x0010
_IDC_ARROW = 32512
_WINDING = 2

_TIMER_ID = 1
_FOLLOW_MS = 40             # 跟随画面窗口的间隔：跟手，又不至于空转烧 CPU

# DWM 属性编号
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

_CLASS_NAME = "KuaituiWinBar"
_H = ctypes.c_void_p        # 一切句柄都按指针走
_LR = ctypes.c_ssize_t      # LRESULT / LONG_PTR
_POINT = wintypes.POINT
_log_lock = threading.Lock()


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", _H),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", _H),
                ("hIcon", _H),
                ("hCursor", _H),
                ("hbrBackground", _H),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", _H), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", _H), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


# 函数原型必须显式声明：句柄在 64 位上是指针，不声明 argtypes 的话 ctypes 会按 32 位
# C int 传参，句柄被截断，GDI 调用会悄无声息地全部失败（或画到别的地方去）。
def _decl(fn, restype, argtypes):
    fn.restype = restype
    fn.argtypes = argtypes


_api_ready = False
_api_lock = threading.Lock()
_user32 = _gdi32 = _kernel32 = _dwmapi = None
_wndproc = None             # 窗口过程（4 参：hwnd/msg/wparam/lparam）
_enumproc = None            # EnumWindows 回调（2 参），和窗口过程不是一回事，别混用


def _ensure_api():
    global _api_ready, _user32, _gdi32, _kernel32, _dwmapi, _wndproc, _enumproc
    if _api_ready:
        return
    with _api_lock:
        if _api_ready:
            return
        u, g, k = (ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32)
        U, W = wintypes.UINT, wintypes.LPCWSTR
        C = ctypes.c_int
        for name, res, args in (
                ("GetWindowRect", wintypes.BOOL, (_H, _H)),
                ("GetClientRect", wintypes.BOOL, (_H, _H)),
                ("ClientToScreen", wintypes.BOOL, (_H, _H)),
                ("GetWindowLongW", ctypes.c_long, (_H, ctypes.c_int)),
                ("SetWindowLongW", ctypes.c_long, (_H, ctypes.c_int, ctypes.c_long)),
                ("FindWindowW", _H, (wintypes.LPCWSTR, wintypes.LPCWSTR)),
                ("GetCursorPos", wintypes.BOOL, (_H,)),
                ("GetWindowThreadProcessId", wintypes.DWORD, (_H, _H)),
                ("EnumWindows", wintypes.BOOL, (_H, wintypes.LPARAM)),
                ("GetClassNameW", C, (_H, wintypes.LPWSTR, C)),
                ("IsWindow", wintypes.BOOL, (_H,)),
                ("IsIconic", wintypes.BOOL, (_H,)),
                ("IsWindowVisible", wintypes.BOOL, (_H,)),
                ("SetWindowPos", wintypes.BOOL, (_H, _H, C, C, C, C, U)),
                ("ShowWindow", wintypes.BOOL, (_H, C)),
                ("DestroyWindow", wintypes.BOOL, (_H,)),
                ("SetCapture", _H, (_H,)),
                ("ReleaseCapture", wintypes.BOOL, ()),
                ("SetLayeredWindowAttributes", wintypes.BOOL,
                 (_H, wintypes.DWORD, ctypes.c_byte, wintypes.DWORD)),
                ("InvalidateRect", wintypes.BOOL, (_H, _H, wintypes.BOOL)),
                ("SetTimer", _H, (_H, _H, U, _H)),
                ("KillTimer", wintypes.BOOL, (_H, _H)),
                ("PostMessageW", wintypes.BOOL, (_H, U, wintypes.WPARAM, wintypes.LPARAM)),
                ("SendMessageW", _LR, (_H, U, wintypes.WPARAM, wintypes.LPARAM)),
                ("PostQuitMessage", None, (C,)),
                ("GetMessageW", wintypes.BOOL, (_H, _H, U, U)),
                ("TranslateMessage", wintypes.BOOL, (_H,)),
                ("DispatchMessageW", _LR, (_H,)),
                ("DefWindowProcW", _LR, (_H, U, wintypes.WPARAM, wintypes.LPARAM)),
                ("RegisterClassW", wintypes.WORD, (_H,)),
                ("CreateWindowExW", _H, (wintypes.DWORD, W, W, wintypes.DWORD,
                                         C, C, C, C, _H, _H, _H, _H)),
                ("BeginPaint", _H, (_H, _H)),
                ("EndPaint", wintypes.BOOL, (_H, _H)),
                ("LoadImageW", _H, (_H, W, U, C, C, U)),
                ("LoadCursorW", _H, (_H, _H)),
                ("SetCursor", _H, (_H,)),
                ("DestroyIcon", wintypes.BOOL, (_H,)),
                ("GetSystemMetrics", C, (C,)),
                ("GetSystemMetricsForDpi", C, (C, U)),
                ("GetDpiForSystem", U, ()),
                ("GetDpiForWindow", U, (_H,)),
                ("SetThreadDpiAwarenessContext", _H, (_H,))):
            _decl(getattr(u, name), res, args)
        _decl(g.CreateCompatibleDC, _H, (_H,))
        _decl(g.CreateCompatibleBitmap, _H, (_H, ctypes.c_int, ctypes.c_int))
        _decl(g.SelectObject, _H, (_H, _H))
        _decl(g.DeleteObject, wintypes.BOOL, (_H,))
        _decl(g.DeleteDC, wintypes.BOOL, (_H,))
        _decl(g.BitBlt, wintypes.BOOL, (_H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, _H, ctypes.c_int, ctypes.c_int,
                                        wintypes.DWORD))
        _decl(g.CreateSolidBrush, _H, (wintypes.DWORD,))
        _decl(g.CreatePen, _H, (ctypes.c_int, ctypes.c_int, wintypes.DWORD))
        _decl(g.SetBkMode, ctypes.c_int, (_H, ctypes.c_int))
        _decl(g.MoveToEx, wintypes.BOOL, (_H, ctypes.c_int, ctypes.c_int, _H))
        _decl(g.LineTo, wintypes.BOOL, (_H, ctypes.c_int, ctypes.c_int))
        _decl(g.Polygon, wintypes.BOOL, (_H, _H, ctypes.c_int))
        _decl(g.SetPolyFillMode, ctypes.c_int, (_H, ctypes.c_int))
        _decl(g.RoundRect, wintypes.BOOL,
              (_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
               ctypes.c_int, ctypes.c_int))
        # 下面这几个是「好看的线条」必需的：ExtCreatePen 才能做出圆端点 + 圆拐角的几何笔
        _decl(g.ExtCreatePen, _H, (wintypes.DWORD, wintypes.DWORD, _H, wintypes.DWORD, _H))
        _decl(g.GetStockObject, _H, (ctypes.c_int,))
        _decl(g.Ellipse, wintypes.BOOL,
              (_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int))
        _decl(g.Arc, wintypes.BOOL,
              (_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int))
        _decl(g.Polyline, wintypes.BOOL, (_H, _H, ctypes.c_int))
        _decl(k.GetModuleHandleW, _H, (W,))
        _decl(k.GetLastError, wintypes.DWORD, ())
        _decl(k.OpenProcess, _H, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD))
        _decl(k.CloseHandle, wintypes.BOOL, (_H,))
        _user32, _gdi32, _kernel32 = u, g, k
        _wndproc = ctypes.WINFUNCTYPE(_LR, _H, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)(_on_message)
        _enumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, _H, wintypes.LPARAM)
        try:
            _dwmapi = ctypes.windll.dwmapi
            _decl(_dwmapi.DwmSetWindowAttribute,
                  ctypes.c_long, (_H, wintypes.DWORD, _H, wintypes.DWORD))
        except Exception:                      # 老系统没有 dwmapi：只是染不上色，不算错
            _dwmapi = None
        _api_ready = True


def _log(msg):
    try:
        with _log_lock:
            storage_write(LAUNCH_LOG_STREAM,
                          "[标题栏] %s %s\n" % (time.strftime("%H:%M:%S"), msg), append=True)
    except Exception:
        pass


# ---------- 功能栏：按钮清单与尺寸 ----------
# 顺序就是右侧竖列自上而下的顺序。
# 后五个只在「镜像桌面」出现：镜像应用开的是虚拟屏，返回 / 桌面 / 多任务 / 通知栏 /
# 控制中心这些打到手机真实界面上没有意义——要操作虚拟屏里的应用，那是 scrcpy 的活。
_BAR_COMMON = ("pin", "volume_up", "volume_down", "rotate_lock")
_BAR_DESKTOP = ("back", "home", "app_switch", "notifications", "control_center")

def _bar_actions(desktop):
    return _BAR_COMMON + (_BAR_DESKTOP if desktop else ())

# 条带尺寸（逻辑像素，实际按 DPI 缩放）
_STRIP_W = 34           # 展开后的宽度
_STRIP_EDGE = 6         # 收起时贴边留的那条细条：看得出有东西，又基本不挡画面
_STRIP_PAD = 4          # 条内上下留白
_BTN_MIN, _BTN_MAX = 22, 30
_COLLAPSE_DELAY = 0.6   # 鼠标离开后多久自动收起

# winbar 不认识 adb（device 已经 import 了本模块，反过来会成环），所以
# 「把动作发给手机」这一步由入口 launcher_server.py 注入进来。
_action_handler = None

def set_action_handler(fn):
    """注入动作执行器，签名 (serial, action) -> None。"""
    global _action_handler
    _action_handler = fn


# ---------- 对外接口 ----------

_bars = {}                  # {scrcpy 进程 pid: _Bar}
_bars_lock = threading.Lock()


def attach(proc, icon_path=None, always_on_top=False, serial=None, desktop=False):
    """给 scrcpy 窗口挂上右侧功能栏：窗口一出现就挂（后台线程，不阻塞调用方）。

    同一个进程只挂一次；scrcpy 退出后叠加窗口会自己收尾并清掉登记表。

    serial / desktop 是给按钮用的：前者决定 adb 命令发给哪台设备，后者决定要不要摆
    「返回 / 桌面 / 多任务 / 通知栏 / 控制中心」那一组。
    """
    pid = getattr(proc, "pid", 0)
    if not pid:
        return
    with _bars_lock:
        if pid in _bars:
            return
        _bars[pid] = _Bar(proc, icon_path, bool(always_on_top), serial, bool(desktop))
    threading.Thread(target=_run, args=(pid,), name="winbar-%d" % pid, daemon=True).start()


def shutdown():
    """退出应用时清掉登记表：窗口与句柄随进程一起被系统释放。"""
    with _bars_lock:
        bars = list(_bars.values())
        _bars.clear()
    for b in bars:
        b.cancelled = True


class _Bar:
    """一个叠加条的全部状态。只在它自己的线程里改。"""

    def __init__(self, proc, icon_path, pinned, serial=None, desktop=False):
        self.proc = proc
        self.pid = getattr(proc, "pid", 0)
        self.icon_path = icon_path
        self.pinned = pinned
        self.serial = serial        # 按钮的 adb 命令发给哪台设备
        self.desktop = desktop      # 镜像桌面才摆「返回」那一组
        self.open = False           # 功能栏是否已展开（鼠标悬停才展开）
        self.close_at = 0.0         # 鼠标离开后到点收起的时间戳
        self.rotate_on = False      # 强制横屏按钮的高亮状态（真实状态在手机上）
        self.actions = []           # 与 btns 一一对应的动作名
        self.target = None          # scrcpy 画面窗口
        self.hwnd = None            # 叠加条自己
        self.scale = 1.0
        self.icon = None            # 任务栏 / 标题栏用的 HICON
        self.icon_big = None
        self.hot = -1               # 鼠标悬停的按钮
        self.pressed = -1           # 按下还没松开的按钮
        self.btns = []              # [(左, 上, 右, 下), ...] 屏幕坐标
        self.band = None            # 最近一次算出来的功能栏条带（屏幕坐标）
        self.geom = None            # 最近一次的画面窗口矩形（变了才重算布局）
        self.retry_at = 0.0         # 布局失败后什么时候再试一次
        self.cancelled = False


def _run(pid):
    bar = None
    try:
        _ensure_api()
        bar = _bars.get(pid)
        if bar is None:
            return
        _set_thread_dpi()
        bar.scale = _scale_of(None)
        # 先把图标转好再去等窗口：等窗口的这几秒刚好用来转 ICO，窗口一出来图标就位
        bar.icon, bar.icon_big = _load_icons(_icon_source(bar), bar.scale)
        hwnd = _wait_target(bar)
        if bar.cancelled or not hwnd:
            return
        bar.target = hwnd
        _style_frame(hwnd)                  # DWM 深色标题栏
        _apply_icon(bar)                    # 目标应用图标（标题栏 + 任务栏）
        if _create(bar):
            _message_loop()
    except Exception as e:
        _log("异常退出：%r" % (e,))
    finally:
        if bar is not None:
            _release(bar)
        with _bars_lock:
            _bars.pop(pid, None)


def _icon_source(bar):
    """图标来源：目标应用自己的图标；没有就用快投自己的（镜像桌面就是这样）。"""
    if bar.icon_path and os.path.exists(bar.icon_path):
        return bar.icon_path
    own = os.path.join(RES_DIR, "appicon.ico")
    return own if os.path.exists(own) else None


def _set_thread_dpi():
    """本线程声明 DPI 感知：不声明的话 GDI 位图会被系统按缩放拉伸，图标会发虚。"""
    try:
        _user32.SetThreadDpiAwarenessContext(_H(-4))       # PER_MONITOR_AWARE_V2
    except Exception:
        pass


def _scale_of(hwnd):
    try:
        dpi = (_user32.GetDpiForWindow(hwnd) if hwnd else _user32.GetDpiForSystem())
        if dpi:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def _wait_target(bar, timeout=25):
    """等 scrcpy 的画面窗口出现；进程提前退出（启动失败）或超时返回 None。

    等的是「已显示」的窗口：scrcpy 是先建隐藏窗口、装好图标、显示第一帧时才 ShowWindow，
    看到可见窗口就说明它的初始化（含 SDL_SetWindowIcon）已经跑完了，这时候再改图标才稳。
    """
    deadline = time.time() + timeout
    while time.time() < deadline and not bar.cancelled:
        hwnd = _find_hwnd(bar.pid)
        if hwnd:
            return hwnd
        if _proc_gone(bar.pid):
            return None
        time.sleep(0.05)
    return None


def _proc_gone(pid):
    """进程是否已经退出：拿得到进程句柄就说明还在跑。"""
    h = _kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return True
    _kernel32.CloseHandle(h)
    return False


def _find_hwnd(pid):
    """按 PID 找第一个可见的顶层窗口（scrcpy 只开一个主窗口）。"""
    found = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == _CLASS_NAME:
            return True                     # 我们自己造的叠加条，跳过
        found.append(hwnd)
        return True

    try:
        _user32.EnumWindows(_enumproc(cb), 0)
    except Exception:
        return None
    return found[0] if found else None


# ---------- 图标 ----------

def _load_icons(path, scale):
    """把 webp/png 图标转成 ICO 并载入两份句柄（标题栏用小的、任务栏用大的）。

    句柄由本进程一直持有到窗口关闭——提前释放，窗口上那张图就空了。
    """
    if not path:
        return None, None
    tmp = None
    try:
        from PIL import Image  # 只有这里用得上，放到函数里导入
        tmp = tempfile.mkdtemp(prefix="kuaitou_bar_")
        ico = os.path.join(tmp, "app.ico")
        with Image.open(path) as im:
            im.convert("RGBA").save(ico, format="ICO",
                                    sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        small = _user32.LoadImageW(None, ico, _IMAGE_ICON, int(round(16 * scale)),
                                   int(round(16 * scale)), _LR_LOADFROMFILE)
        big = _user32.LoadImageW(None, ico, _IMAGE_ICON, int(round(32 * scale)),
                                 int(round(32 * scale)), _LR_LOADFROMFILE)
        return small or None, big or None
    except Exception as e:
        _log("转图标失败：%r" % (e,))
        return None, None
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _apply_icon(bar):
    """把画面窗口的标题栏 / 任务栏图标换成目标应用的图标。

    只设一次：scrcpy 只在初始化时设过自己的图标（向量图转出来的），我们排在它后面；
    补一次是防它在首帧渲染后又刷一遍。
    """
    if not bar.icon_big:
        return
    try:
        _user32.SendMessageW(bar.target, _WM_SETICON, _ICON_BIG, bar.icon_big)
        if bar.icon:
            _user32.SendMessageW(bar.target, _WM_SETICON, _ICON_SMALL, bar.icon)
    except Exception:
        pass


def _release(bar):
    for attr in ("icon", "icon_big"):
        h = getattr(bar, attr, None)
        if not h:
            continue
        setattr(bar, attr, None)
        try:
            _user32.DestroyIcon(h)
        except Exception:
            pass


# ---------- DWM 深色标题栏 ----------

def _style_frame(hwnd):
    """把原生标题栏染成和主界面一套的深色。

    这几项都是跨进程生效的（DWM 状态挂在窗口上，不要求同进程）——实测五个属性全部
    返回 S_OK，标题栏像素从系统默认的 #1c2125 变成我们设的 #1a1d24。
    """
    if _dwmapi is None:
        return
    want = ((_DWMWA_USE_IMMERSIVE_DARK_MODE, 1, "深色模式"),
            (_DWMWA_CAPTION_COLOR, _BG, "标题栏底色"),
            (_DWMWA_TEXT_COLOR, _TEXT, "标题文字"),
            (_DWMWA_BORDER_COLOR, _LINE, "边框"),
            (_DWMWA_WINDOW_CORNER_PREFERENCE, 2, "圆角"))   # 2 = DWMWCP_ROUND
    for attr, val, name in want:
        v = ctypes.c_int(val)
        try:
            hr = _dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
            if hr != 0:
                _log("DWM %s 失败 hr=0x%08X（系统版本可能不支持，不影响使用）"
                     % (name, hr & 0xFFFFFFFF))
        except Exception as e:
            _log("DWM %s 异常：%r" % (name, e))


# ---------- 主窗口标题栏：染色 + 去图标与标题，做出「类似无边框」的观感 ----------
# 主窗口是 pywebview 自己建的（WinForms + WebView2，同进程），这里只在外围改它的非客户区：
# 标题栏与界面同色、没有图标也没有「快投」几个字，只剩最小化/最大化/关闭和一圈主题色边框。
# 窗口标题文字保持「快投」不动（只是染成看不见），入口里靠标题找窗口的单实例唤起不能失效。
_MAIN_TITLE = "快投"

# (标题栏底色, 边框色, 是否深色)，与 index.html 的 CSS 变量一一对应；
# 标题文字色故意取和底色一样——DWM 没有「隐藏标题」的开关，染成底色就等于看不见。
_MAIN_THEME = {
    "dark": ((0x1a, 0x1d, 0x24), (0x2b, 0x2f, 0x3a), 1),
    "light": ((0xff, 0xff, 0xff), (0xdd, 0xe1, 0xe8), 0),
}

# 主窗口显示之后再补染一次的等待时长：太短了 WinForms 还没刷完，太长了用户能看见跳变。
_MAIN_SETTLE = 1.2

# 已经处理过的窗口，以及上次染上去的主题。
# 「去图标 + 加无图标标记」是重活：标记加上去之后得靠 SWP_FRAMECHANGED 让系统重算窗口
# 边框，而那个动作会把整块窗口（包括里面的网页）重画一遍。启动时做一次无所谓，
# 但每次切主题都跟着做，用户就会看见窗口闪一下——所以这里记下来，只在第一次做。
_styled_hwnd = None
_styled_theme = None
_style_lock = threading.Lock()


def style_main_window(theme="light"):
    """按主题给主窗口的标题栏上色（后台线程，不阻塞调用方）。

    主窗口可能在 webview.start() 之后才出现，所以这里自己等窗口，调用点随便什么时候调都行。
    切换主题时再调一次即可：去图标那种重活只干一次，之后只更新 DWM 的配色。
    """
    threading.Thread(target=_run_main_style, args=(str(theme),),
                     name="main-titlebar", daemon=True).start()


def _run_main_style(theme):
    global _styled_hwnd, _styled_theme
    try:
        _ensure_api()
        _set_thread_dpi()
        hwnd = _wait_main_window()
        if not hwnd:
            return
        with _style_lock:
            chrome = _styled_hwnd != hwnd           # 这个窗口还没去过图标
            if not chrome and _styled_theme == theme:
                return                              # 同一个窗口、同一套主题：无事可做
            _styled_hwnd, _styled_theme = hwnd, theme
        _style_main_frame(hwnd, theme, chrome)
        # 窗口刚显示出来时 pywebview 底下的 WinForms 还会把图标和非客户区再刷一遍，
        # 紧跟着染的那次会被顶回去（实测标题栏又变回系统默认色、图标也回来了），
        # 所以等它安定下来补一次。只有窗口第一次出现才需要：切主题也走这条路径，
        # 每次都补的话用户会在切完一秒后又看见窗口闪一下。
        if chrome:
            time.sleep(_MAIN_SETTLE)
            if _user32.IsWindow(hwnd):
                _style_main_frame(hwnd, theme, True)
    except Exception as e:
        _log("主窗口标题栏处理失败：%r" % (e,))


def _wait_main_window(timeout=30):
    """等主窗口出现：标题是「快投」，再核对窗口属于本进程，免得误改别的同名窗口。

    优先等它真正显示出来（WinForms 显示之后还会再刷一次窗口样式，太早染会被顶掉）；
    静默启动时窗口一直藏着，那就退而求其次先把当前句柄处理掉。
    """
    mypid = os.getpid()
    deadline = time.time() + timeout
    fallback = None
    while time.time() < deadline:
        hwnd = _user32.FindWindowW(None, _MAIN_TITLE)
        if hwnd:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == mypid:
                if _user32.IsWindowVisible(hwnd):
                    return hwnd
                if fallback is None:
                    fallback = hwnd
        time.sleep(0.2)
    return fallback


def _style_main_frame(hwnd, theme, chrome=True):
    """把主题刷到主窗口上。

    chrome=True 才动窗口样式与图标——那部分要重算窗口边框，会连网页一起重画，
    所以只在窗口第一次出现时做；之后换主题只改 DWM 的几个颜色，不碰窗口结构。
    """
    caption, border, dark = _MAIN_THEME.get(theme, _MAIN_THEME["light"])
    if chrome:
        # 去掉标题栏图标：WS_EX_DLGMODALFRAME 是系统「这个窗口不显示图标」的标记，
        # 加上后必须让窗口重算一次边框（SWP_FRAMECHANGED）才生效。
        try:
            ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_DLGMODALFRAME)
            _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                 _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER |
                                 _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        except Exception as e:
            _log("去掉主窗口标题栏图标失败：%r" % (e,))
        # Win11 光靠上面那个标记还不够：实测图标照旧画着，还得把窗口的图标本身清掉
        # （只清小图标也没用，标题栏会回落到大图标）。清空后任务栏 / Alt+Tab 会用 exe 自带
        # 的图标顶上，而那也是快投自己的图标，观感不变。
        try:
            for kind in (_ICON_SMALL, _ICON_BIG):
                _user32.SendMessageW(hwnd, _WM_SETICON, kind, 0)
        except Exception as e:
            _log("清空主窗口图标失败：%r" % (e,))
    if _dwmapi is None:
        return
    want = ((_DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0, "深色模式"),
            (_DWMWA_CAPTION_COLOR, _rgb(*caption), "标题栏底色"),
            (_DWMWA_TEXT_COLOR, _rgb(*caption), "标题文字"),
            (_DWMWA_BORDER_COLOR, _rgb(*border), "边框"),
            (_DWMWA_WINDOW_CORNER_PREFERENCE, 2, "圆角"))       # 2 = DWMWCP_ROUND
    for attr, val, name in want:
        v = ctypes.c_int(val)
        try:
            hr = _dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
            if hr != 0:
                _log("主窗口 DWM %s 失败 hr=0x%08X（系统版本可能不支持，不影响使用）"
                     % (name, hr & 0xFFFFFFFF))
        except Exception as e:
            _log("主窗口 DWM %s 异常：%r" % (name, e))


# ---------- 叠加窗口 ----------

def _register_class():
    wc = _WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(_wndproc, _H)
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    wc.hCursor = _user32.LoadCursorW(None, _H(_IDC_ARROW))
    wc.lpszClassName = _CLASS_NAME
    if _user32.RegisterClassW(ctypes.byref(wc)):
        return True
    # 1410 = 类已存在：同进程重复注册，可以直接用
    return _kernel32.GetLastError() == 1410


def _create(bar):
    """建叠加条自己那条窗口：无边框、不抢焦点、不进任务栏/Alt+Tab，作为画面窗口的属主。

    属主关系（CreateWindowEx 的 hWndParent 传画面窗口）保证它永远压在画面之上、
    跟着一起最小化，也不会在任务栏多冒出一个条目。
    """
    if not _register_class():
        _log("注册窗口类失败 err=%d" % _kernel32.GetLastError())
        return False
    ex = _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE | _WS_EX_LAYERED
    hwnd = _user32.CreateWindowExW(ex, _CLASS_NAME, _CLASS_NAME, _WS_POPUP,
                                   0, 0, 1, 1, bar.target, None,
                                   _kernel32.GetModuleHandleW(None), None)
    if not hwnd:
        _log("创建叠加窗口失败 err=%d" % _kernel32.GetLastError())
        return False
    bar.hwnd = hwnd
    _bars_hwnd[hwnd] = bar
    # 色键透明：键色像素既不显示也点不到，鼠标穿到下面的原生标题栏上
    _user32.SetLayeredWindowAttributes(hwnd, _KEY, 0, _LWA_COLORKEY)
    _user32.SetTimer(hwnd, _TIMER_ID, _FOLLOW_MS, None)
    _on_timer(bar)
    return True


# ---------- 布局：算出手上的按钮压在标题栏的哪个位置 ----------

def _client_screen_rect(hwnd):
    """目标窗口客户区在屏幕上的矩形，也就是画面区域。拿不到返回 None。

    用客户区而不是整窗矩形：功能栏要贴着画面右边缘，标题栏和边框都不算数。
    顺带也不再看窗口样式，所以全屏（没有标题栏）时照样能算出来——旧版全屏是直接放弃的。
    """
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    rc = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    return (pt.x, pt.y, pt.x + w, pt.y + h)


def _layout(bar):
    """算出右侧功能栏的条带与每个按钮（都是屏幕坐标）。量不出来时返回 False。

    收起态只留贴边一条细条、btns 为空——那样既看得出有东西，又点不到；
    展开态按钮竖向均分，边长先按最小尺寸排，放得下再放大到上限。
    """
    rect = _client_screen_rect(bar.target)
    if not rect:
        return False
    left, top, right, bottom = rect
    actions = _bar_actions(bar.desktop)
    pad = int(round(_STRIP_PAD * bar.scale))
    # 放不下所有按钮就整条不摆：宁可不给，也别让按钮溢出到条带外面去
    if not actions or bottom - top - pad * 2 < _BTN_MIN * bar.scale * len(actions):
        return False
    if right - left < int(round(_STRIP_W * 3 * bar.scale)):
        return False
    strip = _STRIP_W if bar.open else _STRIP_EDGE
    sx1 = right - int(round(strip * bar.scale))
    bar.band = (sx1, top + pad, right, bottom - pad)
    if not bar.open:
        bar.btns, bar.actions = [], []
        return True
    avail = bar.band[3] - bar.band[1]
    size = max(int(round(_BTN_MIN * bar.scale)),
               min(int(round(_BTN_MAX * bar.scale)), avail // len(actions)))
    x1 = sx1 + max(0, (right - sx1 - size) // 2)
    y = bar.band[1] + max(0, (avail - size * len(actions)) // 2)
    btns = []
    for _ in actions:
        btns.append((x1, y, x1 + size, y + size))
        y += size
    bar.btns, bar.actions = btns, list(actions)
    return True


def _window_rect(hwnd):
    r = wintypes.RECT()
    if not hwnd or not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r


# ---------- 定时跟随 ----------

def _on_timer(bar):
    if bar is None or not bar.hwnd:
        return
    if not _user32.IsWindow(bar.target):
        _user32.DestroyWindow(bar.hwnd)      # scrcpy 已经没了：叠加条跟着收工
        return
    if _user32.IsIconic(bar.target):         # 最小化时不留几个孤零零的按钮在桌面上
        bar.geom = None                      # 还原时窗口矩形没变，得靠清空来逼一次重算
        _hide(bar)
        return

    r = _window_rect(bar.target)
    if not r:
        return
    geom = (r.left, r.top, r.right, r.bottom)
    retry = bar.band is None and time.time() >= bar.retry_at
    was_open = bar.open
    _update_open(bar)
    if geom != bar.geom or retry or bar.open != was_open:
        bar.geom = geom
        if not _layout(bar):
            bar.retry_at = time.time() + 0.5     # 窗口太小 / 刚起来：半秒后再试，别空转
            _hide(bar)
            return
        bl, bt, br, bb = bar.band
        _user32.SetWindowPos(bar.hwnd, _HWND_TOPMOST if bar.pinned else None,
                             bl, bt, br - bl, bb - bt,
                             _SWP_NOACTIVATE | (0 if bar.pinned else _SWP_NOZORDER))
        if not _user32.IsWindowVisible(bar.hwnd):
            _user32.ShowWindow(bar.hwnd, _SW_SHOWNOACTIVATE)
    if bar.band is None:
        return
    _hover(bar)


def _update_open(bar):
    """按光标位置决定功能栏展开还是收起。

    必须用 GetCursorPos 自己判，不能等 WM_MOUSEMOVE：叠加窗口是色键透明的，细条那一片
    收不到任何鼠标消息（「鼠标穿透」就是这么实现的），靠窗口内移动来触发展开永远不会发生。
    这也是现有 _hover 一直在用的办法。
    """
    if bar.band is None:
        return
    pt = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return
    _, y1, x2, y2 = bar.band
    # 判定区按「展开后」的宽度算：收起时只有 6px，不然鼠标很难正好扫到
    inside = (x2 - int(round(_STRIP_W * bar.scale)) <= pt.x < x2 and y1 <= pt.y < y2)
    now = time.time()
    if inside:
        bar.close_at = 0.0
        bar.open = True
    elif bar.open:
        if not bar.close_at:
            bar.close_at = now + _COLLAPSE_DELAY
        elif now >= bar.close_at:
            bar.open, bar.close_at = False, 0.0


def _hide(bar):
    bar.band = None
    bar.btns = []
    bar.actions = []
    bar.hot = -1
    if _user32.IsWindowVisible(bar.hwnd):
        _user32.ShowWindow(bar.hwnd, _SW_HIDE)


def _hover(bar):
    """悬停高亮：直接用光标位置自己判，不依赖 WM_MOUSEMOVE。

    色键透明的窗口只在图标那几个像素上收得到鼠标消息，靠在窗口内移动来触发重绘并不可靠。
    """
    pt = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return
    r = _window_rect(bar.hwnd)
    if not r:
        return
    idx = -1
    if r.left <= pt.x < r.right and r.top <= pt.y < r.bottom:
        for i, (x1, y1, x2, y2) in enumerate(bar.btns):
            if x1 <= pt.x < x2 and y1 <= pt.y < y2:
                idx = i
                break
    if idx != bar.hot:
        bar.hot = idx
        _user32.InvalidateRect(bar.hwnd, None, False)


def _message_loop():
    msg = _MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))


# ---------- 窗口过程 ----------

_bars_hwnd = {}             # {叠加条 hwnd: _Bar}，窗口过程据此找到自己的状态


def _on_message(hwnd, msg, wparam, lparam):
    bar = _bars_hwnd.get(hwnd)
    try:
        if msg == _WM_PAINT:
            _paint(bar, hwnd)
            return 0
        if msg == _WM_ERASEBKGND:
            return 1                        # 整条我们自己画，别再刷一遍底色（防闪）
        if msg == _WM_TIMER:
            _on_timer(bar)
            return 0
        if msg == _WM_NCDESTROY:
            _bars_hwnd.pop(hwnd, None)
            if bar is not None:
                bar.hwnd = None
            _user32.PostQuitMessage(0)      # 窗口没了，消息循环跟着结束
            return 0
        if bar is None:
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        if msg == _WM_LBUTTONDOWN:
            _on_down(bar)
            return 0
        if msg == _WM_LBUTTONUP:
            _on_up(bar)
            return 0
        if msg == _WM_SETCURSOR:
            _user32.SetCursor(_user32.LoadCursorW(None, _H(_IDC_ARROW)))
            return 1
        if msg == _WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
    except Exception as e:
        _log("消息处理异常：%r" % (e,))
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _btn_at(bar, x, y):
    """鼠标落在哪个按钮上（-1 = 不在按钮上）。坐标是屏幕坐标。"""
    for i, (x1, y1, x2, y2) in enumerate(bar.btns):
        if x1 <= x < x2 and y1 <= y < y2:
            return i
    return -1


def _on_down(bar):
    """按下：记下按钮并抓住鼠标——拖到按钮外松开就算取消，跟系统按钮一个脾气。"""
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    bar.pressed = _btn_at(bar, pt.x, pt.y)
    if bar.pressed >= 0:
        _user32.SetCapture(bar.hwnd)


def _on_up(bar):
    idx = bar.pressed
    bar.pressed = -1
    if idx < 0:
        return
    _user32.ReleaseCapture()
    if idx != bar.hot:                      # 松手时还在按钮上才算数
        return
    if idx < len(bar.actions):
        _run_action(bar, bar.actions[idx])


def _run_action(bar, action):
    """点了一个按钮：置顶是纯窗口操作，留在本地；其余交给注入的执行器去发 adb。

    执行器放后台线程里跑：adb 一次往返几百毫秒，在消息循环里同步等会把整个功能栏
    （连跟随画面的定时器）一起卡住。
    """
    if action == "pin":
        _act_pin(bar)
        return
    if action == "rotate_lock":
        # 乐观翻一下高亮。真实状态在手机上，这里只负责按钮看起来对不对
        bar.rotate_on = not bar.rotate_on
        _user32.InvalidateRect(bar.hwnd, None, False)
    handler = _action_handler
    if handler is None:
        _log("功能栏动作 %s 没有执行器：launcher_server 忘了注入？" % action)
        return
    threading.Thread(target=_invoke_action, args=(handler, bar.serial, action),
                     name="winbar-act", daemon=True).start()


def _invoke_action(handler, serial, action):
    try:
        handler(serial, action)
    except Exception as e:
        _log("功能栏动作 %s 执行失败：%r" % (action, e))


def _act_pin(bar):
    """置顶：改画面窗口的 TOPMOST 状态，并记进配置，下次投屏直接带 --always-on-top。"""
    bar.pinned = not bar.pinned
    try:
        _user32.SetWindowPos(bar.target,
                             _HWND_TOPMOST if bar.pinned else _HWND_NOTOPMOST,
                             0, 0, 0, 0,
                             _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        save_config({"always_on_top": bar.pinned})
    except Exception as e:
        _log("置顶失败：%r" % (e,))
    _log("置顶 -> %s" % ("开" if bar.pinned else "关"))
    _user32.InvalidateRect(bar.hwnd, None, False)


# ---------- 绘制 ----------

def _paint(bar, hwnd):
    ps = _PAINTSTRUCT()
    hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
    if not hdc:
        return
    try:
        if bar is not None and bar.band:
            _paint_bar(bar, hdc)
    finally:
        _user32.EndPaint(hwnd, ctypes.byref(ps))


def _fill(hdc, x1, y1, x2, y2, color):
    br = _gdi32.CreateSolidBrush(color)
    old = _gdi32.SelectObject(hdc, br)
    pen = _gdi32.CreatePen(0, 1, color)
    oldp = _gdi32.SelectObject(hdc, pen)
    _gdi32.RoundRect(hdc, x1, y1, x2, y2, 6, 6)
    _gdi32.SelectObject(hdc, oldp)
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(pen)
    _gdi32.DeleteObject(br)


def _veil(hdc, w, h):
    """整块铺键色：这块地方既不显示也收不到鼠标，等于「透明 + 穿透」。"""
    br = _gdi32.CreateSolidBrush(_KEY)
    old = _gdi32.SelectObject(hdc, br)
    pen = _gdi32.CreatePen(0, 1, _KEY)
    oldp = _gdi32.SelectObject(hdc, pen)
    _gdi32.RoundRect(hdc, -1, -1, w + 2, h + 2, 0, 0)
    _gdi32.SelectObject(hdc, oldp)
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(pen)
    _gdi32.DeleteObject(br)


def _paint_bar(bar, hdc):
    r = _window_rect(bar.hwnd)
    if not r:
        return
    w, h = r.right - r.left, r.bottom - r.top
    _veil(hdc, w, h)
    ox, oy = bar.band[0], bar.band[1]
    if not bar.open:
        # 收起态：贴右边缘一条细高亮，提示「这儿有东西」。鼠标扫过来就展开
        _fill(hdc, ox, oy, ox + w - 1, oy + h - 1, _DIM)
        return
    for i, (x1, y1, x2, y2) in enumerate(bar.btns):
        x, y = x1 - ox, y1 - oy
        bw, bh = x2 - x1, y2 - y1
        hovered = (bar.hot == i)
        action = bar.actions[i] if i < len(bar.actions) else ""
        if hovered:
            _fill(hdc, x + 1, y + 1, x + bw - 1, y + bh - 1, _HOVER)
        # 置顶 / 强制横屏开着时图标换成强调色：一眼看得出当前是「已开」
        on = (action == "pin" and bar.pinned) or (action == "rotate_lock" and bar.rotate_on)
        _draw_glyph(hdc, x + bw // 2, y + bh // 2, bar.scale,
                    _ACCENT if on else (_TEXT if hovered else _DIM), action)


# ---------- 图标：统一的「细线 + 圆头圆角」笔触 ----------
# 参考的那套图标（vivo 那种观感）线条细、端点圆、拐角圆。GDI 默认的 cosmetic 笔是方头，
# 描出来又硬又脏，所以统一改用几何笔 + 圆端点 + 圆连接，并且所有图标的笔宽都取自同一个
# 常量，摆在一列里粗细才一致。
_PS_GEOMETRIC = 0x00010000
_BS_SOLID = 0
_NULL_BRUSH = 5
_STROKE_W = 1.7


class _LOGBRUSH(ctypes.Structure):
    _fields_ = [("lbStyle", wintypes.UINT),
                ("lbColor", wintypes.DWORD),
                ("lbHatch", ctypes.c_size_t)]


class _Stroke:
    """一次绘制用的笔与刷。

    NULL_BRUSH 是关键：RoundRect / Ellipse / Polygon 都会拿当前画刷去填充，想只描轮廓
    就得先把画刷换成空的，需要实心圆点时再切回来。
    """

    def __init__(self, hdc, width, color):
        self.hdc = hdc
        brush_def = _LOGBRUSH(_BS_SOLID, color, 0)
        self.pen = _gdi32.ExtCreatePen(_PS_GEOMETRIC, max(1, int(round(width))),
                                       ctypes.byref(brush_def), 0, None)
        if not self.pen:                       # 老系统兜底：至少别画不出来
            self.pen = _gdi32.CreatePen(0, max(1, int(round(width))), color)
        self.old_pen = _gdi32.SelectObject(hdc, self.pen)
        self.brush = _gdi32.CreateSolidBrush(color)
        self.old_brush = _gdi32.SelectObject(hdc, _gdi32.GetStockObject(_NULL_BRUSH))

    def solid(self):
        _gdi32.SelectObject(self.hdc, self.brush)

    def hollow(self):
        _gdi32.SelectObject(self.hdc, _gdi32.GetStockObject(_NULL_BRUSH))

    def close(self):
        _gdi32.SelectObject(self.hdc, self.old_pen)
        _gdi32.SelectObject(self.hdc, self.old_brush)
        _gdi32.DeleteObject(self.pen)
        _gdi32.DeleteObject(self.brush)


def _seg(hdc, x1, y1, x2, y2):
    _gdi32.MoveToEx(hdc, int(round(x1)), int(round(y1)), None)
    _gdi32.LineTo(hdc, int(round(x2)), int(round(y2)))


def _path(hdc, pts):
    """折线。用几何笔画出来拐角是圆的，箭头那种折角才不会显得尖。"""
    arr = (_POINT * len(pts))(*[wintypes.POINT(int(round(x)), int(round(y))) for x, y in pts])
    _gdi32.Polyline(hdc, arr, len(pts))


def _oval(hdc, cx, cy, r):
    _gdi32.Ellipse(hdc, int(round(cx - r)), int(round(cy - r)),
                   int(round(cx + r)), int(round(cy + r)))


def _rrect(hdc, x1, y1, x2, y2, r):
    _gdi32.RoundRect(hdc, int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2)),
                     int(round(r)), int(round(r)))


def _arc(hdc, cx, cy, rx, ry, a0, a1):
    """圆弧（角度制，0° = 正右，逆时针为正）。GDI 的 Arc 也是逆时针画的。"""
    x0, y0 = cx + rx * math.cos(a0), cy - ry * math.sin(a0)
    x1, y1 = cx + rx * math.cos(a1), cy - ry * math.sin(a1)
    _gdi32.Arc(hdc, int(round(cx - rx)), int(round(cy - ry)),
               int(round(cx + rx)), int(round(cy + ry)),
               int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))


def _g_pin(hdc, cx, cy, u, st):
    """置顶：顶上一条横线当天花板，下面一个朝上的折角箭头。"""
    st.hollow()
    _seg(hdc, cx - u * .78, cy - u * .82, cx + u * .78, cy - u * .82)
    _path(hdc, [(cx - u * .52, cy - u * .06), (cx, cy - u * .58), (cx + u * .52, cy - u * .06)])
    _seg(hdc, cx, cy - u * .5, cx, cy + u * .72)


def _g_volume(hdc, cx, cy, u, st, up):
    """音量：喇叭 + 声波。加号那边多一道弧，两者一眼能分开。"""
    st.hollow()
    _path(hdc, [(cx - u * .85, cy - u * .3), (cx - u * .38, cy - u * .3),
                (cx + u * .04, cy - u * .78), (cx + u * .04, cy + u * .78),
                (cx - u * .38, cy + u * .3), (cx - u * .85, cy + u * .3),
                (cx - u * .85, cy - u * .3)])
    tip = cx + u * .16
    _arc(hdc, tip, cy, u * .42, u * .42, math.radians(-48), math.radians(48))
    if up:
        _arc(hdc, tip, cy, u * .8, u * .8, math.radians(-48), math.radians(48))


def _g_rotate(hdc, cx, cy, u, st):
    """强制横屏：横过来的手机（圆角屏 + 底部一条短横线当手势条）。"""
    st.hollow()
    w, h = u * .95, u * .58
    _rrect(hdc, cx - w, cy - h, cx + w, cy + h, u * .3)
    _seg(hdc, cx - u * .2, cy + h * .52, cx + u * .2, cy + h * .52)


def _g_back(hdc, cx, cy, u, st):
    """返回：一个左折角（现代安卓的返回手势图标）。"""
    st.hollow()
    _path(hdc, [(cx + u * .12, cy - u * .72), (cx - u * .6, cy), (cx + u * .12, cy + u * .72)])


def _g_home(hdc, cx, cy, u, st):
    """桌面：一个圆（安卓导航栏上的主页就是它）。"""
    st.hollow()
    _oval(hdc, cx, cy, u * .7)


def _g_app_switch(hdc, cx, cy, u, st):
    """多任务：一个圆角方框（安卓导航栏上的最近任务）。"""
    st.hollow()
    r = u * .66
    _rrect(hdc, cx - r, cy - r, cx + r, cy + r, u * .26)


def _g_notifications(hdc, cx, cy, u, st):
    """通知栏：铃铛（圆顶 + 两侧直壁 + 底沿 + 一个铃舌）。"""
    st.hollow()
    r = u * .56
    _arc(hdc, cx, cy - u * .1, r, r, math.radians(0), math.radians(180))
    _seg(hdc, cx - r, cy - u * .1, cx - r, cy + u * .4)
    _seg(hdc, cx + r, cy - u * .1, cx + r, cy + u * .4)
    _seg(hdc, cx - u * .78, cy + u * .4, cx + u * .78, cy + u * .4)
    st.solid()
    _oval(hdc, cx, cy + u * .76, max(1.0, u * .17))


def _g_control_center(hdc, cx, cy, u, st):
    """控制中心：两条滑杆各带一个滑块（快捷设置面板）。"""
    st.hollow()
    _seg(hdc, cx - u * .85, cy - u * .42, cx + u * .85, cy - u * .42)
    _seg(hdc, cx - u * .85, cy + u * .42, cx + u * .85, cy + u * .42)
    st.solid()
    _oval(hdc, cx - u * .28, cy - u * .42, max(1.5, u * .26))
    _oval(hdc, cx + u * .36, cy + u * .42, max(1.5, u * .26))


_GLYPHS = {
    "pin": _g_pin,
    "volume_up": lambda hdc, cx, cy, u, st: _g_volume(hdc, cx, cy, u, st, True),
    "volume_down": lambda hdc, cx, cy, u, st: _g_volume(hdc, cx, cy, u, st, False),
    "rotate_lock": _g_rotate,
    "back": _g_back,
    "home": _g_home,
    "app_switch": _g_app_switch,
    "notifications": _g_notifications,
    "control_center": _g_control_center,
}


def _draw_glyph(hdc, cx, cy, scale, color, action):
    """按动作画图标。

    所有图标共用同一支「细线 + 圆头圆角」的笔（见 _Stroke），粗细也取自同一个常量，
    竖排在一列里才显得是一套。绘制全部用 GDI 图元现画，不引外部图片资源。
    """
    drawer = _GLYPHS.get(action)
    if not drawer:
        return
    st = _Stroke(hdc, _STROKE_W * scale, color)
    try:
        drawer(hdc, cx, cy, max(5.0, _GLYPH * scale / 2.0), st)
    finally:
        st.close()


def _line(hdc, x1, y1, x2, y2, color, width=1):
    pen = _gdi32.CreatePen(0, max(1, int(width)), color)
    old = _gdi32.SelectObject(hdc, pen)
    _gdi32.MoveToEx(hdc, int(x1), int(y1), None)
    _gdi32.LineTo(hdc, int(x2), int(y2))
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(pen)
