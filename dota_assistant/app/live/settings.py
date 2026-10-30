"""设置：账号设置保存、Dota 目录、热键注册。"""
from __future__ import annotations

import logging
from typing import Callable

from ...ai import llm
from ...core import config
from ...core.dota_install import find_dota
from ...game.gsi import ensure_cfg
from ...game.hotkeys import Hotkeys
from . import consts

log = logging.getLogger(__name__)


class SettingsMixin:
    def _start_hotkeys(self) -> None:
        """按 cfg 里的键注册热键；设置里改了键就整组重新注册"""
        actions = {"hotkey_toggle": (self.toggle_overlay,), "hotkey_lock": (self.toggle_lock,),
                   "hotkey_rescan": (self.scan, True), "hotkey_shot": (self.save_shot,),
                   "hotkey_brief": (self.show_brief,)}

        def key(name: str, fn: Callable, *args):
            def press():
                log.info("热键 %s", name)
                self.submit(fn, *args)
            return press
        if self.hotkeys:
            self.hotkeys.stop()
        self.hotkeys = Hotkeys({getattr(self.cfg, f): key(getattr(self.cfg, f), *actions[f])
                                for f in consts.HOTKEY_ACTIONS}).start()

    def save_settings(self, a: dict) -> None:
        """控制面板「设置」：账号（好友代码、双排队友好友代码、STRATZ token）和 AI 打法来源写进 config.toml。
        密钥字段没传 = 不改（面板不会把已存的密钥发回页面）；传空字符串 = 清掉。
        AI 设置下一局就生效；账号要重启助手才会重新拉个人数据和分位置数据。"""
        updates = {}
        for key in ("account_id", "duo_account_id"):
            if key in a:
                acc = str(a[key] or "0").strip()
                if not acc.isdigit():
                    raise ValueError("好友代码应该是一串数字")
                updates[key] = int(acc)
        for key, name in consts.SECRET_FIELDS.items():
            if key in a:
                val = str(a[key] or "").strip()
                if any(c.isspace() or c == '"' for c in val):
                    raise ValueError(f"{name}里不该有空格或引号，检查一下是不是复制多了")
                updates[key] = val
        if "ai_plan" in a:
            updates["ai_plan"] = bool(a["ai_plan"])
        if "ai_provider" in a:
            if a["ai_provider"] not in llm.PROVIDERS:
                raise ValueError(f"不认识的 AI 来源 {a['ai_provider']!r}")
            updates["ai_provider"] = a["ai_provider"]
        if "ai_model" in a:
            model = str(a["ai_model"] or "").strip()
            if not model:
                raise ValueError("模型名不能空")
            updates["ai_model"] = model
        if "ai_base_url" in a:
            url = str(a["ai_base_url"] or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("接口地址要以 http:// 或 https:// 开头")
            updates["ai_base_url"] = url
        if "hotkeys" in a:
            updates.update(consts._check_hotkeys(self.cfg, a["hotkeys"]))
        if "dota_dir" in a:
            updates["dota_dir"] = consts._check_dota_dir(a["dota_dir"])
        if not updates:
            return
        hotkeys_changed = any(updates.get(f, getattr(self.cfg, f)) != getattr(self.cfg, f) for f in consts.HOTKEY_ACTIONS)
        account_changed = any(updates.get(k, getattr(self.cfg, k)) != getattr(self.cfg, k)
                              for k in ("account_id", "duo_account_id", "stratz_token"))
        dota_changed = updates.get("dota_dir", self.cfg.dota_dir) != self.cfg.dota_dir
        path = config.save(updates, self.config_file)
        for k, v in updates.items():
            setattr(self.cfg, k, v)
        log.info("设置已保存到 %s：%s", path, {k: ("已设置" if v else "未设置") if k in consts.SECRET_FIELDS else v
                                               for k, v in updates.items()})
        if hotkeys_changed and self.hotkeys:
            self._start_hotkeys()
        dota_msg = self._apply_dota_dir() if dota_changed else ""
        self.message = (dota_msg if dota_msg
                        else "设置已保存。账号改了要重启助手才会重新拉个人数据" if account_changed
                        else "设置已保存，热键已换好" if hotkeys_changed
                        else "设置已保存，AI 打法下一局按新设置")

    def _apply_dota_dir(self) -> str:
        """设置里改了 Dota 目录：重新找一次，GSI 配置马上写进去（不用重启助手，但 Dota 要重开才读）"""
        if not self.use_gsi:
            return ""
        self.dota_dir = find_dota(self.cfg.dota_dir)
        if self.dota_dir is None:
            return "设置已保存，但还是没找到 Dota 目录，阵营先在控制面板手动选"
        written = ensure_cfg(self.dota_dir, self.cfg.gsi_port)
        if written:
            log.info("GSI 配置写进了 %s", written)
            return f"设置已保存，GSI 配置写进了 {self.dota_dir}：重开 Dota 才生效"
        return f"设置已保存，Dota 目录 {self.dota_dir}（GSI 配置已经在了）"

    def settings_view(self) -> dict:
        """给面板「设置」窗口看的；密钥本身不发给页面，只给末 4 位让玩家认得出是哪个"""
        c = self.cfg
        return {"account_id": c.account_id, "duo_account_id": c.duo_account_id, "stratz_token": consts._mask(c.stratz_token), "ai_plan": c.ai_plan,
                "ai_provider": c.ai_provider, "ai_model": c.ai_model, "ai_api_key": consts._mask(c.ai_api_key),
                "ai_base_url": c.ai_base_url, "providers": llm.PROVIDERS,
                "dota_dir": c.dota_dir, "dota_found": str(self.dota_dir or ""),
                "hotkeys": [{"field": f, "key": getattr(c, f), "text": text} for f, (text, _) in consts.HOTKEY_ACTIONS.items()]}
