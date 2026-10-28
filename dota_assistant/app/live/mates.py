"""队友战绩：控制面板填好友代码查。"""
from __future__ import annotations

import logging
import threading
import time

from ...domain import mates
from ...sources import http

log = logging.getLogger(__name__)


class MatesMixin:
    def query_mate(self, code, force: bool = False) -> None:
        """查队友战绩：后台线程联网，查完排进工作线程写回。20 分钟内查过的不重查（force = 面板上点了「刷新」）"""
        aid = mates.parse_code(code)
        e = self.mates.touch(aid)
        if e["busy"]:
            return
        if not force and mates.Mates.fresh(e):
            self.mates.save()
            self.message = f"{e['data']['name']} 刚查过，要重查点「刷新」"
            return
        e["busy"], e["error"] = True, ""
        self.message = f"正在查好友代码 {aid} 的战绩…"

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._mate_fetch(self.cfg, aid), ""
                log.info("队友战绩 %s 查好了（%.1f 秒）", aid, time.monotonic() - t0)
            except LookupError as ex:
                res, err = None, str(ex)
            except http.NETWORK_ERRORS as ex:     # 断网 / 限速：一行就够，不记堆栈
                log.warning("队友战绩 %s 没查到：%s", aid, ex)
                res, err = None, f"联网失败：{ex}"
            except Exception as ex:
                log.exception("队友战绩 %s 出错", aid)
                res, err = None, f"出错：{ex}（详情见日志）"
            self.submit(self._mate_ready, aid, res, err)
        threading.Thread(target=work, daemon=True, name="mate").start()

    def _mate_ready(self, aid: int, res: dict | None, err: str) -> None:
        self.mates.done(aid, res, err)
        self.message = err or (f"{res['name']} 的战绩查好了" if res else "")
