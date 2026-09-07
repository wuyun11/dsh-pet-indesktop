# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from pet.notice import NoticeChannel, _safe_detail_url


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _StubConfig:
    def __init__(self, data: dict): self._data = data
    def get(self, key: str, default=None): return self._data.get(key, default)


class _FakeWindow:
    def visible_content_rect(self) -> QRect: return QRect(0, 0, 800, 600)


def _write_notice(data_dir, payload: dict) -> None:
    (data_dir / "notice.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_audit(data_dir) -> list[dict]:
    path = data_dir / "notice-ack.log"
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_channel(tmp_path, *, enabled=True, duration_ms=1000):
    return NoticeChannel(_StubConfig({"notice": {"enabled": enabled, "poll_sec": 3, "duration_ms": duration_ms, "data_dir": str(tmp_path), "detail_base_url": "http://127.0.0.1:8084"}}), lambda: _FakeWindow())


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
