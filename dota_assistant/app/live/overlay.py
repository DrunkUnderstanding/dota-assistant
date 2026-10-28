"""叠加层：显隐、固定 / 解锁、拖动后的位置、底色样式。"""
from __future__ import annotations

import json
import logging
import re

from ...core import config
from ...core.config import Config
from . import consts

log = logging.getLogger(__name__)


class OverlayMixin:
    # ---------- 网页发来的操作 ----------

    def set_overlay_style(self, key: str, value, save: bool = True) -> None:
        """叠加层底色 / 不透明度。面板上拖滑块时 save=False 只改内存（叠加层马上跟着变），松手再写进 config.toml"""
        if key == "overlay_opacity":
            value = round(min(1.0, max(consts.OPACITY_MIN, float(value))), 2)
        elif not re.fullmatch(r"#[0-9a-fA-F]{6}", str(value)):
            raise ValueError(f"颜色要写成 #rrggbb，收到 {value!r}")
        else:
            value = str(value).lower()
        setattr(self.cfg, key, value)
        if save:
            path = config.save({key: value}, self.config_file)
            log.info("叠加层外观已保存到 %s：%s = %s", path, key, value)
            self.message = "叠加层外观已保存"

    def overlay_style(self) -> dict:
        try:
            opacity = min(1.0, max(consts.OPACITY_MIN, float(self.cfg.overlay_opacity)))
        except (TypeError, ValueError):
            opacity = Config.overlay_opacity
        color = self.cfg.overlay_color if re.fullmatch(r"#[0-9a-fA-F]{6}", str(self.cfg.overlay_color)) else Config.overlay_color
        return {"opacity": opacity, "color": color}

    # ---------- 显示 ----------

    def toggle_overlay(self) -> None:
        """有局内提醒弹着时先关提醒（局内叠加层鼠标点不到，靠热键关）"""
        if self.coach_card:
            self._close_card()
        elif self.jungle_cue:
            self.mute_cue()
        elif self.pick_key:
            self.pick_key = None
        else:
            self.user_hidden = not self.user_hidden

    def _close_card(self) -> None:
        if self._showing_brief():
            self.message = f"本局要点已收起，按 {self.cfg.hotkey_brief} 或控制面板「本局要点」再看"
        self.coach_card = None

    def close_overlay(self) -> None:
        """叠加层右上角的 ×：弹着局内提醒就只关提醒，否则把整个叠加层收起（解锁着也一并固定）"""
        if self.coach_card:
            self._close_card()
            return
        if self.jungle_cue:
            self.mute_cue()
            return
        if self.pick_key:
            self.pick_key = None
            return
        self.user_hidden = True
        if not self.overlay_locked:
            self.set_locked(True)
        self.message = f"叠加层已关闭，按 {self.cfg.hotkey_toggle} 或点「显示叠加层」重新打开"

    def toggle_lock(self) -> None:
        self.set_locked(not self.overlay_locked)

    def set_locked(self, locked: bool) -> None:
        self.overlay_locked = locked
        self.message = "叠加层位置已固定" if locked else f"叠加层已解锁：按住卡片拖动，放好后点右上角的锁（或按 {self.cfg.hotkey_lock}）固定"
        self._save_overlay_state()

    def overlay_moved(self, x: float, y: float, width: int | None = None) -> None:
        if not (-1 <= x <= 2 and -1 <= y <= 2):
            raise ValueError("叠加层位置不对")
        width = width if width is not None else (self.overlay_pos or {}).get("width")
        self.overlay_pos = {"x": round(x, 4), "y": round(y, 4)}
        if width is not None:
            if not consts.OVERLAY_WIDTH[0] <= int(width) <= consts.OVERLAY_WIDTH[1]:
                raise ValueError("叠加层宽度不对")
            self.overlay_pos["width"] = int(width)
        self._save_overlay_state()

    def _load_overlay_state(self) -> None:
        try:
            raw = json.loads(self.overlay_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.overlay_locked = bool(raw.get("locked", True))
        if isinstance(raw.get("x"), (int, float)) and isinstance(raw.get("y"), (int, float)):
            self.overlay_pos = {"x": raw["x"], "y": raw["y"]}
            if isinstance(raw.get("width"), int) and consts.OVERLAY_WIDTH[0] <= raw["width"] <= consts.OVERLAY_WIDTH[1]:
                self.overlay_pos["width"] = raw["width"]

    def _save_overlay_state(self) -> None:
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text(json.dumps({"locked": self.overlay_locked, **(self.overlay_pos or {})}),
                                     encoding="utf-8")

    def overlay_visible(self) -> bool:
        if not self.overlay_locked:              # 解锁是为了摆位置，总得看得见
            return True
        if self.user_hidden:
            return False
        if self.coach_card or self.pick_key or self.jungle_cue:
            return True
        in_game_not_draft = bool(self.gsi and self.gsi.state.alive and not self.gsi.state.in_draft
                                 and self.mode == "auto")
        return not in_game_not_draft

    def overlay_view(self) -> dict:
        """野区提醒：有卡片时叠在卡片顶上一行，没卡片时自己一张小卡片（能关）"""
        cue = self.jungle_cue
        if cue and self.coach_card:
            return {**self.coach_card, "cue": {"title": cue.title, "text": cue.lines[0][1]}}
        if cue:
            return {"kind": "notice", "title": cue.title, "closable": True, "jungle": True,
                    "lines": [{"kind": k, "text": t} for k, t in cue.lines]}
        return self.coach_card or self.pick_view() or self.notice or self.draft_view()
