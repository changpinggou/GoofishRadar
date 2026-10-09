"""launchd 每日入口（T09 任务 2）：按工作日/周末时刻 ± 随机分钟错峰后执行 run。

由 plist 在最早配置时刻拉起；实际执行时刻在此模块内二次校正：
- 工作日 → schedule.weekday_time（缺省 time），周末 → weekend_time；
- ±randomize_minutes 随机错峰（风控对策，FR-10）；
- 若随机后时刻已过（如手动提前触发），顺延 10–90 秒内随机执行。
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta


def compute_run_at(now: datetime, settings) -> datetime:
    weekend = now.weekday() >= 5
    h, m = settings.schedule.resolve_time(weekend).split(":")
    target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    rm = settings.schedule.randomize_minutes
    run_at = target + timedelta(minutes=random.uniform(-rm, rm)) if rm else target
    if run_at < now:
        run_at = now + timedelta(seconds=random.uniform(10, 90))
    return run_at


def main(argv=None) -> int:
    from .cli import main as cli_main
    from .config import load_settings

    settings, _ = load_settings()
    now = datetime.now()
    run_at = compute_run_at(now, settings)
    wait = (run_at - datetime.now()).total_seconds()
    print(f"[launchd] {now:%F %T} 拉起；计划 {run_at:%F %T} 执行 run"
          f"（等待 {wait / 60:.1f} 分钟）", flush=True)
    if wait > 0:
        time.sleep(wait)
    return cli_main(["run"])


if __name__ == "__main__":
    sys.exit(main())
