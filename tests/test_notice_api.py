# -*- coding: utf-8 -*-
"""POST /api/notice 本机接口测试：接收结果语义、id 增量、禁用 503、回环约束。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from pet.notice import NoticeChannel
from pet.notice_api import (
    NoticeDispatcher,
    NoticeHttpServer,
    is_loopback_address,
    validate_api_host,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _StubConfig:
    def __init__(self, data: dict): self._data = data
    def get(self, key: str, default=None): return self._data.get(key, default)


class _FakeWindow:
    def visible_content_rect(self) -> QRect: return QRect(0, 0, 800, 600)


def _make_channel(tmp_path, *, enabled=True):
    cfg = {"notice": {"enabled": enabled, "duration_ms": 15000,
                      "data_dir": str(tmp_path), "ack_callback_base": ""}}
    return NoticeChannel(_StubConfig(cfg), lambda: _FakeWindow())


def _post(url: str, payload, *, content_type: str = "application/json"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _start(channel):
    dispatcher = NoticeDispatcher(enabled=channel.is_enabled)
    dispatcher.notice_received.connect(channel.push_http)
    server = NoticeHttpServer("127.0.0.1", 0, dispatcher)
    server.start()
    return server, dispatcher


def test_post_notice_returns_ok_and_displays_bubble(tmp_path, app):
    channel = _make_channel(tmp_path)
    server, _ = _start(channel)
    try:
        status, body = _post(
            f"http://127.0.0.1:{server.port}/api/notice",
            {"id": "task-1", "text": "任务完成\n请查看"},
        )
        assert status == 200 and body == {"ok": True}
        app.processEvents()
        assert channel._bubble.isVisible()
        assert channel._bubble._notice_id == "task-1"
    finally:
        server.stop()
        channel.stop()


def test_same_id_repeat_does_not_re_pop(tmp_path, app):
    channel = _make_channel(tmp_path)
    server, _ = _start(channel)
    try:
        url = f"http://127.0.0.1:{server.port}/api/notice"
        _post(url, {"id": "task-1", "text": "第一条"})
        app.processEvents()
        assert channel._bubble._notice_id == "task-1"
        _post(url, {"id": "task-1", "text": "第一条"})
        app.processEvents()
        assert channel._bubble._notice_id == "task-1"
        assert not (tmp_path / "notice-ack.log").exists()
        _post(url, {"id": "task-2", "text": "第二条"})
        app.processEvents()
        assert channel._bubble._notice_id == "task-2"
        lines = [
            json.loads(line)
            for line in (tmp_path / "notice-ack.log").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert lines[0]["id"] == "task-1" and lines[0]["event"] == "expire"
    finally:
        server.stop()
        channel.stop()


def test_invalid_body_returns_400(tmp_path, app):
    channel = _make_channel(tmp_path)
    server, _ = _start(channel)
    try:
        url = f"http://127.0.0.1:{server.port}/api/notice"
        status, body = _post(url, {"text": "缺 id"})
        assert status == 400 and body["ok"] is False
        status, body = _post(url, {"id": "x"})
        assert status == 400 and body["ok"] is False
        status, body = _post(url, {"id": "x", "text": ""})
        assert status == 400 and body["ok"] is False
    finally:
        server.stop()
        channel.stop()


def test_wrong_content_type_returns_400(tmp_path, app):
    channel = _make_channel(tmp_path)
    server, _ = _start(channel)
    try:
        status, _ = _post(
            f"http://127.0.0.1:{server.port}/api/notice",
            {"id": "x", "text": "y"},
            content_type="text/plain",
        )
        assert status == 400
    finally:
        server.stop()
        channel.stop()


def test_unknown_path_returns_404(tmp_path, app):
    channel = _make_channel(tmp_path)
    server, _ = _start(channel)
    try:
        status, _ = _post(f"http://127.0.0.1:{server.port}/other", {"id": "x", "text": "y"})
        assert status == 404
    finally:
        server.stop()
        channel.stop()


def test_disabled_channel_returns_503(tmp_path, app):
    channel = _make_channel(tmp_path, enabled=False)
    server, _ = _start(channel)
    try:
        status, body = _post(
            f"http://127.0.0.1:{server.port}/api/notice", {"id": "x", "text": "y"}
        )
        assert status == 503 and body["ok"] is False
        app.processEvents()
        assert not channel._bubble.isVisible()
    finally:
        server.stop()
        channel.stop()


def test_loopback_helpers():
    assert is_loopback_address("127.0.0.1")
    assert is_loopback_address("::1")
    assert is_loopback_address("::ffff:127.0.0.1")
    assert not is_loopback_address("192.168.1.5")
    validate_api_host("127.0.0.1")
    validate_api_host("localhost")
    validate_api_host("::1")
    with pytest.raises(ValueError):
        validate_api_host("0.0.0.0")