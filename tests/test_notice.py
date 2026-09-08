# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

from pet.notice import MIN_NOTICE_DURATION_MS, NoticeBubble, NoticeChannel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _StubConfig:
    def __init__(self, data: dict): self._data = data
    def get(self, key: str, default=None): return self._data.get(key, default)


class _FakeWindow:
    def visible_content_rect(self) -> QRect: return QRect(0, 0, 800, 600)


class _TrackingWindow(_FakeWindow):
    """支持位置监听的窗口桩：记录 listener，可模拟桌宠移动。"""

    def __init__(self, rect: QRect | None = None):
        self.rect = QRect(rect or QRect(0, 0, 800, 600))
        self.listeners = []

    def visible_content_rect(self) -> QRect:
        return QRect(self.rect)

    def add_position_listener(self, listener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_position_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)


def _read_audit(data_dir) -> list[dict]:
    path = data_dir / "notice-ack.log"
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_channel(tmp_path, *, enabled=True, duration_ms=15000, window=None, ack_callback_base=""):
    return NoticeChannel(_StubConfig({"notice": {"enabled": enabled, "duration_ms": duration_ms, "data_dir": str(tmp_path), "ack_callback_base": ack_callback_base}}), lambda: window if window is not None else _FakeWindow())


def _click_ack(channel) -> None: channel._bubble._button.click()


def test_config_duration_below_min_is_raised_to_minimum(tmp_path, app):
    channel = _make_channel(tmp_path, duration_ms=1000)
    channel.push_http("n1", "新")
    assert channel._bubble.isVisible()
    assert channel._bubble._timer.interval() == MIN_NOTICE_DURATION_MS
    channel.stop()


def test_config_duration_above_min_is_honoured(tmp_path, app):
    channel = _make_channel(tmp_path, duration_ms=30000)
    channel.push_http("n1", "新")
    assert channel._bubble._timer.interval() == 30000
    channel.stop()


def test_notice_bubble_registers_position_listener_and_follows_pet(tmp_path, app):
    win = _TrackingWindow(QRect(0, 0, 800, 600))
    channel = _make_channel(tmp_path, window=win)
    channel.push_http("n2", "新")
    assert channel._bubble.isVisible()
    # 展示后自动挂上桌宠位置监听（且只挂一次）
    assert len(win.listeners) == 1
    # 桌宠移动 → listener 用最新可见内容矩形重定位气泡
    placed = []
    bubble = channel._bubble
    original_place = bubble._place

    def spy_place(anchor):
        placed.append(QRect(anchor))
        original_place(anchor)

    bubble._place = spy_place
    win.rect = QRect(400, 300, 200, 150)
    win.listeners[0](win)
    assert len(placed) == 1 and placed[0] == QRect(400, 300, 200, 150)
    assert bubble.isVisible()
    channel.stop()
    # 停用通道后摘除监听，避免对已销毁桌宠窗口的悬空引用
    assert win.listeners == []


def test_bubble_reposition_follows_anchor_and_ignores_hidden_state(tmp_path, app):
    bubble = NoticeBubble()
    anchor = QRect(300, 320, 100, 80)  # 远离屏边，保证落点不受 availableGeometry 钳位
    bubble.show_notice("n", "新", 1, anchor)
    assert bubble.isVisible()
    before = bubble.pos()
    bubble.reposition(QRect(550, 420, 100, 80))  # 平移 (+250, +100)
    after = bubble.pos()
    assert after == before + QPoint(250, 100)
    bubble.dismiss()
    bubble.reposition(QRect(10, 10, 100, 80))  # 隐藏时不移动
    assert bubble.pos() == after


def test_push_http_shows_and_dedups_by_id(tmp_path, app):
    channel = _make_channel(tmp_path)
    channel.push_http("h1", "第一条")
    assert channel._bubble.isVisible()
    assert channel._bubble._notice_id == "h1"
    channel.push_http("h1", "第一条")
    assert channel._bubble._notice_id == "h1"
    assert _read_audit(tmp_path) == []
    channel.push_http("h2", "第二条")
    assert channel._bubble._notice_id == "h2"
    lines = _read_audit(tmp_path)
    assert lines[0]["id"] == "h1" and lines[0]["event"] == "expire"
    channel.stop()


def test_push_http_ignored_when_disabled(tmp_path, app):
    channel = _make_channel(tmp_path, enabled=False)
    channel.push_http("h1", "x")
    assert not channel._bubble.isVisible()
    channel.stop()


def test_push_http_ignores_empty_fields(tmp_path, app):
    channel = _make_channel(tmp_path)
    channel.push_http("", "x")
    channel.push_http("h1", "   ")
    assert not channel._bubble.isVisible()
    channel.stop()


def test_ack_fires_http_callback_with_id(tmp_path, app):
    fired = threading.Event()
    calls = []
    channel = _make_channel(tmp_path, ack_callback_base="http://127.0.0.1:4091")

    def recorder(url: str) -> None:
        calls.append(url)
        fired.set()

    channel._post_ack = recorder
    channel.push_http("n2", "新")
    _click_ack(channel)
    assert fired.wait(2.0)
    assert calls == ["http://127.0.0.1:4091/notify/n2/ack"]
    lines = _read_audit(tmp_path)
    assert lines[0]["event"] == "ack" and lines[0]["id"] == "n2"
    channel.stop()


def test_ack_without_callback_base_only_audits(tmp_path, app):
    channel = _make_channel(tmp_path)
    channel.push_http("n2", "新")
    _click_ack(channel)
    lines = _read_audit(tmp_path)
    assert lines[0]["event"] == "ack" and lines[0]["id"] == "n2"
    channel.stop()


def test_ack_callback_failure_keeps_local_audit(tmp_path, app):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    channel = _make_channel(tmp_path, ack_callback_base=f"http://127.0.0.1:{closed_port}")
    channel.push_http("n2", "新")
    _click_ack(channel)
    lines = _read_audit(tmp_path)
    assert lines[0]["event"] == "ack" and lines[0]["id"] == "n2"
    time.sleep(0.2)  # 后台回调线程失败只记日志，不影响本地审计
    channel.stop()


def test_ack_callback_reaches_backend_with_post(tmp_path, app):
    captured = {}
    received = threading.Event()

    class _Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            captured["path"] = self.path
            self.send_response(200)
            self.end_headers()
            received.set()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    receiver.daemon_threads = True
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    try:
        port = receiver.server_address[1]
        channel = _make_channel(tmp_path, ack_callback_base=f"http://127.0.0.1:{port}")
        channel.push_http("n2", "新")
        _click_ack(channel)
        assert received.wait(3.0)
        assert captured["path"] == "/notify/n2/ack"
        channel.stop()
    finally:
        receiver.shutdown()
        receiver.server_close()