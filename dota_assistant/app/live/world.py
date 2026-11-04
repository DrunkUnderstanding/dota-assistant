"""游戏画面里的野点标记：小地图野点（camp_marks，ingame._jungle_tick 每秒算）亮着时，单独一个线程每秒截几次 Dota 窗口，
从自己英雄的血条 + GSI 坐标反推镜头（vision/camera.py），web/world.html 按 /api/world 把圈画在游戏画面里的野点上。

不走主控的工作线程：要每秒 25 次左右，工作线程 1 秒一轮还要做识别。这个线程只读 camp_marks / GSI（整个换掉的引用，不改里面），
只写自己的 _world，网页请求线程读 _world 算屏幕位置。截图句柄在这个线程里建（同一个句柄不能跨线程用）。
"""
from __future__ import annotations

import logging
import math
import threading
import time

from ...vision import camera

log = logging.getLogger(__name__)

# 有标记时多久截一次（秒）。截 Dota 窗口约 16 毫秒 + 认血条约 5 毫秒，一秒 25 帧占一个核的一半左右（9500F 实测）；
# 以前 0.1 秒一帧，拉镜头时框明显跟不上（玩家 2026-11-03：「有点延迟，感觉怪怪的」）
FRAME = 0.04
IDLE = 0.25                  # 没标记时多久看一次
STALE = 0.6                  # 镜头位置过了这么久没更新就不画（英雄出画面了 / 截不到）
EXTRAPOLATE = 0.35           # GSI 坐标最多往后推多久（推送间隔 0.1 秒，英雄在走时按速度推到截图那一刻）
# 拉镜头时按镜头速度把位置往前推到「框画上屏幕那一刻」：截图 → 认血条 → 页面拉 → Electron 画，加起来落后 0.05~0.1 秒，
# 镜头拉得快时框明显拖在后面（玩家 2026-11-03：「镜头移动比较快的时候还是会有点跟不上」）
PREDICT = 0.12               # 最多往前推多久（秒）
DISPLAY_LAG = 0.03           # 页面拿到位置以后到画在屏幕上大概还要多久（拉取 + Electron 画 + 系统合成）
CAM_SPEED = (300, 10000)     # 镜头速度（世界单位 / 秒）在这个范围里才推：太慢是认血条 / GSI 的抖动，太快是点小地图跳过去
CAM_AGREE = 0.4              # 最近两段的速度差不超过速度的这么多，才算在匀速拉（刚开始拉 / 刚停下那一帧不推）
# 画面里这些地方是 Dota 自己的界面，标记落在里面就不画（1080 高时的像素）：顶栏、底部操作栏、左下小地图
HUD_TOP, HUD_BOTTOM, MINIMAP_W, MINIMAP_H = 50, 150, 300, 310
VISIBLE = 60                 # 框至少露出这么大（像素见方）才画


def camera_velocity(track: list[tuple[float, tuple[float, float]]]) -> tuple[float, float]:
    """最近三帧认到的镜头位置 [(时刻, (x, y))] → 镜头在往哪个方向拉、多快（世界单位 / 秒）。
    两段速度对得上（匀速在拉）才算；刚开始拉、刚停、点小地图跳过去、认血条抖动、中间丢了帧都给 0，框宁可慢一点也别乱飘"""
    if len(track) < 3:
        return 0.0, 0.0
    (ta, a), (tb, b), (tc, c) = track[-3:]
    if not 0 < tb - ta <= 2 * FRAME + 0.02 or not 0 < tc - tb <= 2 * FRAME + 0.02:
        return 0.0, 0.0
    v1 = ((b[0] - a[0]) / (tb - ta), (b[1] - a[1]) / (tb - ta))
    v2 = ((c[0] - b[0]) / (tc - tb), (c[1] - b[1]) / (tc - tb))
    v = ((c[0] - a[0]) / (tc - ta), (c[1] - a[1]) / (tc - ta))
    speed = math.hypot(*v)
    if not CAM_SPEED[0] <= speed <= CAM_SPEED[1] or math.hypot(v2[0] - v1[0], v2[1] - v1[1]) > CAM_AGREE * speed:
        return 0.0, 0.0
    return v


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
        grabber, tracker, found, track = None, camera.Tracker(), None, []
        while not self._stop.is_set():
            if not self._world_active():
                tracker.target, found, track = None, None, []
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
                track = (track + [(t0, (float(target[0]), float(target[1])))])[-3:]
                self._world = {"t": t0, "target": track[-1][1], "v": camera_velocity(track),
                               "size": (img.shape[1], img.shape[0])}
            else:
                track = []
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
        Dota 自己画的黄框一样）；牌子挂在 (sx, sy)（框的上边）。
        框当成和英雄一样高（不知道地面高低；按地形高度表贴地面试过，崖边反而更歪，2026-11-03 全图巡检）。认不出镜头就 show=False"""
        w, marks, now = getattr(self, "_world", None), self.camp_marks, time.time()
        if not marks or not w or now - w["t"] > STALE or self._bb_anchor is None or not self._world_active():
            return {"show": False}
        size, s = w["size"], w["size"][1] / 1080
        vx, vy = w.get("v") or (0.0, 0.0)
        ahead = min(now - w["t"] + DISPLAY_LAG, PREDICT)          # 在拉镜头：推到框画上屏幕那一刻镜头大概在哪
        target = (w["target"][0] + vx * ahead, w["target"][1] + vy * ahead)
        out = []
        for m in marks:
            x0, y0, x1, y1 = m["box"]
            pts = camera.project([(x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, (y0 + y1) / 2)], target, size)
            (x, y), outline = pts[4], pts[:4]
            shape = {"poly": [[round(a, 1), round(b, 1)] for a, b in outline]}
            top = float(outline[2:, 1].min())                          # 北边那条的上沿
            xs, ys = list(outline[:, 0]), list(outline[:, 1])
            # 框有一块（至少 VISIBLE 像素见方）露在游戏画面里（去掉顶栏、底部操作栏）就画；整个缩在小地图那一角里不画。
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
