"""深度搜索：进度状态与「停止搜索」。

不打真网络、不起扫描线程：只验证状态机本身——取消标记什么时候置、什么时候复位、
收尾文案怎么写。真正的扫描逻辑另有一套真机验证。
"""

import time

import pytest

from kuaitou import discover


@pytest.fixture(autouse=True)
def clean_state():
    """每条用例前后都把状态恢复原样，免得互相污染（模块里是全局单例状态）。"""
    def reset():
        discover._deep_cancel.clear()
        with discover._deep_lock:
            discover._deep_state.update({"running": False, "phase": "idle", "text": "",
                                         "found": [], "canceled": False})

    reset()
    yield
    reset()


def test_deep_stop_false_while_running_normally():
    assert discover._deep_stop(time.time() + 60) is False


def test_deep_stop_true_after_cancel():
    discover._deep_cancel.set()
    assert discover._deep_stop(time.time() + 60) is True


def test_deep_stop_true_when_deadline_passed():
    """时间预算用完和用户点停止是同一件事：扫描线程看到就收工。"""
    assert discover._deep_stop(time.time() - 1) is True


def test_cancel_when_idle_changes_nothing():
    st = discover.deep_cancel()
    assert st["running"] is False
    assert st["canceled"] is False
    assert discover._deep_cancel.is_set() is False       # 没在跑就别留下标记，免得下一轮一来就停


def test_cancel_marks_a_running_search():
    with discover._deep_lock:
        discover._deep_state.update({"running": True, "text": "端口扫描 1/9：192.168.1.5…"})
    st = discover.deep_cancel()
    assert discover._deep_cancel.is_set() is True
    assert "正在停止" in st["text"]                       # 界面据此告诉用户「收到，正在收尾」


def test_new_round_resets_the_cancel_flag(monkeypatch):
    """上一轮被停过，下一轮不能一上来就自己停。"""
    discover._deep_cancel.set()
    started = []
    monkeypatch.setattr(discover.threading, "Thread",
                        lambda **kw: started.append(kw) or _NullThread())
    st = discover.deep_discover()
    assert st["running"] is True and st["canceled"] is False
    assert discover._deep_cancel.is_set() is False
    assert started and started[0]["name"] == "deep-scan"


class _NullThread:
    """顶替扫描线程：只记下「要起线程」，不真的跑。"""

    def __init__(self, **_kw):
        pass

    def start(self):
        pass
