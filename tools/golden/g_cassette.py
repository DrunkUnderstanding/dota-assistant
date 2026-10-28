"""网络录放（cassette）+ Windows OCR 录放 + 「check 时不许碰外面」的几道闸。

HTTP：包住 requests.sessions.Session.request（requests.get / post、各模块自己的 Session 最后都走它）。
  键 =（方法, URL, params, data, json 请求体），不含请求头。
  请求头 / timeout / 连接层重试另外核对：
    · 每条请求的「发出去的样子」（产品代码给的 headers、Session 自带的非默认 headers、timeout、适配器的 max_retries）
      按 主机 归类计数，写进快照末尾（data._stratz 的 Authorization / User-Agent: STRATZ_API 搬家时丢了会比出来）；
    · record 时把这份也存进 cassette（"sent"，值里的真 token 一律换回占位符 DUMMY_TOKEN，真 token 不落盘），
      check 时和这次发的比，对不上的记进快照末尾。
  record：真联网，响应按顺序追加进 cassettes/http/<sha1>.json；同一个键请求多次就存多份，回放时按顺序给（给完了重复最后一份）。
  check：只从 cassette 取；取不到抛 CassetteMiss（是 requests.ConnectionError 的子类：产品代码当断网处理，错误会出现在快照里）。
OCR：roles.ocr（PowerShell 调 Windows OCR）按「图片像素的 sha1 + 语言」录放，存 cassettes/ocr.json。
闸（check 模式）：socket 连非本机地址直接报错；PowerShell / claude / node / npm / electron 子进程一律不起（record 时只放行 OCR 的 PowerShell）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import requests
import requests.exceptions
import requests.structures
from requests.models import Response

from g_common import DUMMY_TOKEN


class CassetteMiss(requests.exceptions.ConnectionError):
    """check 模式下 cassette 里没有这个请求"""


def _canon(v):
    """参数 / 请求体 → 可比较的 JSON 值"""
    if v is None:
        return None
    if isinstance(v, bytes):
        return {"bytes": base64.b64encode(v).decode("ascii")}
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


class HttpCassette:
    def __init__(self, root: Path, mode: str, token: str = ""):
        self.dir = root / "http"
        self.mode = mode
        self.token = token
        self.lock = threading.Lock()
        self.served: dict[str, int] = {}      # check：每个键已经给过几份
        self.misses: list[str] = []
        self.calls = 0
        self.sent: Counter = Counter()        # 「发出去的样子」→ 次数（写进快照末尾）
        self.sent_mismatch: list[str] = []    # check：和 cassette 里存的「发出去的样子」对不上的
        self._orig = None

    @staticmethod
    def key(method: str, url: str, params, data, body) -> tuple[str, dict]:
        req = {"method": method.upper(), "url": url, "params": _canon(params), "data": _canon(data), "json": _canon(body)}
        text = json.dumps(req, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(text.encode("utf-8")).hexdigest(), req

    def _path(self, h: str) -> Path:
        return self.dir / f"{h}.json"

    def install(self) -> HttpCassette:
        self._orig = requests.sessions.Session.request
        cas = self

        def request(session, method, url, params=None, data=None, headers=None, *args, **kw):
            return cas.handle(session, method, url, params, data, headers, args, kw)
        requests.sessions.Session.request = request
        return self

    # ---- 请求头 / timeout / 重试 ----
    def _scrub(self, v):
        """真 token（record 时内存里才有）换回占位符：存盘 / 进快照的只有占位符"""
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        v = str(v)
        return v.replace(self.token, DUMMY_TOKEN) if self.token else v

    def sent_info(self, session, url, headers, kw) -> dict:
        defaults = requests.utils.default_headers()
        sess = {k: self._scrub(v) for k, v in (session.headers or {}).items() if defaults.get(k) != v}
        try:
            mr = session.get_adapter(url).max_retries
            am = mr.allowed_methods
            am = sorted(am) if isinstance(am, (set, frozenset, list, tuple)) else am      # frozenset 的顺序随哈希变
            retries = f"total={mr.total} backoff={mr.backoff_factor} methods={am} status={list(mr.status_forcelist or ())}"
        except Exception as e:                       # noqa: BLE001
            retries = f"<取不到：{type(e).__name__}>"
        return {"headers": {str(k): self._scrub(v) for k, v in sorted((headers or {}).items(), key=lambda kv: str(kv[0]))},
                "session_headers": dict(sorted(sess.items())), "timeout": _canon(kw.get("timeout")), "retries": retries,
                "other": sorted(k for k in kw if k not in ("json", "timeout"))}

    def summary(self) -> list[str]:
        out = [f"发出去的样子（按主机归类，共 {sum(self.sent.values())} 次）："] if self.sent else []
        out += [f"  {n} 次  {sig}" for sig, n in sorted(self.sent.items())]
        if self.sent_mismatch:
            out.append(f"和 cassette 里录的请求头 / timeout 对不上的 {len(self.sent_mismatch)} 次：")
            out += [f"  {x}" for x in sorted(set(self.sent_mismatch))]
        return out

    # ---- 处理一个请求 ----
    def handle(self, session, method, url, params, data, headers, args, kw):
        body = kw.get("json")
        h, req = self.key(method, url, params, data, body)
        sent = self.sent_info(session, url, headers, kw)
        sig = f"{method.upper()} {urlsplit(url).netloc} " + json.dumps(sent, ensure_ascii=False, sort_keys=True)
        with self.lock:
            self.calls += 1
            self.sent[sig] += 1
        kw = dict(kw, _golden_sent=sent)
        if self.mode == "record":
            return self._record(h, req, session, method, url, params, data, headers, args, kw)
        return self._replay(h, req, kw.pop("_golden_sent"))

    def _record(self, h, req, session, method, url, params, data, headers, args, kw):
        sent = kw.pop("_golden_sent")
        if headers and self.token:
            headers = {k: (v.replace(DUMMY_TOKEN, self.token) if isinstance(v, str) else v) for k, v in headers.items()}
        try:
            r = self._orig(session, method, url, params, data, headers, *args, **kw)
        except requests.exceptions.RequestException as e:
            self._append(h, req, {"error": type(e).__name__, "message": str(e).replace(self.token or "\0", "***"),
                                  "sent": sent})
            raise
        self._append(h, req, dict(self._dump(r), sent=sent))
        return r

    def _append(self, h, req, resp: dict) -> None:
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self._path(h)
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {"request": req, "responses": []}
            doc["responses"].append(resp)
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def _dump(self, r: Response) -> dict:
        out = {"status": r.status_code, "reason": r.reason, "url": r.url, "encoding": r.encoding,
               "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type",)}}
        content = r.content or b""
        try:
            out["text"] = content.decode("utf-8")
        except UnicodeDecodeError:
            out["b64"] = base64.b64encode(content).decode("ascii")
        return out

    def _replay(self, h, req, sent):
        with self.lock:
            try:
                doc = json.loads(self._path(h).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = None
            if not doc or not doc.get("responses"):
                self.misses.append(f"{req['method']} {req['url']} params={json.dumps(req['params'], ensure_ascii=False)[:200]}")
                raise CassetteMiss(f"cassette 里没有：{req['method']} {req['url']}")
            i = self.served.get(h, 0)
            self.served[h] = i + 1
            resp = doc["responses"][min(i, len(doc["responses"]) - 1)]
            if "sent" in resp and resp["sent"] != sent:      # 老 cassette 没存 "sent" 的不比（快照末尾的摘要照样会比出来）
                self.sent_mismatch.append(f"{req['method']} {req['url']}：录的 {json.dumps(resp['sent'], ensure_ascii=False, sort_keys=True)}"
                                          f" ≠ 这次 {json.dumps(sent, ensure_ascii=False, sort_keys=True)}")
        if "error" in resp:
            exc = getattr(requests.exceptions, resp["error"], None)
            if not (isinstance(exc, type) and issubclass(exc, requests.exceptions.RequestException)):
                exc = requests.exceptions.RequestException
            raise exc(resp["message"])
        r = Response()
        r.status_code = resp["status"]
        r.reason = resp.get("reason") or ""
        r.url = resp.get("url") or req["url"]
        r.encoding = resp.get("encoding")
        r.headers = requests.structures.CaseInsensitiveDict(resp.get("headers") or {})
        r._content = resp["text"].encode("utf-8") if "text" in resp else base64.b64decode(resp.get("b64", ""))
        r._content_consumed = True
        return r


class OcrCassette:
    """包住 roles.ocr(img, lang)。返回值是 roles.Word 的列表"""

    def __init__(self, root: Path, mode: str):
        self.path = root / "ocr.json"
        self.mode = mode
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.misses = 0

    @staticmethod
    def key(img, lang: str) -> str:
        import numpy as np
        a = np.ascontiguousarray(img)
        h = hashlib.sha1(f"{a.shape}|{a.dtype}|{lang}|".encode("utf-8"))
        h.update(a.tobytes())
        return h.hexdigest()

    def wrap(self, real_ocr, word_cls):
        cas = self

        def ocr(img, lang: str = "zh-Hans-CN"):
            k = cas.key(img, lang)
            with cas.lock:
                hit = cas.data.get(k)
            if hit is None and cas.mode == "record":
                try:
                    words = real_ocr(img, lang)
                    hit = {"words": [dict(vars(w)) for w in words]}
                except OSError as e:
                    hit = {"error": str(e)}
                with cas.lock:
                    cas.data[k] = hit
                    cas.path.parent.mkdir(parents=True, exist_ok=True)
                    cas.path.write_text(json.dumps(cas.data, ensure_ascii=False, indent=0), encoding="utf-8")
            if hit is None:
                cas.misses += 1
                raise OSError("OCR cassette 里没有这张图（golden check 模式不调 Windows OCR）")
            if "error" in hit:
                raise OSError(hit["error"])
            return [word_cls(**w) for w in hit["words"]]
        return ocr


# ---------- 闸 ----------

_BLOCKED_EXES = ("claude", "node", "npm", "electron")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def install_guards(mode: str, log: list[str]) -> None:
    """check 模式不许联网 / 起 PowerShell；不管什么模式都不起 AI / Electron，不打开资源管理器、不读注册表找 Steam"""
    orig_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        first = args[0] if isinstance(args, (list, tuple)) and args else str(args)
        exe = os.path.basename(str(first)).lower()
        if any(b in exe for b in _BLOCKED_EXES) or (mode == "check" and "powershell" in exe):
            log.append(f"拦下子进程：{exe}")
            raise OSError(f"golden 模式不起子进程 {exe}")
        return orig_popen_init(self, args, *a, **kw)
    subprocess.Popen.__init__ = popen_init

    if mode == "check":
        orig_connect = socket.socket.connect

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) else str(address)
            if str(host) not in _LOCAL_HOSTS:
                log.append(f"拦下联网：{host}")
                raise OSError(f"golden check 模式不许联网（{host}）")
            return orig_connect(sock, address)
        socket.socket.connect = connect

    # 控制面板「打开日志」会 os.startfile 弹资源管理器
    os.startfile = lambda *a, **kw: log.append(f"os.startfile {a[0] if a else ''}")
    # 别去注册表找 Steam / Dota：find_dota 只看设置里的目录，跟这台机器装没装 Dota 无关
    try:
        import winreg

        def no_reg(*a, **kw):
            raise OSError("golden：不读注册表")
        winreg.OpenKey = no_reg
    except ImportError:
        pass
    import webbrowser
    webbrowser.open = lambda *a, **kw: log.append(f"webbrowser.open {a[0] if a else ''}") or True
