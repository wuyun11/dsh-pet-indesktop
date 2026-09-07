# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

from pet.notice import MIN_NOTICE_DURATION_MS, NoticeBubble, NoticeChannel, _safe_detail_url


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


def _write_notice(data_dir, payload: dict) -> None:
    (data_dir / "notice.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_audit(data_dir) -> list[dict]:
    path = data_dir / "notice-ack.log"
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_channel(tmp_path, *, enabled=True, duration_ms=15000, window=None):
    return NoticeChannel(_StubConfig({"notice": {"enabled": enabled, "poll_sec": 3, "duration_ms": duration_ms, "data_dir": str(tmp_path), "detail_base_url": "http://127.0.0.1:8084"}}), lambda: window if window is not None else _FakeWindow())


def _show_second_notice(channel, tmp_path, payload: dict) -> None:
    """基线 poll 后写入并展示一条新 notice。"""
    _write_notice(tmp_path, {"id": "n1", "text": "旧", "ts": 1})
    channel._poll()
    _write_notice(tmp_path, payload)
    channel._poll()


def _click_ack(channel) -> None: channel._bubble._button.click()


def test_payload_duration_below_min_is_raised_to_minimum(tmp_path, app):
    channel = _make_channel(tmp_path)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2, "duration_ms": 2000})
    assert channel._bubble.isVisible()
    assert channel._bubble._timer.interval() == MIN_NOTICE_DURATION_MS
    channel.stop()


def test_payload_duration_above_min_is_honoured(tmp_path, app):
    channel = _make_channel(tmp_path)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2, "duration_ms": 30000})
    assert channel._bubble._timer.interval() == 30000
    channel.stop()


def test_payload_without_duration_uses_config_duration(tmp_path, app):
    channel = _make_channel(tmp_path, duration_ms=20000)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2})
    assert channel._bubble._timer.interval() == 20000
    channel.stop()


def test_config_duration_below_min_is_raised_to_minimum(tmp_path, app):
    channel = _make_channel(tmp_path, duration_ms=1000)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2})
    assert channel._bubble._timer.interval() == MIN_NOTICE_DURATION_MS
    channel.stop()


def test_invalid_payload_duration_is_ignored(tmp_path, app):
    channel = _make_channel(tmp_path, duration_ms=20000)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2, "duration_ms": "abc"})
    assert channel._bubble._timer.interval() == 20000
    channel.stop()


def test_notice_bubble_registers_position_listener_and_follows_pet(tmp_path, app):
    win = _TrackingWindow(QRect(0, 0, 800, 600))
    channel = _make_channel(tmp_path, window=win)
    _show_second_notice(channel, tmp_path, {"id": "n2", "text": "新", "ts": 2})
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


def _click_ack(channel) -> None: channel._bubble._button.click()


def test_detail_url_only_allows_local_decision_center():
    assert _safe_detail_url("http://127.0.0.1:8084/decisions/d1", "http://127.0.0.1:8084")
    assert _safe_detail_url("https://example.com/decisions/d1", "http://127.0.0.1:8084") == ""
    assert _safe_detail_url("http://127.0.0.1:8084/jobs", "http://127.0.0.1:8084") == ""
    assert _safe_detail_url("file:///tmp/x", "http://127.0.0.1:8084") == ""


def test_first_poll_is_baseline_only(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "旧通知", "ts": 100})
    channel = _make_channel(tmp_path); channel._poll()
    assert channel._baseline is True and not channel._bubble.isVisible()
    channel.stop()


def test_allowed_detail_button_is_visible_and_ack_is_independent(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "旧", "ts": 1})
    channel = _make_channel(tmp_path); channel._poll()
    _write_notice(tmp_path, {"id": "n2", "text": "建议有变化", "ts": 2, "detail_url": "http://127.0.0.1:8084/decisions/d2"})
    channel._poll()
    assert channel._bubble._detail_button.isVisible()
    assert _read_audit(tmp_path) == []
    _click_ack(channel)
    lines = _read_audit(tmp_path)
    assert lines[0]["event"] == "ack" and lines[0]["detail_url"].endswith("/decisions/d2")
    channel.stop()


def test_external_detail_url_is_not_exposed(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "旧", "ts": 1})
    channel = _make_channel(tmp_path); channel._poll()
    _write_notice(tmp_path, {"id": "n2", "text": "坏链接", "ts": 2, "detail_url": "https://example.com/decisions/d2"})
    channel._poll()
    assert not channel._bubble._detail_button.isVisible()
    channel.stop()


def test_replacement_audits_old_notice_as_expire(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "hi", "ts": 1})
    channel = _make_channel(tmp_path); channel._poll()
    _write_notice(tmp_path, {"id": "n2", "text": "第一条", "ts": 2}); channel._poll()
    _write_notice(tmp_path, {"id": "n3", "text": "第二条", "ts": 3}); channel._poll()
    lines = _read_audit(tmp_path)
    assert lines[0]["id"] == "n2" and lines[0]["event"] == "expire"
    channel.stop()


def test_no_file_or_bad_json_is_silent(tmp_path, app):
    channel = _make_channel(tmp_path); channel._poll(); assert channel._baseline is False
    (tmp_path / "notice.json").write_text("{broken", encoding="utf-8"); channel._poll(); assert channel._baseline is False
    channel.stop()
