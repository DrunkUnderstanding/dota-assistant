"""语音播报：常驻一个 PowerShell 进程调用 Windows 自带的 System.Speech（SAPI），不需要装任何 Python 包。
新的一句会打断还没念完的上一句，避免排队念过时的推荐。"""
from __future__ import annotations

import subprocess

_SCRIPT = r"""
[Console]::InputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1
if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) } else { [Console]::Error.WriteLine('NO_ZH_VOICE') }
$s.Rate = __RATE__
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($line -eq $null) { break }
    $s.SpeakAsyncCancelAll()
    if ($line.Length -gt 0) { [void]$s.SpeakAsync($line) }
}
"""


class Speaker:
    def __init__(self, rate: int = 0, enabled: bool = True):
        self.enabled = enabled
        self._proc: subprocess.Popen | None = None
        self._rate = max(-10, min(10, rate))

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 _SCRIPT.replace("__RATE__", str(self._rate))],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self._proc

    def say(self, text: str) -> None:
        if not self.enabled:
            return
        proc = self._ensure()
        try:
            proc.stdin.write((text.replace("\n", "，") + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            self._proc = None

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.stdin.close()
            self._proc.wait(timeout=3)
