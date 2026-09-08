# -*- coding: utf-8 -*-
"""桌宠本机 HTTP 接口：POST /api/notice 接收 {id, text} 并交给显示逻辑。

- 只监听 loopback，不对局域网开放；非回环来源一律拒绝。
- HTTP 处理在独立线程（ThreadingHTTPServer + daemon 线程），不阻塞桌宠交互；
  实际展示通过 Qt queued signal 排队回 GUI 线程。
- 响应是接收结果，不是已读回执：200 = 已交给显示逻辑；400 = 正文无效；
  503 = 通知功能关闭或暂不可用。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Signal

LOG = logging.getLogger(__name__)

NOTICE_API_PATH = "/api/notice"
MAX_BODY_BYTES = 64 * 1024


class NoticeHttpError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class NoticeHttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dsh-pet-notice/1.0"
    timeout = 10

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("notice-api %s - %s", self.client_address[0], fmt % args)

    def do_POST(self) -> None:  # noqa: N802 - HTTP 动词方法名遵循 stdlib 约定
        if not is_loopback_address(self.client_address[0]):
            self._reply(403, {"ok": False, "reason": "loopback only"})
            return
        if self.path.split("?", 1)[0] != NOTICE_API_PATH:
            self._reply(404, {"ok": False, "reason": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._reply(400, {"ok": False, "reason": "content-type must be application/json"})
            return
        try:
            data = self._read_json()
        except NoticeHttpError as exc:
            self._reply(exc.status, {"ok": False, "reason": exc.reason})
            return
        notice_id = str(data.get("id") or "").strip()
        text = str(data.get("text") or "").strip()
        if not notice_id or not text:
            self._reply(400, {"ok": False, "reason": "id and text are required"})
            return
        dispatcher = getattr(self.server, "dispatcher", None)
        if dispatcher is None or not dispatcher.is_enabled():
            self._reply(503, {"ok": False, "reason": "notice disabled"})
            return
        # 只入队，不等待展示完成：响应是接收结果
        dispatcher.notice_received.emit(notice_id, text)
        self._reply(200, {"ok": True})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise NoticeHttpError(400, "missing content-length")  # noqa: B904
        if length <= 0 or length > MAX_BODY_BYTES:
            raise NoticeHttpError(400, "invalid body size")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise NoticeHttpError(400, "invalid json")  # noqa: B904
        if not isinstance(data, dict):
            raise NoticeHttpError(400, "body must be a json object")
        return data

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NoticeDispatcher(QObject):
    """HTTP 线程 → GUI 线程的桥：把收到的通知排队到显示逻辑。"""

    notice_received = Signal(str, str)

    def __init__(self, *, enabled=None):
        super().__init__()
        self._enabled_check = enabled if callable(enabled) else (lambda: True)

    def is_enabled(self) -> bool:
        return bool(self._enabled_check())


class NoticeHttpServer:
    """在后台线程运行的本机 notice API 服务；占用端口失败时直接抛 OSError。"""

    def __init__(self, host: str, port: int, dispatcher: NoticeDispatcher):
        server = ThreadingHTTPServer((host, port), NoticeHttpHandler)
        server.daemon_threads = True
        server.dispatcher = dispatcher
        self._server = server
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="notice-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread = None


def is_loopback_address(address: str) -> bool:
    """判断来源地址是否回环（含 IPv4-mapped ::ffff:127.0.0.1）。"""
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def validate_api_host(host: str) -> None:
    """API 只允许监听本机回环地址，不对局域网开放。"""
    if str(host).strip() in {"127.0.0.1", "localhost", "::1"}:
        return
    raise ValueError("notice.api_host 必须是本机回环地址")