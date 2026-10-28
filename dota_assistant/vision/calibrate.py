"""校准：在一张选人界面截图上框出英雄格子，存成 calibration.json。

只需框每一排的第一个和最后一个格子，中间的按等距补齐。框的时候只框头像图，别带上名字和边框。
操作：鼠标拖框；Backspace 撤销上一步；S 跳过禁用格；Esc 放弃。
"""
from __future__ import annotations

from pathlib import Path

import cv2
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QInputDialog, QWidget

from .recognize import CALIBRATION_PATH, Calibration, Rect, interpolate, read_image

STEPS = [
    ("radiant", "天辉第 1 个英雄头像（最左）"),
    ("radiant", "天辉第 5 个英雄头像（最右）"),
    ("dire", "夜魇第 1 个英雄头像（最左）"),
    ("dire", "夜魇第 5 个英雄头像（最右）"),
    ("bans", "（可选）第一个禁用头像 —— 按 S 跳过"),
    ("bans", "最后一个禁用头像"),
]
COLORS = {"radiant": QColor(90, 220, 120), "dire": QColor(240, 90, 90), "bans": QColor(200, 200, 200)}


class CalibrateWindow(QWidget):
    def __init__(self, image_path: Path):
        super().__init__()
        self.img = read_image(image_path)
        h, w = self.img.shape[:2]
        rgb = cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB)
        self.pix = QPixmap.fromImage(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.scale = min(avail.width() * 0.95 / w, avail.height() * 0.9 / h, 1.0)
        self.setFixedSize(int(w * self.scale), int(h * self.scale))
        self.setWindowTitle(f"校准 · {image_path.name} · {w}×{h}")
        self.note = f"{image_path.name} {w}x{h}"
        self.rects: list[Rect] = []          # 已完成步骤的矩形（比例）
        self.drag: tuple[QPoint, QPoint] | None = None
        self.saved = False

    # 屏幕坐标 ↔ 比例
    def to_rect(self, a: QPoint, b: QPoint) -> Rect:
        r = QRect(a, b).normalized()
        W, H = self.width(), self.height()
        return r.x() / W, r.y() / H, r.width() / W, r.height() / H

    def to_qrect(self, r: Rect) -> QRect:
        W, H = self.width(), self.height()
        return QRect(int(r[0] * W), int(r[1] * H), int(r[2] * W), int(r[3] * H))

    def slots(self) -> dict[str, list[Rect]]:
        out = {"radiant": [], "dire": [], "bans": []}
        for i in range(0, len(self.rects) - 1, 2):
            side = STEPS[i][0]
            out[side] = interpolate(self.rects[i], self.rects[i + 1], 5 if side != "bans" else 2)
        return out

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pix)
        for side, rs in self.slots().items():
            p.setPen(QPen(COLORS[side], 2))
            for r in rs:
                p.drawRect(self.to_qrect(r))
        if len(self.rects) % 2 == 1:
            p.setPen(QPen(COLORS[STEPS[len(self.rects) - 1][0]], 2, Qt.DashLine))
            p.drawRect(self.to_qrect(self.rects[-1]))
        if self.drag:
            p.setPen(QPen(QColor(255, 220, 80), 2))
            p.drawRect(QRect(*self.drag).normalized())
        step = len(self.rects)
        tip = (f"第 {step + 1}/{len(STEPS)} 步：框出{STEPS[step][1]}" if step < len(STEPS) else "完成")
        tip += "    Backspace 撤销 · Esc 放弃"
        # 提示条放在底部：英雄格就在屏幕最顶上，放顶部会把它们盖住
        y = self.height() - 34
        p.fillRect(0, y, self.width(), 34, QColor(0, 0, 0, 190))
        p.setPen(QColor(255, 230, 150))
        p.setFont(QFont("Microsoft YaHei UI", 12))
        p.drawText(12, y + 23, tip)

    def mousePressEvent(self, e):
        self.drag = (e.position().toPoint(), e.position().toPoint())

    def mouseMoveEvent(self, e):
        if self.drag:
            self.drag = (self.drag[0], e.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, e):
        if not self.drag:
            return
        a, b = self.drag
        self.drag = None
        if abs(a.x() - b.x()) < 4 or abs(a.y() - b.y()) < 4:
            self.update()
            return
        self.rects.append(self.to_rect(a, b))
        if len(self.rects) == len(STEPS):
            self.finish(with_bans=True)
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Backspace and self.rects:
            self.rects.pop()
            self.update()
        elif e.key() == Qt.Key_S and len(self.rects) == 4:
            self.finish(with_bans=False)
        elif e.key() == Qt.Key_Escape:
            self.close()

    def finish(self, with_bans: bool) -> None:
        r = self.rects
        cal = Calibration(interpolate(r[0], r[1], 5), interpolate(r[2], r[3], 5), [], self.note)
        if with_bans:
            n, ok = QInputDialog.getInt(self, "禁用格数量", "第一个到最后一个禁用格一共几个？", 6, 2, 24)
            if ok:
                cal.bans = interpolate(r[4], r[5], n)
        cal.save()
        self.saved = True
        self.close()


def calibrate(image_path: Path) -> bool:
    app = QApplication.instance() or QApplication([])
    w = CalibrateWindow(image_path)
    w.show()
    app.exec()
    if w.saved:
        print(f"已保存 {CALIBRATION_PATH}")
    return w.saved
