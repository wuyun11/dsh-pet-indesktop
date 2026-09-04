# -*- coding: utf-8 -*-
"""专用通知通道（挂单/操作提醒）。

契约（与 Electron 版 dsh-pet 一致，stock-watch 推送页/监控脚本零改动复用）：
- 推送：外部脚本写入 notice.json：{"id": <唯一>, "text": "...", "ts": <毫秒>}
- 展示：轮询到 id 变化 → 宠物旁弹出带「确认收到」按钮的气泡
- 审计：点确认 → notice-ack.log 追加 {"id", "event":"ack", ...}；
       超时未确认 → 追加 {"id", "event":"expire", ...}（JSONL 追加）
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

LOG = logging.getLogger(__name__)

# 默认推送数据目录：<仓库根>/data/notice（可通过配置 notice.data_dir 覆盖）
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "notice"

_BUBBLE_STYLE = """
#notice-bubble {
  background: rgba(255, 255, 255, 0.95);
  border: 1px solid rgba(32, 49, 112, 0.35);
  border-radius: 10px;
}
#notice-bubble #notice-text {
  color: #203170;
  font-size: 13px;
}
#notice-bubble #notice-btn {
  background: #203170;
  color: #ffffff;
  border: none;
  border-radius: 5px;
  padding: 3px 10px;
  font-size: 12px;
}
#notice-bubble #notice-btn:hover {
  background: #2f74e0;
}
"""


class NoticeBubble(QFrame):
    """宠物旁置顶通知气泡：文案 + 「确认收到」按钮；超时未确认自动关闭并触发过期回调。

    独立 Tool 窗口（自带接收鼠标事件，不需要穿透翻转——这正是独立气泡相对
    透明主窗口内嵌 DOM 的优势）。
    """

    acked = Signal(str)  # notice id
    expired = Signal(str)  # notice id

    def __init__(self, parent: QWidget | None = None, *, duration_ms: int = 15000):
        super().__init__(parent)
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
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
        self._duration_ms = max(1000, int(duration_ms))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_expire)
        self.hide()

    # ------------------------------------------------------------ 对外
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

    # ------------------------------------------------------------ 事件
    def _on_ack(self) -> None:
        self._timer.stop()
        self.hide()
        self.acked.emit(self._notice_id)

    def _on_expire(self) -> None:
        self.hide()
        self.expired.emit(self._notice_id)

    # ------------------------------------------------------------ 定位
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
    """通知通道：轮询 notice.json，id 变化即弹带确认按钮的气泡；ack/expire 追加审计日志。"""

    def __init__(
        self,
        config,
        window_provider,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        settings = config.get("notice", {}) if isinstance(config.get("notice", {}), dict) else {}
        self._enabled = bool(settings.get("enabled", True))
        configured_dir = str(settings.get("data_dir") or "")
        self._data_dir = Path(configured_dir).expanduser() if configured_dir else _DEFAULT_DATA_DIR
        self._file = self._data_dir / "notice.json"
        self._ack_log = self._data_dir / "notice-ack.log"
        poll_sec = max(1, int(settings.get("poll_sec", 3) or 3))
        duration_ms = max(1000, int(settings.get("duration_ms", 15000) or 15000))
        self._window_provider = window_provider
        self._bubble = NoticeBubble(duration_ms=duration_ms)
        self._bubble.acked.connect(self._on_acked)
        self._bubble.expired.connect(self._on_expired)
        self._baseline = False
        self._last_id = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.setInterval(poll_sec * 1000)

    def start(self) -> None:
        if not self._enabled:
            return
        self._timer.start()
        self._poll()

    def stop(self) -> None:
        self._timer.stop()
        self._bubble.dismiss()

    # ------------------------------------------------------------ 轮询
    def _poll(self) -> None:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        notice_id = str(data.get("id") or "")
        text = str(data.get("text") or "").strip()
        if not notice_id or not text:
            return
        if not self._baseline:
            self._baseline = True  # 首次仅记基线：不重放启动前的旧通知
            self._last_id = notice_id
            return
        if notice_id == self._last_id:
            return
        self._last_id = notice_id
        win = self._window_provider()
        anchor = win.visible_content_rect() if win is not None else None
        LOG.info("notice 触发 id=%s", notice_id)
        self._bubble.show_notice(notice_id, text, int(data.get("ts") or 0), anchor)

    # ------------------------------------------------------------ 审计
    def _on_acked(self, notice_id: str) -> None:
        self._audit(notice_id, "ack")

    def _on_expired(self, notice_id: str) -> None:
        self._audit(notice_id, "expire")

    def _audit(self, notice_id: str, event: str) -> None:
        """追加 JSONL 审计：ack=用户确认 / expire=未确认过期。"""
        line = {
            "id": notice_id,
            "event": event,
            "text": self._bubble._notice_text,
            "ts": self._bubble._notice_ts,
            "receivedAt": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._ack_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            LOG.info("notice 审计 event=%s id=%s", event, notice_id)
        except OSError:
            LOG.exception("notice 审计日志写入失败")