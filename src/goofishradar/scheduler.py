"""每日定时调度（T09 / FR-8）：launchd 用户代理 + 错峰包装入口。

结构：
- plist（~/Library/LaunchAgents/com.goofishradar.daily.plist）：按配置的最早时刻触发；
- _daily 入口：按当日是工作日/周末解析目标时刻，±randomize_minutes 随机错峰后执行 run；
- install/uninstall：launchctl load/unload + plist 文件管理。

合规约束：调度仅在本机运行，错峰随机化是风控对策的一部分（FR-10）。
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from .config import Settings
from .paths import ROOT

LABEL = "com.goofishradar.daily"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def earliest_fire(settings: Settings) -> tuple[int, int]:
    """plist 触发时刻 = 工作日/周末目标时刻中较早的一个（实际时刻由 _daily 再校正）。"""
    times = sorted([settings.schedule.resolve_time(False),
                    settings.schedule.resolve_time(True)])
    h, m = times[0].split(":")
    return int(h), int(m)


def build_plist(settings: Settings, python_bin: Path, root: Path) -> bytes:
    hour, minute = earliest_fire(settings)
    log_path = Path(settings.paths.logs_dir) / "launchd_daily.log"
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(python_bin), "-m", "goofishradar._daily"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PYTHONPATH": str(Path(root) / "src"),
                                 "PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(payload)


def _launchctl(args: list[str], check: bool = True) -> None:
    try:
        subprocess.run(["launchctl", *args], check=check,
                       capture_output=True, text=True)
    except FileNotFoundError:
        if check:
            raise


def install_schedule(settings: Settings, *, python_bin: Path | None = None,
                     root: Path | None = None, plist_file: Path | None = None,
                     load: bool = True) -> Path:
    """安装/刷新调度。先 unload 旧项再写 plist 再 load，保证重复安装幂等。"""
    root = Path(root) if root else ROOT
    python_bin = Path(python_bin) if python_bin else root / ".venv" / "bin" / "python"
    pfile = Path(plist_file) if plist_file else plist_path()
    pfile.parent.mkdir(parents=True, exist_ok=True)
    if load and pfile.exists():
        _launchctl(["unload", str(pfile)], check=False)
    pfile.write_bytes(build_plist(settings, python_bin, root))
    if load:
        _launchctl(["load", str(pfile)])
    return pfile


def uninstall_schedule(plist_file: Path | None = None, load: bool = True) -> bool:
    pfile = Path(plist_file) if plist_file else plist_path()
    if not pfile.exists():
        return False
    if load:
        _launchctl(["unload", str(pfile)], check=False)
    pfile.unlink()
    return True


def is_loaded() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, check=False).stdout
    except FileNotFoundError:
        return False
    return any(line.split()[:1] == ["-"] and LABEL in line
               for line in out.splitlines()) or LABEL in out
