"""联网基础：OpenDota / STRATZ 地址、带重试的会话、请求函数、出错类型。

别的模块一律用 http._get(...) / http._stratz(...) 这种「模块.名字」形式调用，测试只 patch 这一处。
"""
from __future__ import annotations

import logging
import time

import requests
from urllib3.util.retry import Retry

from ..core.config import Config

OPENDOTA = "https://api.opendota.com/api"
STRATZ = "https://api.stratz.com/graphql"

logger = logging.getLogger(__name__)  # 不叫 log：不少函数有 log 参数（进度提示）


class APIError(RuntimeError):
    """数据源自己报错（STRATZ errors / explorer err）：和断网、503 一样算网络问题"""


NETWORK_ERRORS = (requests.RequestException, APIError)   # 日志里只记一行，不记堆栈（不是程序 bug）


def _say(msg: str) -> None:
    """进度提示：打印出来，同时记进日志"""
    print(msg, flush=True)
    logger.info(msg.strip())

_session = requests.Session()
# 网络不稳时 STRATZ / OpenDota 偶尔直接断连接（SSL EOF / 10054）：连接层自动重试，HTTP 状态码仍由下面自己处理
_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(
    total=6, backoff_factor=1, allowed_methods=None, status_forcelist=(), raise_on_status=False)))


def _get(url: str, **params):
    for attempt in range(4):
        r = _session.get(url, params=params, timeout=120)
        if r.status_code == 429:
            logger.warning("OpenDota 限速（429），%d 秒后重试：%s", 5 * (attempt + 1), url)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 400 and "explorer" in url:
            return r.json()   # explorer 把 SQL 错误 / 超时放在 400 的 err 字段里
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _stratz(cfg: Config, query: str, **variables) -> dict:
    for attempt in range(5):
        r = _session.post(STRATZ, json={"query": query, "variables": variables}, timeout=60,
                          headers={"Authorization": f"Bearer {cfg.stratz_token}", "User-Agent": "STRATZ_API"})
        if r.status_code != 429:
            break
        logger.warning("STRATZ 限速（429），%d 秒后重试", 3 * (attempt + 1))
        time.sleep(3 * (attempt + 1))      # 免费 token 有每秒 / 每分钟限速
    r.raise_for_status()
    res = r.json()
    if res.get("errors"):
        logger.warning("STRATZ 返回错误：%s", res["errors"])
        raise APIError(f"STRATZ 出错：{res['errors'][0].get('message')}")
    return res["data"]


def _sql(q: str) -> list[dict]:
    res = _get(f"{OPENDOTA}/explorer", sql=q)
    if res.get("err"):
        raise APIError(f"OpenDota explorer 出错：{res['err']}")
    return res["rows"]


# STRATZ 的段位只分到两档一组
_STRATZ_BRACKETS = {1: "HERALD_GUARDIAN", 2: "HERALD_GUARDIAN", 3: "CRUSADER_ARCHON", 4: "CRUSADER_ARCHON",
                    5: "LEGEND_ANCIENT", 6: "LEGEND_ANCIENT", 7: "DIVINE_IMMORTAL", 8: "DIVINE_IMMORTAL"}


def _stratz_brackets(cfg: Config) -> list[str]:
    if not (cfg.rank_min and cfg.rank_max):
        return ["ALL"]
    return sorted({_STRATZ_BRACKETS[m] for m in range(cfg.rank_min, cfg.rank_max + 1)})
