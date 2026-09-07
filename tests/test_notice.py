# -*- coding: utf-8 -*-
"""NoticeChannel 专用通知通道单测（文件驱动：基线 / 展示 / ack / expire / 顶替审计）。"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from pet.notice import NoticeChannel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _StubConfig:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class _FakeWindow:
    def visible_content_rect(self) -> QRect:
        return QRect(0, 0, 800, 600)


def _write_notice(data_dir, payload: dict) -> None:
    (data_dir / "notice.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _read_audit(data_dir) -> list[dict]:
    log_path = data_dir / "notice-ack.log"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_channel(tmp_path, *, enabled=True, duration_ms=1000):
    cfg = _StubConfig(
        {
            "notice": {
                "enabled": enabled,
                "poll_sec": 3,
                "duration_ms": duration_ms,
                "data_dir": str(tmp_path),
            }
        }
    )
    return NoticeChannel(cfg, lambda: _FakeWindow())


def _click_ack(channel) -> None:
    channel._bubble._button.click()


def test_disabled_channel_does_not_poll(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "hi", "ts": 1})
    channel = _make_channel(tmp_path, enabled=False)
    channel.start()
    assert not channel._bubble.isVisible()
    assert channel._baseline is False
    assert _read_audit(tmp_path) == []
    channel.stop()


def test_string_false_disables_channel(tmp_path, app):
    cfg = _StubConfig({"notice": {"enabled": "false", "data_dir": str(tmp_path)}})
    channel = NoticeChannel(cfg, lambda: _FakeWindow())
    channel.start()
    assert channel._enabled is False
    assert channel._timer.isActive() is False
    channel.stop()


def test_first_poll_is_baseline_only(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "旧通知", "ts": 100})
    channel = _make_channel(tmp_path)
    channel._poll()
    assert channel._baseline is True
    assert channel._last_id == "n1"
    assert not channel._bubble.isVisible()
    assert _read_audit(tmp_path) == []
    channel._poll()  # 同一 id 不重复展示
    assert not channel._bubble.isVisible()
    channel.stop()


def test_ack_audits_and_hides_bubble(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "hi", "ts": 100})
    channel = _make_channel(tmp_path)
    channel._poll()  # 基线 n1
    _write_notice(tmp_path, {"id": "n2", "text": "挂单成交", "ts": 200})
    channel._poll()
    assert channel._bubble.isVisible()
    _click_ack(channel)
    assert not channel._bubble.isVisible()
    lines = _read_audit(tmp_path)
    assert len(lines) == 1
    assert lines[0]["id"] == "n2"
    assert lines[0]["event"] == "ack"
    assert lines[0]["text"] == "挂单成交"
    assert lines[0]["ts"] == 200
    assert lines[0]["receivedAt"]
    channel.stop()


def test_timeout_audits_expire(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "hi", "ts": 1})
    channel = _make_channel(tmp_path, duration_ms=1000)
    channel._poll()  # 基线 n1
    _write_notice(tmp_path, {"id": "n2", "text": "注意", "ts": 2})
    channel._poll()
    assert channel._bubble.isVisible()
    assert channel._bubble._timer.isActive()  # 超时定时器已挂上
    channel._bubble._on_expire()  # 等价超时：隐藏并触发 expired 信号 → 审计
    assert not channel._bubble.isVisible()
    lines = _read_audit(tmp_path)
    assert len(lines) == 1
    assert lines[0]["id"] == "n2"
    assert lines[0]["event"] == "expire"
    channel.stop()


def test_replacement_audits_old_notice_as_expire(tmp_path, app):
    _write_notice(tmp_path, {"id": "n1", "text": "hi", "ts": 1})
    channel = _make_channel(tmp_path)
    channel._poll()  # 基线 n1
    _write_notice(tmp_path, {"id": "n2", "text": "第一条", "ts": 2})
    channel._poll()
    assert channel._bubble.isVisible()
    _write_notice(tmp_path, {"id": "n3", "text": "第二条", "ts": 3})
    channel._poll()  # 顶替：n2 先按 expire 收口
    assert channel._bubble.isVisible()
    lines = _read_audit(tmp_path)
    assert len(lines) == 1
    assert lines[0]["id"] == "n2"
    assert lines[0]["event"] == "expire"
    assert lines[0]["text"] == "第一条"
    assert lines[0]["ts"] == 2
    _click_ack(channel)  # 顶替后的新气泡仍可正常 ack
    lines = _read_audit(tmp_path)
    assert len(lines) == 2
    assert lines[1]["id"] == "n3"
    assert lines[1]["event"] == "ack"
    channel.stop()


def test_no_file_or_bad_json_is_silent(tmp_path, app):
    channel = _make_channel(tmp_path)
    channel._poll()  # notice.json 不存在 → 静默
    assert channel._baseline is False
    (tmp_path / "notice.json").write_text("{broken", encoding="utf-8")
    channel._poll()  # 坏 JSON → 静默
    assert channel._baseline is False
    channel.stop()
