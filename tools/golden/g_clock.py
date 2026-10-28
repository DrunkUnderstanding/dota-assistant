"""假时钟：让快照不随「今天几号、几点」变。

- time.time() 冻在 meta.json 记的冻结时刻 E（cli 模式下再加上进程里真实过去的秒数，保留「耗时」的意思）；
  冻结目录里所有文件的 mtime 也设成 E，缓存（brief / coach 按 mtime 判断几天内）永远算新鲜，不会过期去联网。
- virtual 模式（Live 回放）：time.time / time.monotonic 都只在 advance() 时走，time.sleep 也只是拨时钟、不真等，
  死亡卡片「至少显示 3 秒」、认位置「隔 3 秒再试」这类按秒的逻辑完全可控。
- virtual 模式下 datetime.now / 不带参数的 strftime / localtime 也跟着假时钟走（文件名里的时间固定）。
- shift_days ≠ 0：模拟「隔 N 天再跑」——datetime.now / date.today / 不带参数的 time.strftime / localtime 都往后拨 N 天
  （time.time 仍是 E，和真的隔天跑一样：E 是冻结的）。要在 import dota_assistant 之前装，`from datetime import datetime`
  才会拿到换过的类。

只换 `time` / `datetime` 模块上的属性；threading / queue 在 import 时就绑了真的 monotonic，不受影响。
"""
from __future__ import annotations

import datetime as _dt
import time as _time

_real_time = _time.time
_real_monotonic = _time.monotonic
_real_sleep = _time.sleep
_real_localtime = _time.localtime
_real_strftime = _time.strftime
_real_gmtime = _time.gmtime
_real_ctime = _time.ctime
_RealDatetime = _dt.datetime
_RealDate = _dt.date

MONO_BASE = 100_000.0


class Clock:
    def __init__(self, epoch: float, mode: str, shift_days: int = 0, sleep: str = "real"):
        """mode：cli（E + 真实流逝）/ virtual（只在 advance 时走）。sleep：real / skip（check 时不真等）"""
        self.epoch = float(epoch)
        self.mode = mode
        self.shift = shift_days * 86400
        self.sleep_mode = sleep
        self.offset = 0.0                 # virtual：已经拨过去的秒数
        self._start = _real_monotonic()

    # ---- 假函数 ----
    def time(self) -> float:
        if self.mode == "virtual":
            return self.epoch + self.offset
        return self.epoch + (_real_monotonic() - self._start)

    def monotonic(self) -> float:
        return MONO_BASE + self.offset

    def sleep(self, secs: float) -> None:
        if self.mode == "virtual":
            self.advance(max(0.0, float(secs)))
        elif self.sleep_mode == "real":
            _real_sleep(secs)

    def advance(self, secs: float) -> None:
        self.offset += float(secs)

    def _wall(self) -> float:
        """「墙上时间」（datetime.now 那一类用）：cli 模式是真实时间 + 平移；virtual 模式跟着假时钟走 + 平移
        （回放跑得比真实快，死亡推送 / 截图的文件名按真实秒取，有时撞名有时不撞，结果不固定）"""
        base = self.time() if self.mode == "virtual" else _real_time()
        return base + self.shift

    def install(self) -> Clock:
        _time.time = self.time
        _time.sleep = self.sleep
        if self.mode == "virtual":
            _time.monotonic = self.monotonic
        if self.shift or self.mode == "virtual":
            self._install_shift()
        return self

    def _install_shift(self) -> None:
        clock = self

        def localtime(secs=None):
            return _real_localtime(clock._wall() if secs is None else secs)

        def gmtime(secs=None):
            return _real_gmtime(clock._wall() if secs is None else secs)

        def strftime(fmt, t=None):
            return _real_strftime(fmt, localtime() if t is None else t)

        def ctime(secs=None):
            return _real_ctime(clock._wall() if secs is None else secs)

        class FakeDatetime(_RealDatetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock._wall(), tz)

            @classmethod
            def today(cls):
                return cls.fromtimestamp(clock._wall())

            @classmethod
            def utcnow(cls):
                return cls.fromtimestamp(clock._wall(), _dt.timezone.utc).replace(tzinfo=None)

        class FakeDate(_RealDate):
            @classmethod
            def today(cls):
                return cls.fromtimestamp(clock._wall())

        _time.localtime, _time.gmtime, _time.strftime, _time.ctime = localtime, gmtime, strftime, ctime
        _dt.datetime, _dt.date = FakeDatetime, FakeDate
