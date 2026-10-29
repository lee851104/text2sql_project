"""Batch-file rules that hold on every platform, so CI on Linux enforces them too.

Windows 登入時 cmd 以 Big5（950）開啟；批次檔先 `chcp 65001` 再讀後面的中文。切換字碼頁之後，
cmd 讀檔的位置會被多位元組文字弄亂，緊接著的指令可能被讀壞。開機啟動檔曾因此在 `chcp` 後面
多一行中文註解，結果登入時收集器沒有啟動。實際行為由 Windows 上的 test_realtime_launchers 驗證。
"""

from __future__ import annotations

from ingest.validate import PROJECT_ROOT

STARTUP = "開機自動啟動-即時收集.bat"


def test_the_startup_launcher_has_only_ascii_between_chcp_and_start() -> None:
    lines = (PROJECT_ROOT / STARTUP).read_text(encoding="utf-8").splitlines()
    chcp = next(index for index, line in enumerate(lines) if line.startswith("chcp 65001"))
    start = next(index for index, line in enumerate(lines) if line.startswith("start "))

    between = [line for line in lines[chcp + 1 : start] if not line.isascii()]

    assert chcp < start
    assert between == [], "chcp 65001 與 start 之間不能有中文；中文註解請放在 chcp 之前"


def test_powershell_helpers_are_ascii() -> None:
    """Windows PowerShell 5.1 讀沒有 BOM 的 .ps1 用 ANSI（cp950）：中文註解的位元組會吃掉換行，
    下一行程式就被當成註解，而且不會報錯（實測 .python-version 的雜湊因此變成空字串）。"""

    for script in (PROJECT_ROOT / "scripts").glob("*.ps1"):
        raw = script.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") or raw.isascii(), script.name
