"""实时助手界面：叠加层（置顶、透明、鼠标穿透）+ 控制面板（手动输入 / 纠正识别 / 学模板）。

数据流：
  定时截屏 → vision.recognize → 连续两次结果一致才采用 → 填进控制面板的 10 个格子
  格子（识别或手动） + GSI（我方阵营、我选的英雄） → advisor.advise → 叠加层 + 语音
控制面板的格子是唯一的「当前局面」，识别和手动输入都只是往里填。
"""
from __future__ import annotations

import html
import time
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from . import advisor as adv
from .config import ROOT, Config
from .engine import POSITION_NAMES
from .gsi import GSIServer
from .heroes import Hero
from .hotkeys import Hotkeys
from .speech import Speaker
from .vision import EMPTY, Calibration, DraftView, TemplateBank, recognize, write_image

SHOTS_DIR = ROOT / "shots"
STYLE = {
    "title": "color:#f0c060; font-weight:600;",
    "dim": "color:#9aa4ad;",
    "head": "color:#7fc8ff; font-weight:600;",
    "row": "color:#f2f2f2;",
    "warn": "color:#ff9a5a;",
}


def lines_html(lines: list[tuple[str, str]]) -> str:
    return "<br>".join(f'<span style="{STYLE[k]}">{html.escape(t)}</span>' for k, t in lines)


class Overlay(QWidget):
    def __init__(self, cfg: Config):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(cfg.overlay_width)
        frame = QFrame(self)
        frame.setObjectName("panel")
        alpha = int(255 * max(0.2, min(1.0, cfg.overlay_opacity)))
        frame.setStyleSheet(f"#panel {{ background: rgba(12,16,20,{alpha}); border: 1px solid rgba(255,255,255,40);"
                            f" border-radius: 10px; }}")
        self.label = QLabel(frame)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setStyleSheet("font-family: 'Microsoft YaHei UI'; font-size: 11pt; line-height: 140%;")
        self.label.setFixedWidth(cfg.overlay_width - 30)   # 固定宽度，折行后的高度才算得准
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.addWidget(self.label)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        screens = QGuiApplication.screens()
        screen = screens[cfg.monitor - 1] if 0 < cfg.monitor <= len(screens) else QGuiApplication.primaryScreen()
        g = screen.geometry()
        self.move(int(g.x() + g.width() * cfg.overlay_x), int(g.y() + g.height() * cfg.overlay_y))

    def show_lines(self, lines: list[tuple[str, str]]) -> None:
        self.label.setText(lines_html(lines))
        self.label.setMinimumHeight(0)                    # 先解除上次的固定高度，heightForWidth 才不受它影响
        self.label.setMaximumHeight(16777215)
        self.label.setFixedHeight(self.label.heightForWidth(self.label.width()))
        self.setFixedHeight(self.label.height() + 22)


class ControlPanel(QWidget):
    def __init__(self, names: list[str], cfg: Config):
        super().__init__()
        self.setWindowTitle("Dota 选人助手 · 控制面板")
        self.setMinimumWidth(720)
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.position = QComboBox()
        self.position.addItems([f"{p} {POSITION_NAMES[p]}" for p in range(1, 6)])
        self.position.setCurrentIndex(cfg.position - 1)
        self.side = QComboBox()
        self.side.addItems(["阵营：自动（GSI）", "我在天辉", "我在夜魇"])
        self.mode = QComboBox()
        self.mode.addItems(["自动识别", "手动输入"])
        self.voice = QCheckBox("语音")
        self.voice.setChecked(cfg.voice)
        self.scan_without_gsi = QCheckBox("没连上 GSI 也持续识别")
        self.scan_without_gsi.setChecked(True)
        for w in (QLabel("位置"), self.position, self.side, self.mode, self.voice, self.scan_without_gsi):
            top.addWidget(w)
        top.addStretch()
        lay.addLayout(top)

        self.status = QLabel()
        self.status.setStyleSheet("color:#666;")
        lay.addWidget(self.status)

        completer = QCompleter(names, self)
        completer.setFilterMode(Qt.MatchContains)
        grid = QGridLayout()
        self.slots: dict[str, list[QLineEdit]] = {}
        for row, (side, title) in enumerate((("radiant", "天辉"), ("dire", "夜魇"))):
            grid.addWidget(QLabel(title), row, 0)
            self.slots[side] = []
            for i in range(5):
                e = QLineEdit()
                e.setCompleter(completer)
                grid.addWidget(e, row, i + 1)
                self.slots[side].append(e)
        grid.addWidget(QLabel("禁用"), 2, 0)
        self.bans = QLineEdit()
        self.bans.setPlaceholderText("逗号分隔，可以留空；识别到的禁用会自动加上")
        grid.addWidget(self.bans, 2, 1, 1, 5)
        lay.addLayout(grid)

        btns = QHBoxLayout()
        self.apply_btn = QPushButton("按格子重新推荐")
        self.clear_btn = QPushButton("清空")
        self.rescan_btn = QPushButton("立即识别")
        self.learn_btn = QPushButton("记住这次识别（按格子里的名字存模板）")
        self.shot_btn = QPushButton("保存截图")
        for b in (self.apply_btn, self.clear_btn, self.rescan_btn, self.learn_btn, self.shot_btn):
            btns.addWidget(b)
        lay.addLayout(btns)

        self.preview = QLabel()
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.preview.setStyleSheet("background:#10151a; padding:10px; border-radius:6px;"
                                   "font-family:'Microsoft YaHei UI'; font-size:10.5pt;")
        lay.addWidget(self.preview, 1)

    def all_slot_edits(self) -> list[QLineEdit]:
        return self.slots["radiant"] + self.slots["dire"]


class App(QObject):
    toggle_requested = Signal()
    rescan_requested = Signal()
    shot_requested = Signal()

    def __init__(self, cfg: Config, screen_factory=None):
        super().__init__()
        self.cfg = cfg
        self.advisor = adv.Advisor(cfg)
        self.db = self.advisor.db
        self.speaker = Speaker(cfg.voice_rate, cfg.voice)
        self.cal = Calibration.load()
        self.bank = TemplateBank()
        self.screen = None
        if screen_factory is None:
            from .capture import Screen
            screen_factory = lambda: Screen(cfg.monitor)
        self._screen_factory = screen_factory
        try:
            self.gsi = GSIServer(cfg.gsi_port).start()
        except OSError:
            self.gsi = None                  # 端口被占用：照样能用，只是没有 GSI

        names = sorted({h.name_cn for h in self.db} | {h.name_en for h in self.db})
        self.panel = ControlPanel(names, cfg)
        self.overlay = Overlay(cfg)
        self.user_hidden = False
        self.last_view: DraftView | None = None
        self.pending_key = None
        self.prev_draft: adv.Draft | None = None
        self.last_scan = 0.0

        p = self.panel
        p.apply_btn.clicked.connect(lambda: self.refresh())
        p.clear_btn.clicked.connect(self.clear)
        p.rescan_btn.clicked.connect(lambda: self.scan(force=True))
        p.learn_btn.clicked.connect(self.learn)
        p.shot_btn.clicked.connect(self.save_shot)
        p.voice.toggled.connect(lambda on: setattr(self.speaker, "enabled", on))
        for w in (p.position, p.side):
            w.currentIndexChanged.connect(lambda _: self.refresh(force_speech=True))
        for e in p.all_slot_edits() + [p.bans]:
            e.editingFinished.connect(lambda: self.refresh())

        self.toggle_requested.connect(self.toggle_overlay)
        self.rescan_requested.connect(lambda: self.scan(force=True))
        self.shot_requested.connect(self.save_shot)
        self.hotkeys = Hotkeys({cfg.hotkey_toggle: self.toggle_requested.emit,
                                cfg.hotkey_rescan: self.rescan_requested.emit,
                                cfg.hotkey_shot: self.shot_requested.emit}).start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(cfg.scan_interval * 1000))
        self.refresh()

    # ---------- 局面 ----------

    def side(self) -> str | None:
        i = self.panel.side.currentIndex()
        if i == 1:
            return "radiant"
        if i == 2:
            return "dire"
        return self.gsi.state.team if self.gsi and self.gsi.state.alive else None

    def position(self) -> int:
        return self.panel.position.currentIndex() + 1

    def _hero(self, text: str, edit: QLineEdit | None = None) -> Hero | None:
        text = text.strip()
        if not text:
            if edit:
                edit.setStyleSheet("")
            return None
        try:
            h = self.db.find(text)
            if edit:
                edit.setStyleSheet("")
            return h
        except KeyError:
            if edit:
                edit.setStyleSheet("background:#ffd8d8;")
            return None

    def current_draft(self) -> adv.Draft:
        p = self.panel
        team = {s: [h for e in p.slots[s] if (h := self._hero(e.text(), e))] for s in ("radiant", "dire")}
        side = self.side() or "radiant"
        other = "dire" if side == "radiant" else "radiant"
        bans = [h for t in p.bans.text().replace("，", ",").split(",") if (h := self._hero(t))]
        if self.last_view and p.mode.currentIndex() == 0:
            bans += [self.db.by_id[i] for i in self.last_view.ids("bans") if i in self.db.by_id]
        my = None
        if self.gsi and self.gsi.state.alive and self.gsi.state.my_hero_key:
            my = next((h for h in self.db if h.key == self.gsi.state.my_hero_key), None)
        allies = [h for h in team[side] if not my or h.id != my.id]
        return adv.Draft(allies, team[other], bans, my)

    def refresh(self, force_speech: bool = False) -> None:
        draft = self.current_draft()
        advice = self.advisor.advise(draft, self.position())
        lines = adv.render_lines(advice, self.side())
        self.overlay.show_lines(lines)
        self.panel.preview.setText(lines_html(lines))
        if force_speech or self.prev_draft is None or draft.key() != self.prev_draft.key():
            text = adv.speech(self.prev_draft, advice, force=force_speech)
            if text:
                self.speaker.say(text)
        self.prev_draft = draft
        self.update_visibility()
        self.update_status()

    def clear(self) -> None:
        for e in self.panel.all_slot_edits():
            e.clear()
        self.panel.bans.clear()
        self.prev_draft = None
        self.refresh()

    # ---------- 识别 ----------

    def should_scan(self) -> bool:
        if self.panel.mode.currentIndex() != 0 or not (self.cal and self.cal.ready):
            return False
        if self.gsi and self.gsi.state.alive:
            return self.gsi.state.in_draft
        return self.panel.scan_without_gsi.isChecked()

    def tick(self) -> None:
        if self.should_scan():
            self.scan()
        else:
            self.update_visibility()
            self.update_status()

    def grab(self):
        if self.screen is None:
            self.screen = self._screen_factory()
        return self.screen.grab()

    def scan(self, force: bool = False) -> None:
        if not (self.cal and self.cal.ready):
            self.update_status("还没校准：先运行 python -m dota_assistant calibrate <截图>")
            return
        view = recognize(self.grab(), self.cal, self.bank, self.cfg.match_threshold, keep_images=True)
        self.last_scan = time.time()
        key = view.key()
        stable = force or key == self.pending_key            # 连续两次一致才采用，躲开动画过程
        self.pending_key = key
        if not stable:
            return
        changed = self.last_view is None or self.last_view.key() != key
        self.last_view = view
        if changed or force:
            self.fill_slots(view)
            self.refresh(force_speech=force)

    def fill_slots(self, view: DraftView) -> None:
        for side in ("radiant", "dire"):
            for e, s in zip(self.panel.slots[side], getattr(view, side)):
                if e.hasFocus():
                    continue                                   # 正在手动改的格子不覆盖
                e.setText(self.db.by_id[s.hero_id].name_cn if s.hero_id in self.db.by_id else "")
                guess = self.db.by_id.get(s.guess)
                e.setPlaceholderText(f"? {guess.name_cn} {s.score:.2f}" if guess and not s.hero_id else "")
                e.setToolTip(f"相似度 {s.score:.2f}")

    def learn(self) -> None:
        if not self.last_view:
            self.update_status("还没有识别结果可以记")
            return
        n = 0
        for side in ("radiant", "dire"):
            for e, s in zip(self.panel.slots[side], getattr(self.last_view, side)):
                if s.image is None:
                    continue
                h = self._hero(e.text(), e)
                if e.text().strip() and not h:
                    continue                                   # 名字写错的格子跳过
                self.bank.learn(h.id if h else EMPTY, s.image)
                n += 1
        self.update_status(f"已记住 {n} 个格子，模板共 {self.bank.portraits} 张官方 + {self.bank.learned} 张学习")

    def save_shot(self) -> None:
        path = SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        write_image(path, self.grab())
        self.update_status(f"截图已保存：{path}")

    # ---------- 显示 ----------

    def toggle_overlay(self) -> None:
        self.user_hidden = not self.user_hidden
        self.update_visibility()

    def update_visibility(self) -> None:
        in_game_not_draft = (self.gsi and self.gsi.state.alive and not self.gsi.state.in_draft
                             and self.panel.mode.currentIndex() == 0)
        self.overlay.setVisible(not self.user_hidden and not in_game_not_draft)

    def update_status(self, extra: str = "") -> None:
        if self.gsi is None:
            g = f"GSI：端口 {self.cfg.gsi_port} 被占用"
        elif self.gsi.state.alive:
            g = f"GSI：已连接 {self.gsi.state.game_state or ''} 阵营 {self.gsi.state.team or '?'}"
        else:
            g = "GSI：未连接"
        c = "已校准" if self.cal and self.cal.ready else "未校准"
        t = f"上次识别 {time.strftime('%H:%M:%S', time.localtime(self.last_scan))}" if self.last_scan else "未识别"
        keys = f"{self.cfg.hotkey_toggle} 显隐 / {self.cfg.hotkey_rescan} 重识别 / {self.cfg.hotkey_shot} 截图"
        if self.hotkeys.failed:
            keys += f"（{'、'.join(self.hotkeys.failed)} 注册失败，被占用）"
        self.panel.status.setText(" | ".join(x for x in (g, c, t, keys, extra) if x))

    def close(self) -> None:
        self.hotkeys.stop()
        self.speaker.close()
        if self.gsi:
            self.gsi.stop()


def run(cfg: Config) -> int:
    app = QApplication.instance() or QApplication([])
    a = App(cfg)
    a.panel.show()
    a.overlay.show()
    app.aboutToQuit.connect(a.close)
    return app.exec()
