"""投屏窗口原生标题栏的增强。


scrcpy 自己会开一个带原生标题栏的窗口，但那条栏上只有最小化/最大化/关闭，默认还是浅色
的，跟主界面的深色主题对不上。这里在原生标题栏上补三件事：

1. 用 DWM 把标题栏染成主界面的深色（深色标题栏 + 底色/文字色/边框色 + 圆角）；
2. 把窗口图标（含任务栏）换成目标应用自己的图标，ICO 在等窗口的那几秒里先转好，
   窗口一出现就是最终图标，不会再「先出来一个默认图标、过一会儿才换掉」；
3. 在系统按钮左边叠一个自己的按钮：📌 置顶。

   适应窗口 / 全屏这两个动作本来也能做，但 scrcpy 自己就有快捷键（按住 MOD 再按 W / F），
   多摆两个按钮反而把标题栏占满，所以去掉了。

为什么不自己画整条标题栏：scrcpy 用 --window-borderless 起来时是 WS_POPUP 窗口，整个
窗口都算客户区，系统不给缩放边框——实测拖右下角窗口纹丝不动（831×1847 不变）。恢复原生
边框后，拖边缩放、双击最大化、贴边、系统菜单全都回来了，代价是不能再自己画整条栏，
只能在系统的标题栏上做文章。

叠加窗口用色键透明（WS_EX_LAYERED + LWA_COLORKEY）：底色画成键色，那块地方既不显示也
点不到，鼠标会穿过去落到下面的标题栏上——标题栏照样按住拖动，我们只吃自己图标上的点击。
顺带绕开了「窗口激活/非激活时标题栏底色不一样」的麻烦：我们根本不画底色。

线程与 DPI：整条逻辑跑在自己的线程上，进线程先声明 PER_MONITOR_AWARE_V2——主进程不是
DPI 感知的，沿用主线程的坐标会被系统按缩放拉伸。该线程内一律物理像素。
"""


import ctypes
import os
import shutil
import tempfile
import threading
import time
from ctypes import wintypes

from .storage import LAUNCH_LOG_STREAM, RES_DIR, save_config, storage_write

# ---------- 尺寸 / 配色（逻辑像素，实际按 DPI 缩放）----------
_GLYPH = 15                 # 图标本体的边长


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
_WS_CAPTION = 0x00C00000
_GWL_STYLE = -16
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


_CCHILDREN_TITLEBAR = 5


class _TITLEBARINFOEX(ctypes.Structure):
    """WM_GETTITLEBARINFOEX 的返回结构：标题栏矩形 + 每个系统按钮的矩形。

    实测（Win11 原生边框窗口）有效的是索引 2=最小化、3=最大化、5=关闭，其余为空。
    代码里不按索引取，只取「所有非空矩形的并集」，免得不同系统版本索引含义变了就抓瞎。
    """
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcTitleBar", wintypes.RECT),
                ("rgstate", wintypes.DWORD * (_CCHILDREN_TITLEBAR + 1)),
                ("rgrect", wintypes.RECT * (_CCHILDREN_TITLEBAR + 1))]


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


# ---------- 对外接口 ----------

_bars = {}                  # {scrcpy 进程 pid: _Bar}
_bars_lock = threading.Lock()


def attach(proc, icon_path=None, always_on_top=False):
    """给 scrcpy 进程的原生标题栏做增强：窗口一出现就挂上（后台线程，不阻塞调用方）。

    同一个进程只挂一次；scrcpy 退出后叠加窗口会自己收尾并清掉登记表。
    """
    pid = getattr(proc, "pid", 0)
    if not pid:
        return
    with _bars_lock:
        if pid in _bars:
            return
        _bars[pid] = _Bar(proc, icon_path, bool(always_on_top))
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

    def __init__(self, proc, icon_path, pinned):
        self.proc = proc
        self.pid = getattr(proc, "pid", 0)
        self.icon_path = icon_path
        self.pinned = pinned
        self.target = None          # scrcpy 画面窗口
        self.hwnd = None            # 叠加条自己
        self.scale = 1.0
        self.icon = None            # 任务栏 / 标题栏用的 HICON
        self.icon_big = None
        self.hot = -1               # 鼠标悬停的按钮
        self.pressed = -1           # 按下还没松开的按钮
        self.btns = []              # [(左, 上, 右, 下), ...] 客户区坐标
        self.band = None            # 最近一次算出来的标题栏按钮带
        self.geom = None            # 最近一次的画面窗口矩形（变了才重算布局）
        self.retry_at = 0.0         # 布局失败（全屏等）后什么时候再问一次
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


def style_main_window(theme="dark"):
    """按主题给主窗口的标题栏上色（后台线程，不阻塞调用方）。

    主窗口可能在 webview.start() 之后才出现，所以这里自己等窗口，调用点随便什么时候调都行。
    切换主题时再调一次即可，重复调用是幂等的。
    """
    threading.Thread(target=_run_main_style, args=(str(theme),),
                     name="main-titlebar", daemon=True).start()


def _run_main_style(theme):
    try:
        _ensure_api()
        _set_thread_dpi()
        hwnd = _wait_main_window()
        if not hwnd:
            return
        _style_main_frame(hwnd, theme)
        # 窗口刚显示出来时 pywebview 底下的 WinForms 还会把图标和非客户区再刷一遍，
        # 紧跟着染的那次会被顶回去（实测标题栏又变回系统默认色、图标也回来了）。
        # 等它安定下来补一次；幂等，重复调用无害。
        time.sleep(_MAIN_SETTLE)
        if _user32.IsWindow(hwnd):
            _style_main_frame(hwnd, theme)
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


def _style_main_frame(hwnd, theme):
    caption, border, dark = _MAIN_THEME.get(theme, _MAIN_THEME["dark"])
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

def _sys_buttons(hwnd):
    """问系统要标题栏上系统按钮的矩形；一个都没有就说明这窗口没有标题栏（比如全屏）。"""
    info = _TITLEBARINFOEX()
    info.cbSize = ctypes.sizeof(_TITLEBARINFOEX)
    # lParam 声明成了 LPARAM（整数），这里必须传地址的数值：传 c_void_p 会被 ctypes 拒收
    got = _user32.SendMessageW(hwnd, 0x033F, 0, ctypes.addressof(info))
    if not got:
        return None
    return [r for r in info.rgrect if r.right > r.left and r.bottom > r.top]


def _layout(bar):
    """算标题栏那条按钮带（屏幕坐标）与置顶按钮的位置。全屏 / 问不到时返回 False。

    按钮宽度直接照抄系统按钮：几个系统按钮等宽，量出它们的总宽除以个数，我们画的按钮
    就和原生的一模一样大、一样高，看起来才像一家的。
    """
    # 全屏时窗口没有标题栏。这里必须先按窗口样式排掉：全屏下问 WM_GETTITLEBARINFOEX
    # 照样会「成功」返回，但里面是几块离谱的矩形（实测算出 2064×2065 的按钮带），
    # 照它摆按钮会得到一条横贯屏幕的怪东西。
    if not _user32.GetWindowLongW(bar.target, _GWL_STYLE) & _WS_CAPTION:
        return False
    boxes = _sys_buttons(bar.target)
    if not boxes:
        return False
    top = min(b.top for b in boxes)
    bottom = max(b.bottom for b in boxes)
    left = min(b.left for b in boxes)
    right = max(b.right for b in boxes)
    btn_w = max(int(round(28 * bar.scale)), (right - left) // len(boxes))
    bar.btns = [(left - btn_w, top, left, bottom)]
    bar.band = (bar.btns[0][0], top, left, bottom)
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
    if geom != bar.geom or retry:
        bar.geom = geom
        if not _layout(bar):
            bar.retry_at = time.time() + 0.5     # 全屏 / 异常：半秒后再问一次，别空转
            _hide(bar)
            return
        bl, bt, br, bb = bar.band
        _user32.SetWindowPos(bar.hwnd, _HWND_TOPMOST if bar.pinned else None,
                             bl, bt, br - bl, bb - bt,
                             _SWP_NOACTIVATE | (0 if bar.pinned else _SWP_NOZORDER))
    if bar.band is None:
        return
    if not _user32.IsWindowVisible(bar.hwnd):
        _user32.ShowWindow(bar.hwnd, _SW_SHOWNOACTIVATE)
    _hover(bar)


def _hide(bar):
    bar.band = None
    bar.btns = []
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
    if idx >= 0:
        _user32.ReleaseCapture()
        if idx == bar.hot:                  # 松手时还在按钮上才算数
            _act_pin(bar)


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
        if bar is not None and bar.btns:
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
    for x1, y1, x2, y2 in bar.btns:
        x, y = x1 - ox, y1 - oy
        bw, bh = x2 - x1, y2 - y1
        hovered = (bar.hot == 0)
        if hovered:
            _fill(hdc, x + 1, y + 3, x + bw - 1, y + bh - 3, _HOVER)
        # 置顶生效时整个图标换成强调色，一眼看得出当前是「已置顶」
        color = _ACCENT if bar.pinned else (_TEXT if hovered else _DIM)
        _draw_pin(hdc, x + bw // 2, y + bh // 2, bar.scale, color)


def _line(hdc, x1, y1, x2, y2, color, width=1):
    pen = _gdi32.CreatePen(0, max(1, int(width)), color)
    old = _gdi32.SelectObject(hdc, pen)
    _gdi32.MoveToEx(hdc, int(x1), int(y1), None)
    _gdi32.LineTo(hdc, int(x2), int(y2))
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(pen)


def _poly(hdc, pts, color):
    br = _gdi32.CreateSolidBrush(color)
    pen = _gdi32.CreatePen(0, 1, color)
    oldb = _gdi32.SelectObject(hdc, br)
    oldp = _gdi32.SelectObject(hdc, pen)
    arr = (_POINT * len(pts))(*[wintypes.POINT(int(x), int(y)) for x, y in pts])
    _gdi32.SetPolyFillMode(hdc, _WINDING)
    _gdi32.Polygon(hdc, arr, len(pts))
    _gdi32.SelectObject(hdc, oldp)
    _gdi32.SelectObject(hdc, oldb)
    _gdi32.DeleteObject(pen)
    _gdi32.DeleteObject(br)


def _draw_pin(hdc, cx, cy, scale, color):
    """置顶：向上的箭头压在一条底座上。"""
    s = max(8, int(round(_GLYPH * scale)))
    h2 = s // 2
    top = cy - h2
    _poly(hdc, [(cx, top), (cx - h2, top + s // 2), (cx + h2, top + s // 2)], color)
    _line(hdc, cx, top + s // 2, cx, cy + h2, color, max(1, s // 8))
    _line(hdc, cx - h2 + 1, cy + h2, cx + h2 - 1, cy + h2, color, max(1, s // 8))
