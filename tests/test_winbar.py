"""叠加条（投屏窗口右侧功能栏）：纯逻辑部分（布局、命中、悬停展开、动作转发）。

真实的窗口 / GDI 绘制不在这里测 —— 那要靠真机投屏实测。测试只保证不依赖 Windows
消息循环的那几段算术是对的（它们错了用户就点不中按钮、或者按钮溢出到条带外面），
以及「哪些事只该做一次」「哪些动作该转出去」这类调度判断。
"""

import ctypes
import time

import pytest

from kuaitou import winbar


@pytest.fixture(autouse=True)
def _clean_bars():
    """登记表是模块级状态，测试间必须清干净。"""
    with winbar._bars_lock:
        winbar._bars.clear()
    yield
    with winbar._bars_lock:
        winbar._bars.clear()


@pytest.fixture
def no_threads(monkeypatch):
    """attach 会起后台线程去建窗口，测试里换成只登记不执行。"""
    started = []

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(winbar.threading, "Thread", _FakeThread)
    return started


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class _FakeBar:
    """_btn_at / _layout / _update_open / _run_action 只关心这几个字段，不必造真的 _Bar。"""

    def __init__(self, target=None, scale=1.0):
        self.target = target
        self.scale = scale
        self.serial = None
        self.desktop = False
        self.open = True
        self.rotate_on = False
        self.hwnd = None
        self.hot = -1
        self.pressed = -1
        self.btns = []
        self.actions = []
        self.band = None


class _FakeCursorApi:
    """_update_open 靠 GetCursorPos 自己判光标位置：给个固定坐标的假实现。"""

    def __init__(self, x, y, ok=True):
        self.pt = winbar.wintypes.POINT(x, y)
        self.ok = ok

    def GetCursorPos(self, ref):
        if not self.ok:
            return False
        ctypes.memmove(ref, ctypes.byref(self.pt), ctypes.sizeof(self.pt))
        return True


class _FakePaintApi:
    def InvalidateRect(self, *args):
        return True


@pytest.fixture(autouse=True)
def _reset_action_handler():
    """动作执行器是模块级状态，注入过就会留给下一个用例。"""
    winbar.set_action_handler(None)
    yield
    winbar.set_action_handler(None)


def test_rgb_is_colorref_not_web_order():
    """COLORREF 是 0x00BBGGRR，写反了标题栏染色会变成另一个颜色。"""
    assert winbar._rgb(0x11, 0x22, 0x33) == 0x332211


def test_attach_registers_once_per_process(no_threads):
    proc = _FakeProc(4242)
    winbar.attach(proc, icon_path="wechat.png")
    winbar.attach(proc, icon_path="album.png")   # 同一个进程再来一次：不重复挂
    assert list(winbar._bars) == [4242]
    assert winbar._bars[4242].icon_path == "wechat.png"
    assert len(no_threads) == 1


def test_attach_records_pin_state(no_threads):
    winbar.attach(_FakeProc(7), icon_path=None, always_on_top=True)
    assert winbar._bars[7].pinned is True


def test_attach_defaults_not_pinned(no_threads):
    winbar.attach(_FakeProc(8))
    assert winbar._bars[8].pinned is False


def test_attach_without_pid_does_nothing(no_threads):
    winbar.attach(_FakeProc(0))
    assert winbar._bars == {}
    assert no_threads == []


def test_shutdown_cancels_and_forgets_every_bar(no_threads):
    winbar.attach(_FakeProc(11))
    winbar.attach(_FakeProc(12))
    bars = list(winbar._bars.values())
    winbar.shutdown()
    assert winbar._bars == {}
    assert all(b.cancelled for b in bars)


@pytest.fixture(autouse=True)
def _clean_style_state():
    """主窗口的染色记录也是模块级状态，不清干净会串到下一个用例。"""
    winbar._styled_hwnd = None
    winbar._styled_theme = None
    yield
    winbar._styled_hwnd = None
    winbar._styled_theme = None


class _FakeTime:
    def sleep(self, _seconds):
        pass


class _FakeWindowApi:
    def IsWindow(self, _hwnd):
        return True


def test_theme_switch_skips_window_chrome(monkeypatch):
    """切主题只该改 DWM 配色。

    「去图标 + 加无图标标记」要重算窗口边框，会把整块窗口（含里面的网页）重画一遍 ——
    启动时做一次没问题，但切主题要是也跟着做，用户就会看见窗口闪一下。
    """
    calls = []
    monkeypatch.setattr(winbar, "_ensure_api", lambda: None)
    monkeypatch.setattr(winbar, "_set_thread_dpi", lambda: None)
    monkeypatch.setattr(winbar, "_wait_main_window", lambda timeout=30: 100)
    monkeypatch.setattr(winbar, "time", _FakeTime())
    monkeypatch.setattr(winbar, "_user32", _FakeWindowApi())
    monkeypatch.setattr(winbar, "_style_main_frame",
                        lambda hwnd, theme, chrome=True: calls.append((hwnd, theme, chrome)))

    winbar._run_main_style("light")     # 启动：重活 + 染色
    winbar._run_main_style("dark")      # 切主题：只染色
    winbar._run_main_style("dark")      # 同一套主题再来一次：什么都不用做

    assert calls == [(100, "light", True),      # 窗口刚出现
                     (100, "light", True),      # WinForms 安定后的补染（重活仍要做一次）
                     (100, "dark", False)]      # 换主题：不碰窗口结构


def test_btn_at_hits_the_pin_button():
    """命中按闭区间算：右下角那一个像素也算在按钮内。"""
    bar = _FakeBar()
    bar.btns = [(10, 20, 40, 60)]
    assert winbar._btn_at(bar, 11, 30) == 0
    assert winbar._btn_at(bar, 39, 59) == 0                  # 右下角闭区间内
    assert winbar._btn_at(bar, 9, 30) == -1                  # 左侧标题文字区
    assert winbar._btn_at(bar, 40, 30) == -1                 # 右边界外（开区间）


def test_btn_at_ignores_vertical_outside():
    bar = _FakeBar()
    bar.btns = [(10, 20, 40, 60)]
    assert winbar._btn_at(bar, 20, 19) == -1
    assert winbar._btn_at(bar, 20, 60) == -1


def test_btn_at_without_layout_is_minus_one():
    """还没量出布局（全屏、窗口刚起来）时不能误判成按钮。"""
    assert winbar._btn_at(_FakeBar(), 0, 5) == -1


def _patch_client_rect(monkeypatch, rect):
    monkeypatch.setattr(winbar, "_client_screen_rect", lambda hwnd: rect)


def test_bar_actions_are_fewer_for_app_mirroring():
    """镜像应用开的是虚拟屏：返回 / 桌面 / 多任务 / 通知栏 / 控制中心打到手机真实界面上没意义。"""
    common = winbar._bar_actions(False)
    assert common == ("pin", "volume_up", "volume_down", "rotate_lock")
    assert winbar._bar_actions(True) == common + (
        "back", "home", "app_switch", "notifications", "control_center")


def test_layout_sticks_strip_to_right_edge(monkeypatch):
    """展开时贴画面右边缘、按钮竖着排开，而且个个都落在条带里。"""
    _patch_client_rect(monkeypatch, (100, 200, 700, 1000))
    bar = _FakeBar(target=1, scale=1.0)
    bar.desktop = True
    assert winbar._layout(bar) is True

    x1, y1, x2, y2 = bar.band
    assert x2 == 700                                        # 右端贴住画面
    assert (y1, y2) == (200 + winbar._STRIP_PAD, 1000 - winbar._STRIP_PAD)
    assert len(bar.btns) == len(bar.actions) == 9
    for bx1, by1, bx2, by2 in bar.btns:
        assert x1 <= bx1 < bx2 <= x2
        assert y1 <= by1 < by2 <= y2
    assert [b[1] for b in bar.btns] == sorted(b[1] for b in bar.btns)   # 自上而下


def test_layout_collapsed_leaves_no_buttons(monkeypatch):
    """收起态只留贴边一条细条，且一个按钮都没有——否则会点到看不见的东西。"""
    _patch_client_rect(monkeypatch, (100, 200, 700, 1000))
    bar = _FakeBar(target=1, scale=1.0)
    bar.open = False
    assert winbar._layout(bar) is True
    sx1, _, sx2, _ = bar.band
    assert sx2 - sx1 == winbar._STRIP_EDGE
    assert bar.btns == []
    assert bar.actions == []


def test_layout_gives_up_when_window_too_short(monkeypatch):
    """窗口太矮就别硬塞：宁可不摆，也别拿一排按钮糊住小半个画面。"""
    _patch_client_rect(monkeypatch, (0, 0, 600, 60))
    bar = _FakeBar(target=1, scale=1.0)
    bar.desktop = True
    assert winbar._layout(bar) is False


def test_layout_gives_up_without_client_rect(monkeypatch):
    """拿不到客户区（窗口刚起来 / 已经没了）：返回 False，退回定时器稍后重试。"""
    _patch_client_rect(monkeypatch, None)
    assert winbar._layout(_FakeBar(target=1)) is False


def test_hover_opens_immediately_and_collapses_after_delay(monkeypatch):
    """鼠标扫到右边缘就展开；移开后要等一会儿才收起，不然一抖就收、根本点不到。"""
    bar = _FakeBar(target=1, scale=1.0)
    bar.open = False
    bar.band = (600, 200, 700, 1000)

    monkeypatch.setattr(winbar, "_user32", _FakeCursorApi(680, 500))    # 判定区里
    winbar._update_open(bar)
    assert bar.open is True

    monkeypatch.setattr(winbar, "_user32", _FakeCursorApi(10, 500))     # 移开
    winbar._update_open(bar)
    assert bar.open is True, "刚移开不能立刻收，得留出点到按钮的时间"
    assert bar.close_at > 0

    bar.close_at = time.time() - 1                                      # 假装延迟已到
    winbar._update_open(bar)
    assert bar.open is False


def test_run_action_pin_stays_local(monkeypatch):
    """置顶是纯窗口操作，不该绕道去发 adb。"""
    forwarded = []
    winbar.set_action_handler(lambda serial, action: forwarded.append(action))
    monkeypatch.setattr(winbar, "_act_pin", lambda bar: forwarded.append("local-pin"))
    winbar._run_action(_FakeBar(target=1), "pin")
    assert forwarded == ["local-pin"]


def test_run_action_forwards_with_serial(monkeypatch):
    """其余动作要带着设备序列号转出去——发错设备的 adb 命令就麻烦了。"""
    seen = []
    winbar.set_action_handler(lambda serial, action: seen.append((serial, action)))
    bar = _FakeBar(target=1)
    bar.serial = "192.168.1.5:5555"
    winbar._run_action(bar, "back")

    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    assert seen == [("192.168.1.5:5555", "back")]


def test_run_action_without_handler_is_survivable(monkeypatch):
    """忘了注入执行器也只是点了没反应，不能把消息循环带崩。"""
    logged = []
    monkeypatch.setattr(winbar, "_log", logged.append)
    winbar._run_action(_FakeBar(target=1), "home")
    assert logged and "home" in logged[0]


def test_rotate_button_toggles_highlight(monkeypatch):
    """强制横屏的按钮要能看出当前是开着还是关着。"""
    winbar.set_action_handler(lambda serial, action: None)
    monkeypatch.setattr(winbar, "_user32", _FakePaintApi())
    bar = _FakeBar(target=1)
    winbar._run_action(bar, "rotate_lock")
    assert bar.rotate_on is True
    winbar._run_action(bar, "rotate_lock")
    assert bar.rotate_on is False


def test_icon_source_prefers_app_icon(tmp_path):
    png = tmp_path / "wechat.png"
    png.write_bytes(b"x")
    bar = _FakeBar()
    bar.icon_path = str(png)
    assert winbar._icon_source(bar) == str(png)


def test_icon_source_falls_back_when_missing():
    """图标路径不存在（或镜像桌面没有应用图标）时要能安全退回，绝不能抛。"""
    bar = _FakeBar()
    bar.icon_path = "C:/definitely/not/here.png"
    own = winbar.os.path.join(winbar.RES_DIR, "appicon.ico")
    assert winbar._icon_source(bar) == (own if winbar.os.path.exists(own) else None)
