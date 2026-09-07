"""桌面懸浮儀表板（PySide6 / Qt）。

接收 hub 狀態並即時顯示：
    python dashboard_float.py <port>
每 2 秒 poll http://127.0.0.1:<port>/api，顯示 hub 自身狀態與各 job 卡片。

Qt 原生 per-pixel alpha 合成：面板半透明磨砂、文字清晰無鋸齒、無細縫點擊穿透、
整窗可自由拖曳，Win11 支援 DWM Acrylic 毛玻璃。右上角附 ✕ 按鈕，亦支援按 Esc
或滑鼠右鍵選單快速關閉。

相依套件：pip install PySide6
"""
import json
import os
import sys

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QFont
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                                   QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget)
except ImportError:
    sys.stderr.write("桌面儀表板需要 PySide6：pip install PySide6\n")
    sys.exit(1)

# Apple 系統色（執行=琥珀 / 完成=綠 / 失敗=紅）
C_RUN, C_OK, C_BAD = (255, 159, 10), (48, 209, 88), (255, 69, 58)


def rgba(t, a=1.0):
    return f"rgba({t[0]},{t[1]},{t[2]},{a})"


def fmt(secs):
    return f"{secs // 60}m{secs % 60:02d}s"


_QSS = """
#glass { background-color: rgba(20,23,30,0.72); border-radius: 18px; }
QLabel { color: #f2f3f5; background: transparent; }
#mut   { color: #9aa0ac; }
#faint { color: #6b7280; font-family: Consolas; font-size: 11px; }
#desc  { font-weight: bold; font-size: 12px; }
#chip  { color: #9aa0ac; background-color: rgba(27,30,38,0.9);
         border-radius: 6px; padding: 1px 6px; font-family: Consolas; font-size: 11px; }
QScrollArea, #list, #scrollport { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#close_btn {
    color: #9aa0ac;
    background: transparent;
    border: none;
    border-radius: 11px;
    font-size: 13px;
    font-weight: bold;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
}
#close_btn:hover {
    color: #ffffff;
    background-color: rgba(255, 69, 58, 0.85);
}
#close_btn:pressed {
    background-color: rgba(255, 69, 58, 1.0);
}
QMenu {
    background-color: rgba(25, 28, 36, 0.95);
    color: #f2f3f5;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 18px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: rgba(255, 69, 58, 0.75);
    color: #ffffff;
}
"""


class Dashboard(QWidget):
    def __init__(self, port):
        super().__init__()
        self.api = QUrl(f"http://127.0.0.1:{port}/api")
        self.cards = {}
        self._drag = None
        self._pulse_on = True

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(380, 560)
        self.move(40, 80)
        self.setStyleSheet(_QSS)
        self._build()
        self._apply_glass()

        self.nam = QNetworkAccessManager(self)
        self.nam.finished.connect(self._on_reply)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(2000)
        self._poll()

        self.pulse = QTimer(self)          # 執行中卡片的呼吸燈
        self.pulse.timeout.connect(self._breathe)
        self.pulse.start(700)

    # --- 版面 ---
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        glass = QFrame()
        glass.setObjectName("glass")
        outer.addWidget(glass)

        lay = QVBoxLayout(glass)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        # 頂部列：標題 + 右側關閉按鈕
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(4)

        self.hub1 = QLabel("連線中…")
        self.hub1.setFont(QFont("Microsoft JhengHei UI", 10, QFont.Bold))
        top_bar.addWidget(self.hub1, 1)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("close_btn")
        self.btn_close.setToolTip("關閉儀表板 (Esc)")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        top_bar.addWidget(self.btn_close)

        lay.addLayout(top_bar)

        self.hub2 = QLabel("")
        self.hub2.setObjectName("mut")
        self.hub2.setWordWrap(True)
        lay.addWidget(self.hub2)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: rgba(255,255,255,0.08);")
        lay.addWidget(line)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("scrollport")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.listw = QWidget()
        self.listw.setObjectName("list")
        self.vbox = QVBoxLayout(self.listw)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(8)
        self.vbox.addStretch(1)          # 讓卡片靠上堆疊
        self.scroll.setWidget(self.listw)
        lay.addWidget(self.scroll, 1)

        self.empty = QLabel("（尚未派出任何 job）")
        self.empty.setObjectName("mut")
        self.empty.setAlignment(Qt.AlignCenter)
        self.vbox.insertWidget(0, self.empty)

        self.foot = QLabel("")
        self.foot.setObjectName("faint")
        self.foot.setWordWrap(True)
        lay.addWidget(self.foot)

    def _apply_glass(self):
        """Win10/11：DWM Acrylic 讓背後真的模糊（Qt 已負責合成，文字不會毛邊）。
        失敗／非 Windows 就靠 WA_TranslucentBackground 的半透明底，不會壞。"""
        if os.name != "nt":
            return
        try:
            import ctypes

            class _Accent(ctypes.Structure):
                _fields_ = [("state", ctypes.c_uint), ("flags", ctypes.c_uint),
                            ("grad", ctypes.c_uint), ("anim", ctypes.c_uint)]

            class _WCAData(ctypes.Structure):
                _fields_ = [("attr", ctypes.c_int),
                            ("data", ctypes.POINTER(_Accent)),
                            ("size", ctypes.c_size_t)]

            accent = _Accent(4, 0, 0x99110E0B, 0)   # ACCENT_ENABLE_ACRYLICBLURBEHIND, AABBGGRR
            payload = _WCAData(19, ctypes.pointer(accent), ctypes.sizeof(_Accent))
            swca = ctypes.windll.user32.SetWindowCompositionAttribute
            swca.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WCAData)]
            swca(ctypes.c_void_p(int(self.winId())), ctypes.byref(payload))
        except Exception:
            pass

    # --- 拖曳（無邊框窗，整窗可拖）---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None

    # --- 快捷鍵 (Esc / Ctrl+W) ---
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape or (e.key() == Qt.Key_W and e.modifiers() & Qt.ControlModifier):
            self.close()
        else:
            super().keyPressEvent(e)

    # --- 右鍵選單 ---
    def contextMenuEvent(self, e):
        menu = QMenu(self)
        act_close = menu.addAction("關閉儀表板 (Esc)")
        act_close.triggered.connect(self.close)
        menu.exec(e.globalPos())

    # --- 輪詢與渲染 ---
    def _poll(self):
        self.nam.get(QNetworkRequest(self.api))

    def _on_reply(self, reply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._show_waiting()
            reply.deleteLater()
            return
        raw = bytes(reply.readAll().data())
        reply.deleteLater()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._show_waiting()
            return
        self._render(data)

    def _show_waiting(self):
        self.hub1.setText("⚠ 等待 hub 連線…")
        self.hub2.setText(f"{self.api.toString()}\n（hub 或 session 可能尚未就緒）")

    def _render(self, data):
        h = data.get("hub", {})
        self.hub1.setText(f"agent-hub · uptime {fmt(h.get('uptime', 0))}")
        self.hub2.setText(
            f"workers: {', '.join(h.get('workers', [])) or '—'}   ·   "
            f"jobs {h.get('total', 0)} · 執行中 {h.get('running', 0)}"
            f" · ✓ {h.get('done', 0)} · ✗ {h.get('failed', 0)}")

        jobs = data.get("jobs", [])
        self.empty.setVisible(not jobs)
        seen = set()
        for j in jobs:
            seen.add(j["id"])
            c = self.cards.get(j["id"])
            if c is None:
                c = self._make_card(j)
                self.cards[j["id"]] = c
            self._update_card(c, j)
        for jid in list(self.cards):          # 保險：移除消失的 job
            if jid not in seen:
                self.cards[jid]["frame"].setParent(None)
                del self.cards[jid]

        evs = h.get("events", [])
        self.foot.setText("hub 最近：" + ("；".join(e["msg"] for e in evs[-3:]) or "—"))

    def _make_card(self, j):
        f = QFrame()
        f.setObjectName("card")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        top = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(10, 10)
        idl = QLabel(j["id"])
        idl.setObjectName("faint")
        chip = QLabel(j["worker"])
        chip.setObjectName("chip")
        top.addWidget(dot)
        top.addWidget(idl)
        top.addStretch(1)
        top.addWidget(chip)

        desc = QLabel(j["desc"])
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        status = QLabel("")
        status.setObjectName("mut")
        tail = QLabel("")
        tail.setObjectName("faint")
        tail.setWordWrap(True)

        lay.addLayout(top)
        lay.addWidget(desc)
        lay.addWidget(status)
        lay.addWidget(tail)
        self.vbox.insertWidget(self.vbox.count() - 1, f)   # 插在 stretch 之前
        return {"frame": f, "dot": dot, "status": status, "tail": tail, "state": None}

    def _update_card(self, c, j):
        state = ("running" if not j["done"]
                 else "done" if j["status"].startswith("Completed") else "failed")
        if state != c["state"]:
            color = {"running": C_RUN, "done": C_OK, "failed": C_BAD}[state]
            c["frame"].setStyleSheet(
                "QFrame#card{background-color:rgba(32,36,46,0.92);border-radius:12px;"
                f"border-left:3px solid {rgba(color)};}}")
            c["dot"].setStyleSheet(f"background-color:{rgba(color)};border-radius:5px;")
            c["state"] = state
        st = "執行中" if not j["done"] else j["status"]
        c["status"].setText(f"{st} · {fmt(j['elapsed'])}")
        tl = (j.get("tail") or "").splitlines()
        c["tail"].setText(tl[-1][:80] if tl else "")

    def _breathe(self):
        self._pulse_on = not self._pulse_on
        a = 1.0 if self._pulse_on else 0.35
        for c in self.cards.values():
            if c["state"] == "running":
                c["dot"].setStyleSheet(f"background-color:{rgba(C_RUN, a)};border-radius:5px;")


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft JhengHei UI", 9))
    port = sys.argv[1] if len(sys.argv) > 1 else "8787"
    win = Dashboard(port)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
