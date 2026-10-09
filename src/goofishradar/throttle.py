"""全局节流与风控硬约束（T11 / FR-10）：把「低频稳定」做成代码层强制。

组成：
- RateLimiter    页间随机延时（配置区间内均匀分布）
- GlobalThrottle 全局节流：保证任意相邻两次页面请求间隔 ≥ 随机区间取值（跨关键词/跨页面）
- RequestAuditor 请求审计：逐条记录当日页面请求（JSONL）——日预算与间隔统计的唯一事实源
- DailyBudget    单日页数预算：达到硬上限即停止采集（部分完成）
- RiskControl    风控冷却：触发验证码/拦截后写 data/RISK_COOLDOWN_UNTIL，
                  冷却期内拒绝发起任何网络请求（小时级档位，默认 120 分钟）
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

AUDIT_FILE_PREFIX = "request_audit_"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class RateLimiter:
    """页间随机延时（保留原 API，供既有调用与测试使用）。"""

    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = float(lo), float(hi)

    def next_delay(self) -> float:
        return random.uniform(self.lo, self.hi)

    def wait(self) -> float:
        d = self.next_delay()
        time.sleep(d)
        return d


class GlobalThrottle:
    """全局节流器：距上一次放行的间隔强制 ≥ 本次随机取值（区间内均匀分布）。"""

    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = float(lo), float(hi)
        self._last_release: float | None = None

    def wait(self) -> float:
        sampled = random.uniform(self.lo, self.hi)
        now = time.monotonic()
        base = self._last_release if self._last_release is not None else now - sampled
        remain = (base + sampled) - now
        if remain > 0:
            time.sleep(remain)
        self._last_release = time.monotonic()
        return sampled

    def reset(self) -> None:
        self._last_release = None


class RequestAuditor:
    """页面请求审计日志（logs/request_audit_YYYY-MM-DD.jsonl，按日分文件）。"""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def _file(self, day: str | None = None) -> Path:
        return self.log_dir / f"{AUDIT_FILE_PREFIX}{day or _today()}.jsonl"

    def record(self, url: str) -> None:
        self.record_at(url, datetime.now())

    def record_at(self, url: str, ts: datetime) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": ts.isoformat(timespec="milliseconds"),
                 "url": (url or "")[:300]}
        with self._file(ts.strftime("%Y-%m-%d")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _rows(self, day: str) -> list[dict]:
        f = self._file(day)
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def count_today(self) -> int:
        return len(self._rows(_today()))

    def gaps_seconds(self, day: str | None = None) -> list[float]:
        """当日相邻请求间隔（秒），按记录顺序。"""
        rows = self._rows(day or _today())
        gaps = []
        for prev, cur in zip(rows, rows[1:]):
            t0 = datetime.fromisoformat(prev["ts"])
            t1 = datetime.fromisoformat(cur["ts"])
            gaps.append((t1 - t0).total_seconds())
        return gaps


class DailyBudget:
    """单日页数预算：审计日志计数 vs 硬上限。"""

    def __init__(self, auditor: RequestAuditor, max_pages: int):
        self.auditor = auditor
        self.max_pages = int(max_pages)

    def used(self) -> int:
        return self.auditor.count_today()

    def remaining(self) -> int:
        return max(0, self.max_pages - self.used())


class RiskControl:
    """风控冷却档位：触发后冷却期内拒绝发起任何网络请求（默认 120 分钟）。"""

    def __init__(self, data_dir: Path, cooldown_minutes: int = 120):
        self.data_dir = Path(data_dir)
        self.cooldown_minutes = int(cooldown_minutes)

    @property
    def marker_path(self) -> Path:
        return self.data_dir / "RISK_COOLDOWN_UNTIL"

    def trigger(self, reason: str = "") -> datetime:
        until = datetime.now() + timedelta(minutes=self.cooldown_minutes)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            f"{until.isoformat(timespec='seconds')} {reason}\n", encoding="utf-8")
        return until

    def active_until(self) -> datetime | None:
        if not self.marker_path.exists():
            return None
        try:
            until = datetime.fromisoformat(
                self.marker_path.read_text(encoding="utf-8").split()[0])
        except (ValueError, IndexError):
            self.clear()
            return None
        if until <= datetime.now():
            self.clear()
            return None
        return until

    def is_active(self) -> bool:
        return self.active_until() is not None

    def clear(self) -> None:
        self.marker_path.unlink(missing_ok=True)
