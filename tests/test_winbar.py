"""原生标题栏增强：纯逻辑部分（登记表、按钮命中、布局避让系统按钮）。

真实的窗口 / DWM / GDI 绘制不在这里测 —— 那要靠真机投屏实测。测试只保证不依赖
Windows 消息循环的那几段算术是对的（它们错了用户就点不中按钮、或者按钮盖到系统按钮上），
以及「哪件事只该做一次」这类调度判断。
"""

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
    """_btn_at / _layout 只关心 target、scale、btns、band，不必造真的 _Bar。"""

    def __init__(self, target=None, scale=1.0):
        self.target = target
        self.scale = scale
        self.btns = []
        self.band = None


class _FakeUser32:
    """_layout 要先看窗口样式里有没有 WS_CAPTION，测试里给个固定答案。"""

    def __init__(self, style=winbar._WS_CAPTION):
        self.style = style

    def GetWindowLongW(self, hwnd, index):
        assert index == winbar._GWL_STYLE
        return self.style


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
    """现在只剩一个「置顶」按钮，命中范围仍按闭区间算（右下角也算按钮内）。"""
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


def test_layout_places_button_just_left_of_system_buttons(monkeypatch):
    """按钮必须紧挨在系统按钮左边，右边一格都不重叠。"""
    boxes = [
        winbar.wintypes.RECT(1000, 20, 1094, 77),
        winbar.wintypes.RECT(1100, 20, 1194, 77),
        winbar.wintypes.RECT(1200, 20, 1294, 77),
    ]
    monkeypatch.setattr(winbar, "_user32", _FakeUser32())
    monkeypatch.setattr(winbar, "_sys_buttons", lambda hwnd: boxes)
    bar = _FakeBar(target=1, scale=1.0)
    assert winbar._layout(bar) is True

    assert len(bar.btns) == 1
    btn = bar.btns[0]
    assert btn[2] == 1000                                    # 右端正好顶到系统按钮左沿
    assert (btn[1], btn[3]) == (20, 77)                      # 上下与系统按钮齐平
    assert bar.band == (btn[0], 20, 1000, 77)


def test_layout_uses_system_button_width_for_look_and_feel(monkeypatch):
    """按钮宽度照抄系统按钮，看起来才像一家的。"""
    boxes = [winbar.wintypes.RECT(1000, 20, 1094, 77),
             winbar.wintypes.RECT(1100, 20, 1194, 77),
             winbar.wintypes.RECT(1200, 20, 1294, 77)]
    monkeypatch.setattr(winbar, "_user32", _FakeUser32())
    monkeypatch.setattr(winbar, "_sys_buttons", lambda hwnd: boxes)
    bar = _FakeBar(target=1, scale=1.0)
    winbar._layout(bar)
    x1, _, x2, _ = bar.btns[0]
    assert (x2 - x1) == (1294 - 1000) // 3                   # 98，等于系统按钮均宽


def test_layout_returns_false_without_titlebar(monkeypatch):
    """量不到系统按钮（窗口刚起来等）：布局失败，退回去重试。"""
    monkeypatch.setattr(winbar, "_user32", _FakeUser32())
    monkeypatch.setattr(winbar, "_sys_buttons", lambda hwnd: [])
    bar = _FakeBar(target=1)
    assert winbar._layout(bar) is False
    assert bar.btns == []
    assert bar.band is None


def test_layout_returns_false_when_fullscreen(monkeypatch):
    """全屏时窗口没有 WS_CAPTION：直接不摆按钮，也不能去问系统（那边会回一堆坏矩形）。"""
    called = []
    monkeypatch.setattr(winbar, "_user32", _FakeUser32(style=winbar._WS_POPUP))
    monkeypatch.setattr(winbar, "_sys_buttons",
                        lambda hwnd: called.append(hwnd) or [])
    bar = _FakeBar(target=1)
    assert winbar._layout(bar) is False
    assert called == []                                      # 压根没问系统


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
