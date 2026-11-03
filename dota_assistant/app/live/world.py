"""游戏画面里的野点标记：小地图野点（camp_marks，ingame._jungle_tick 每秒算）亮着时，单独一个线程每秒截几次 Dota 窗口，
从自己英雄的血条 + GSI 坐标反推镜头（vision/camera.py），web/world.html 按 /api/world 把圈画在游戏画面里的野点上。

不走主控的工作线程：要每秒 10 次左右，工作线程 1 秒一轮还要做识别。这个线程只读 camp_marks / GSI（整个换掉的引用，不改里面），
只写自己的 _world，网页请求线程读 _world 算屏幕位置。截图句柄在这个线程里建（同一个句柄不能跨线程用）。
"""
from __future__ import annotations

import logging
import threading
import time

from ...vision import camera

log = logging.getLogger(__name__)

FRAME = 0.1                  # 有标记时多久截一次（秒；截 Dota 窗口 + 认血条约 30 毫秒）
IDLE = 0.25                  # 没标记时多久看一次
STALE = 0.6                  # 镜头位置过了这么久没更新就不画（英雄出画面了 / 截不到）
EXTRAPOLATE = 0.35           # GSI 坐标最多往后推多久（推送间隔 0.1 秒，英雄在走时按速度推到截图那一刻）
# 画面里这些地方是 Dota 自己的界面，标记落在里面就不画（1080 高时的像素）：顶栏、底部操作栏、左下小地图
HUD_TOP, HUD_BOTTOM, MINIMAP_W, MINIMAP_H = 50, 150, 300, 310
VISIBLE = 60                 # 框至少露出这么大（像素见方）才画
LOTUS_RING = 350             # 莲花池画多大的圈（世界单位：站进 350 范围自动拾取）


class WorldMixin:
    def start_world(self, grabber_factory=None) -> None:
        """webui.run 起；测试不起（会真截屏）。grabber_factory 给测试换"""
        if getattr(self, "_world_thread", None):
            return
        if grabber_factory is None:
            from ...vision.capture import WindowGrabber
            grabber_factory = WindowGrabber
        self._world_thread = threading.Thread(target=self._world_loop, args=(grabber_factory,), daemon=True, name="world")
        self._world_thread.start()

    def _world_active(self) -> bool:
        st = self.gsi.state if self.gsi else None
        return bool(self.cfg.world_marks and self.camp_marks and st and st.alive and st.in_match)

    def _world_loop(self, grabber_factory) -> None:
        grabber, tracker, found = None, camera.Tracker(), None
        while not self._stop.is_set():
            if not self._world_active():
                tracker.target, found = None, None
                time.sleep(IDLE)
                continue
            t0 = time.time()
            try:
                grabber = grabber or grabber_factory()
                img = grabber.grab()
                hero = self._hero_now(t0)
                target = tracker.update(img, hero) if img is not None and hero else None
            except Exception:                        # 截图 / 识别出错不能拖垮助手，下一帧再试
                log.warning("游戏画面野点：这一帧出错", exc_info=True)
                img, target = None, None
            if target is not None:
                self._world = {"t": t0, "target": (float(target[0]), float(target[1])), "size": (img.shape[1], img.shape[0])}
            if (target is not None) != found:
                found = target is not None
                log.info("游戏画面野点：%s", "认出镜头位置" if found else "认不出镜头位置（画面里找不到自己英雄的血条）")
            time.sleep(max(0.0, FRAME - (time.time() - t0)))

    def _hero_now(self, at: float) -> tuple[float, float] | None:
        """GSI 里自己英雄现在大概在哪：最后一次推送的坐标，英雄在走的话按前两次推送的速度推到 at 那一刻"""
        pts = []
        for t, raw in reversed(self.gsi.state.history()[-6:]):
            h = raw.get("hero") or {}
            if isinstance(h.get("xpos"), (int, float)) and isinstance(h.get("ypos"), (int, float)):
                pts.append((t, float(h["xpos"]), float(h["ypos"])))
            if len(pts) == 2:
                break
        if not pts:
            return None
        t1, x1, y1 = pts[0]
        if len(pts) < 2 or not 0 < t1 - pts[1][0] < 1:
            return x1, y1
        t0, x0, y0 = pts[1]
        dt = min(max(0.0, at - t1), EXTRAPOLATE)
        return x1 + (x1 - x0) / (t1 - t0) * dt, y1 + (y1 - y0) / (t1 - t0) * dt

    def world_view(self) -> dict:
        """给 world.html（网页请求线程里调）：每个标记在 Dota 画面里的样子——野点是刷新框的四个角（poly，和按住 Alt 时
        Dota 自己画的黄框一样），莲花池是一个圈（中心 cx / cy + 横竖半径 rx / ry）；牌子挂在 (sx, sy)（框 / 圈的上边）。
        框当成和英雄一样高（不知道地面高低；按地形高度表贴地面试过，崖边反而更歪，2026-11-03 全图巡检）。认不出镜头就 show=False"""
        w, marks = getattr(self, "_world", None), self.camp_marks
        if not marks or not w or time.time() - w["t"] > STALE or self._bb_anchor is None or not self._world_active():
            return {"show": False}
        size, s = w["size"], w["size"][1] / 1080
        out = []
        for m in marks:
            if m.get("box"):
                x0, y0, x1, y1 = m["box"]
                pts = camera.project([(x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, (y0 + y1) / 2)],
                                     w["target"], size)
                (x, y), outline = pts[4], pts[:4]
                shape = {"poly": [[round(a, 1), round(b, 1)] for a, b in outline]}
                top = float(outline[2:, 1].min())                      # 北边那条的上沿
                xs, ys = list(outline[:, 0]), list(outline[:, 1])
            else:
                pts = camera.project([(m["x"], m["y"]), (m["x"] + LOTUS_RING, m["y"]), (m["x"], m["y"] + LOTUS_RING)],
                                     w["target"], size)
                (x, y), (xr, _), (_, top) = pts
                shape = {"rx": round(xr - x, 1), "ry": round(y - top, 1)}
                xs, ys = [x - shape["rx"], x + shape["rx"]], [y - shape["ry"], y + shape["ry"]]
            # 框 / 圈有一块（至少 VISIBLE 像素见方）露在游戏画面里（去掉顶栏、底部操作栏）就画；整个缩在小地图那一角里不画。
            # 不能只看中心：野点在画面下沿时框中心常常在操作栏后面，框的上半截明明看得见（2026-11-03 实测天辉下路小野就这样没画出来）
            left, right = max(min(xs), 0), min(max(xs), size[0])
            upper, lower = max(min(ys), HUD_TOP * s), min(max(ys), size[1] - HUD_BOTTOM * s)
            if right - left < VISIBLE * s or lower - upper < VISIBLE * s \
                    or (max(xs) < MINIMAP_W * s and min(ys) > size[1] - MINIMAP_H * s):
                continue
            # 牌子挂在框上边、看得见的那一段中间；框上边出了画面就挂到画面顶上那条下面
            out.append({**m, **shape, "cx": round(x, 1), "cy": round(y, 1),
                        "sx": round(min(max(x, left + 60 * s), right - 60 * s), 1),
                        "sy": round(min(max(top, (HUD_TOP + 40) * s), lower), 1)})
        out.sort(key=lambda mk: -mk.get("row", 0))  # 同一个野点：先到的那个（第 0 行）最后画，框在最上面
        return {"show": bool(out), "size": list(size), "anchor": round(self._bb_anchor * 1000), "paused": self._bb_paused,
                "marks": out}
