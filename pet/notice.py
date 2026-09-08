# -*- coding: utf-8 -*-
"""专用通知通道（挂单/操作提醒）：只走本机 HTTP 接收 {id, text} 并弹气泡。

- `POST /api/notice` 由 pet/notice_api.py 提供，本模块只负责展示与审计。
- id 增量：同 id 不重复弹；新 id 顶替当前气泡（旧 id 计入 expire 审计）。
- 气泡外观、显示时长、关闭交互沿用桌宠现有实现，不接收调用方控制参数。
- 点「确认收到」→ 本地审计 + 尽力回调后端 `{ack_callback_base}/notify/{id}/ack`。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

LOG = logging.getLogger(__name__)
MIN_NOTICE_DURATION_MS = 15000  # notice 最短展示时长：低于它的传入时长一律抬到默认时长


def _clamp_notice_duration(value: object) -> int:
    """把配置的毫秒时长抬到最小展示时长下限。"""
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return MIN_NOTICE_DURATION_MS
    return max(MIN_NOTICE_DURATION_MS, ms)


def _default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        from .config import APP_DIR_NAME, _default_base
        return _default_base() / APP_DIR_NAME / "notice"
    return Path(__file__).resolve().parent.parent / "data" / "notice"


def _post_ack_callback(url: str) -> None:
    """尽力一次的 ack 回调：用户点「确认收到」后通知后端；失败只记日志，不做重试队列。"""
    try:
        request = urllib.request.Request(
            url,
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            response.read()
        LOG.info("notice ack 回调成功: %s", url)
    except Exception:  # noqa: BLE001 - 回调失败不影响本地审计
        LOG.warning("notice ack 回调失败: %s", url, exc_info=True)


_BUBBLE_STYLE = """
#notice-bubble { background: rgba(255,255,255,0.95); border: 1px solid rgba(32,49,112,0.35); border-radius: 10px; }
#notice-bubble #notice-text { color: #203170; font-size: 13px; }
#notice-bubble QPushButton { border: none; border-radius: 5px; padding: 3px 10px; font-size: 12px; }
#notice-bubble #notice-btn { background: #203170; color: #ffffff; }
#notice-bubble QPushButton:hover { background: #2f74e0; color: #ffffff; }
"""


class NoticeBubble(QFrame):
    """宠物旁通知气泡：确认收到。"""

    acked = Signal(str)
    expired = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, duration_ms: int = MIN_NOTICE_DURATION_MS):
        super().__init__(parent)
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if hasattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow"):
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setObjectName("notice-bubble")
        self.setStyleSheet(_BUBBLE_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        self._text = QLabel(self)
        self._text.setObjectName("notice-text")
        self._text.setWordWrap(True)
        self._text.setMaximumWidth(320)
        layout.addWidget(self._text)

        row = QHBoxLayout()
        row.addStretch(1)
        self._button = QPushButton("确认收到", self)
        self._button.setObjectName("notice-btn")
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.clicked.connect(self._on_ack)
        row.addWidget(self._button)
        layout.addLayout(row)

        self._notice_id = ""
        self._notice_text = ""
        self._notice_ts = 0
        self._duration_ms = _clamp_notice_duration(duration_ms)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_expire)
        self.hide()

    def show_notice(self, notice_id: str, text: str, ts: int, anchor_rect: QRect | None) -> None:
        self._notice_id = notice_id
        self._notice_text = text
        self._notice_ts = ts
        self._text.setText(text)
        self.adjustSize()
        self._place(anchor_rect)
        self.show()
        self.raise_()
        if self._timer.isActive():
            self._timer.stop()
        self._timer.start(self._duration_ms)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()

    def reposition(self, anchor_rect: QRect | None) -> None:
        """桌宠移动/拖动后重新贴到角色旁（隐藏时不动）。"""
        if self.isVisible():
            self._place(anchor_rect)

    def _on_ack(self) -> None:
        self._timer.stop()
        self.hide()
        self.acked.emit(self._notice_id)

    def _on_expire(self) -> None:
        self.hide()
        self.expired.emit(self._notice_id)

    def _place(self, anchor_rect: QRect | None) -> None:
        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        w, h = self.width(), self.height()
        if anchor_rect is not None and anchor_rect.width() > 0 and anchor_rect.height() > 0:
            x = anchor_rect.center().x() - w // 2
            y = anchor_rect.top() - h - 12
        else:
            x = area.center().x() - w // 2
            y = area.top() + 24
        x = max(area.left() + 8, min(x, area.right() - w - 8))
        y = max(area.top() + 8, min(y, area.bottom() - h - 8))
        self.move(x, y)


class NoticeChannel(QObject):
    """HTTP 通知通道：收 {id, text}，id 增量展示并记录 ack/expire。"""

    def __init__(self, config, window_provider, parent: QObject | None = None) -> None:
        super().__init__(parent)
        settings = config.get("notice", {}) if isinstance(config.get("notice", {}), dict) else {}
        raw_enabled = settings.get("enabled", True)
        self._enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).lower() in ("1", "true", "yes")
        configured_dir = str(settings.get("data_dir") or "")
        self._data_dir = Path(configured_dir).expanduser() if configured_dir else _default_data_dir()
        self._ack_log = self._data_dir / "notice-ack.log"
        self._ack_callback_base = str(settings.get("ack_callback_base") or "").strip().rstrip("/")
        if self._ack_callback_base:
            base = urlsplit(self._ack_callback_base)
            if base.scheme not in {"http", "https"} or not base.netloc:
                raise ValueError("notice.ack_callback_base 必须是合法的 http(s) 地址")
        duration_ms = _clamp_notice_duration(settings.get("duration_ms", MIN_NOTICE_DURATION_MS))
        self._window_provider = window_provider
        self._bubble = NoticeBubble(duration_ms=duration_ms)
        self._bubble.acked.connect(self._on_acked)
        self._bubble.expired.connect(self._on_expired)
        self._follow_win = None
        self._follow_cb = None
        self._http_last_id = ""
        self._post_ack = _post_ack_callback
        self._shown_id = ""
        self._shown_text = ""
        self._shown_ts = 0

    def stop(self) -> None:
        self._detach_position_listener()
        self._bubble.dismiss()

    def is_enabled(self) -> bool:
        return self._enabled

    def push_http(self, notice_id: str, text: str) -> None:
        """HTTP 通道：收 {id, text}，按 id 增量逻辑展示（同 id 不重复弹）。"""
        notice_id = str(notice_id or "").strip()
        text = str(text or "").strip()
        if not self._enabled or not notice_id or not text:
            return
        if notice_id == self._http_last_id:
            return
        self._http_last_id = notice_id
        self._display_notice(notice_id, text, int(time.time()))

    def _display_notice(self, notice_id: str, text: str, ts: int) -> None:
        """展示路径：顶替旧气泡并审计 expire，再展示新气泡。"""
        if self._bubble.isVisible() and self._shown_id:
            LOG.info("notice 顶替 id=%s", self._shown_id)
            self._audit(self._shown_id, "expire")
        win = self._window_provider()
        anchor = win.visible_content_rect() if win is not None else None
        LOG.info("notice 触发 id=%s", notice_id)
        self._bubble.show_notice(notice_id, text, ts, anchor)
        if win is not None:
            self._attach_position_listener(win)
        self._shown_id = notice_id
        self._shown_text = text
        self._shown_ts = ts

    def _attach_position_listener(self, win) -> None:
        """气泡展示后挂上桌宠位置监听：拖动/移动时让气泡贴着角色走。"""
        adder = getattr(win, "add_position_listener", None)
        if not callable(adder):
            return
        if self._follow_win is win and self._follow_cb is not None:
            return
        self._detach_position_listener()
        self._follow_win = win
        callback = getattr(self, "_on_pet_position_changed")
        self._follow_cb = callback
        adder(callback)

    def _detach_position_listener(self) -> None:
        win, callback = self._follow_win, self._follow_cb
        self._follow_win = None
        self._follow_cb = None
        if win is None or callback is None:
            return
        remover = getattr(win, "remove_position_listener", None)
        if callable(remover):
            remover(callback)

    def _on_pet_position_changed(self, pet) -> None:
        """桌宠位置同步帧：用最新可见内容矩形重定位气泡。"""
        getter = getattr(pet, "visible_content_rect", None)
        if callable(getter):
            self._bubble.reposition(getter())

    def _on_acked(self, notice_id: str) -> None:
        self._audit(notice_id, "ack")
        self._fire_ack_callback(notice_id)

    def _on_expired(self, notice_id: str) -> None:
        self._audit(notice_id, "expire")

    def _fire_ack_callback(self, notice_id: str) -> None:
        """在本地审计之外，尽力通知后端一次；失败只记日志，不重试、不影响审计。"""
        if not notice_id or not self._ack_callback_base:
            return
        url = f"{self._ack_callback_base}/notify/{notice_id}/ack"
        threading.Thread(target=self._post_ack, args=(url,), daemon=True, name="notice-ack-callback").start()

    def _audit(self, notice_id: str, event: str) -> None:
        if not notice_id:
            return
        line = {"id": notice_id, "event": event, "text": self._shown_text, "ts": self._shown_ts, "receivedAt": datetime.now().isoformat(timespec="seconds")}
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._ack_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            LOG.info("notice 审计 event=%s id=%s", event, notice_id)
        except OSError:
            LOG.exception("notice 审计日志写入失败")