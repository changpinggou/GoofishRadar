"""商品趋势与快照归档（T10 / FR-6 趋势部分）。

增量口径（与需求单一致）：
- Δ1d：与昨日快照的差值；相邻快照间隔 >1 天视为「数据中断后恢复」，
  增量按最近一次快照计算并在趋势中标注；
- Δ7d：与 7 天前快照的差值，不足 7 天取最早可得；
- 商品消失（最后快照早于参考日）：该商品不再进入当日候选与飙升榜
  （无当前值即无增量），趋势查询中标注「数据中断」，最后已知状态冻结展示；
- 飙升榜核对：store_top_deltas 用 SQL 独立计算当日最大增量，
  供与 T06 评分产出（rankings["rising"]）交叉核验。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .storage import Store

CSV_HEADERS = ["snapshot_date", "want_count", "view_count", "price", "status",
               "source_keyword", "delta_want_1d", "delta_view_1d",
               "delta_want_7d", "delta_view_7d", "interrupted"]


@dataclass
class TrendPoint:
    snapshot_date: str
    want_count: int | None
    view_count: int | None
    price: float | None
    status: str
    source_keyword: str
    delta_want_1d: int | None = None
    delta_view_1d: int | None = None
    delta_want_7d: int | None = None
    delta_view_7d: int | None = None
    interrupted: bool = False   # 距上一快照 >1 天（数据中断后恢复）


@dataclass
class ItemTrend:
    item_id: str
    title: str | None
    item_url: str
    reference_date: str             # 参考日（缺省=今天）
    first_snapshot: str | None = None
    last_snapshot: str | None = None
    interrupted_now: bool = False   # 最后快照早于参考日 → 采集已中断（口径冻结）
    points: list[TrendPoint] = field(default_factory=list)

    @property
    def days(self) -> int:
        return len(self.points)


def item_trend(store: Store, item_id: str,
               reference_date: str | None = None) -> ItemTrend | None:
    """商品逐日快照趋势（含 Δ1d/Δ7d 与中断标注）。商品不存在 → None。"""
    ref = reference_date or datetime.now().strftime("%Y-%m-%d")
    item = store._conn.execute(
        "SELECT item_id, title, item_url FROM item WHERE item_id=?",
        (item_id,)).fetchone()
    if not item:
        return None
    trend = ItemTrend(item_id=item_id, title=item["title"],
                      item_url=item["item_url"], reference_date=ref)

    snaps = store.snapshots_of(item_id)
    if not snaps:
        trend.interrupted_now = True
        return trend

    trend.first_snapshot = snaps[0]["snapshot_date"]
    trend.last_snapshot = snaps[-1]["snapshot_date"]
    trend.interrupted_now = trend.last_snapshot < ref

    prior: list[TrendPoint] = []
    for s in snaps:
        d = s["snapshot_date"]
        point = TrendPoint(
            snapshot_date=d, want_count=s["want_count"],
            view_count=s["view_count"], price=s["price"],
            status=s["status"], source_keyword=s["source_keyword"] or "")
        if prior:
            gap_days = (date.fromisoformat(d)
                        - date.fromisoformat(prior[-1].snapshot_date)).days
            point.interrupted = gap_days > 1
            pw, pv = prior[-1].want_count, prior[-1].view_count
            if pw is not None and point.want_count is not None:
                point.delta_want_1d = point.want_count - pw
            if pv is not None and point.view_count is not None:
                point.delta_view_1d = point.view_count - pv
            # Δ7d 基线：≤ d-7 的最近一点，不足 7 天取最早一点
            target = (date.fromisoformat(d) - timedelta(days=7)).isoformat()
            base = next((p for p in reversed(prior)
                         if p.snapshot_date <= target), prior[0])
            if base.want_count is not None and point.want_count is not None:
                point.delta_want_7d = point.want_count - base.want_count
            if base.view_count is not None and point.view_count is not None:
                point.delta_view_7d = point.view_count - base.view_count
        prior.append(point)
    trend.points = prior
    return trend


def trend_to_csv(trend: ItemTrend) -> str:
    """趋势序列 → CSV 文本（UTF-8，含表头）。"""
    from io import StringIO
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for p in trend.points:
        writer.writerow([p.snapshot_date, p.want_count, p.view_count,
                         p.price, p.status, p.source_keyword,
                         p.delta_want_1d, p.delta_view_1d,
                         p.delta_want_7d, p.delta_view_7d,
                         int(p.interrupted)])
    if trend.interrupted_now:
        writer.writerow([f"# 数据中断：最后快照 {trend.last_snapshot}，"
                         f"参考日 {trend.reference_date}（增量冻结口径）"])
    return buf.getvalue()


def store_top_deltas(store: Store, date_str: str, limit: int = 20) -> list[dict]:
    """SQL 独立计算当日各商品的想要数 Δ1d（取各自最近一次更早快照为基线），
    降序返回——供与 T06 飙升榜交叉核验（T10 验收标准 2）。"""
    rows = store._conn.execute(
        """SELECT s.item_id, s.want_count,
                  s.want_count - (SELECT p.want_count FROM item_snapshot p
                                  WHERE p.item_id = s.item_id
                                    AND p.snapshot_date < s.snapshot_date
                                  ORDER BY p.snapshot_date DESC LIMIT 1) AS delta_1d
           FROM item_snapshot s
           WHERE s.snapshot_date = ? AND s.want_count IS NOT NULL
             AND EXISTS (SELECT 1 FROM item_snapshot p
                         WHERE p.item_id = s.item_id
                           AND p.snapshot_date < s.snapshot_date)
           ORDER BY delta_1d DESC
           LIMIT ?""", (date_str, limit)).fetchall()
    return [{"item_id": r["item_id"], "want_count": r["want_count"],
             "delta_1d": r["delta_1d"]} for r in rows]


_ARCHIVE_HEADERS = ["item_id", "snapshot_date", "price", "want_count",
                    "view_count", "status", "source_keyword", "page_number",
                    "crawled_at"]


def archive_old_snapshots(store: Store, retain_days: int, archive_dir: Path,
                          today: str | None = None) -> dict:
    """超期明细快照归档（导出 CSV 追加后清理，T10 任务 4；明细保留 ≥ retain_days 天）。"""
    ref = today or datetime.now().strftime("%Y-%m-%d")
    cutoff = (date.fromisoformat(ref) - timedelta(days=retain_days)).isoformat()
    rows = store._conn.execute(
        """SELECT item_id, snapshot_date, price, want_count, view_count,
                  status, source_keyword, page_number, crawled_at
           FROM item_snapshot WHERE snapshot_date < ?
           ORDER BY snapshot_date, item_id""", (cutoff,)).fetchall()
    result = {"archived": 0, "cutoff": cutoff, "archive_file": None}
    if not rows:
        return result

    archive_dir.mkdir(parents=True, exist_ok=True)
    out = archive_dir / "snapshots_archive.csv"
    new_file = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(_ARCHIVE_HEADERS)
        for r in rows:
            writer.writerow([r[k] for k in _ARCHIVE_HEADERS])
    store._conn.execute("DELETE FROM item_snapshot WHERE snapshot_date < ?",
                        (cutoff,))
    store._conn.commit()
    result.update({"archived": len(rows), "archive_file": str(out)})
    return result
