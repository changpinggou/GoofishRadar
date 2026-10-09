"""T10 单元测试：逐日趋势/中断口径/消失冻结/归档幂等/飙升榜 SQL 交叉核对。"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

import pytest

from goofishradar.config import RankingCfg
from goofishradar.ranking import build_rankings, rankings_to_dict
from goofishradar.ranking import ScoreInput
from goofishradar.parser import ItemRecord
from goofishradar.storage import Store
from goofishradar.trend import (
    archive_old_snapshots, item_trend, store_top_deltas, trend_to_csv)


def rec(item_id: str, want: int | None, keyword: str = "iPhone",
        view: int | None = None) -> ItemRecord:
    return ItemRecord(
        item_id=item_id, title=f"商品{item_id}", price=100.0, want_count=want,
        view_count=view, publish_time=None, status="在售", seller_nick="s",
        seller_id="1", location="上海", pic_url="p",
        item_url=f"https://www.goofish.com/item?id={item_id}", tags=[],
        source_keyword=keyword, source_sort="", page_number=1,
        crawled_at=f"{keyword}-{item_id}")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def seed_day(store, day: str, items: list[tuple[str, int]]):
    store.upsert_items([rec(i, w) for i, w in items], day)


# ---- 逐日趋势（T10 任务 1/3）----

class TestItemTrend:
    def test_contiguous_days_deltas_and_sequence(self, store):
        seed_day(store, "2026-10-01", [("a", 10)])
        seed_day(store, "2026-10-02", [("a", 15)])
        seed_day(store, "2026-10-03", [("a", 12)])
        trend = item_trend(store, "a", reference_date="2026-10-03")
        assert trend is not None and trend.days == 3
        assert trend.interrupted_now is False
        assert [p.delta_want_1d for p in trend.points] == [None, 5, -3]
        assert [p.delta_want_7d for p in trend.points] == [None, 5, 2]  # 不足7天取最早
        assert all(not p.interrupted for p in trend.points)

    def test_gap_marked_as_interruption(self, store):
        seed_day(store, "2026-10-01", [("a", 10)])
        seed_day(store, "2026-10-04", [("a", 20)])     # 间隔 3 天
        trend = item_trend(store, "a", reference_date="2026-10-04")
        assert trend.points[1].interrupted is True
        assert trend.points[1].delta_want_1d == 10     # 按最近一次快照计算
        assert trend.points[0].interrupted is False

    def test_missing_item_returns_none_and_unknown_shows_interrupted(self, store):
        assert item_trend(store, "nope") is None
        seed_day(store, "2026-10-01", [("a", 10)])
        # 参考日 10-09：最后快照 10-01 → 采集已中断（消失口径：冻结 + 标注）
        trend = item_trend(store, "a", reference_date="2026-10-09")
        assert trend.interrupted_now is True
        assert trend.last_snapshot == "2026-10-01"
        assert trend.points[-1].delta_want_1d is None  # 无后续采集 → 无新增量
        csv_text = trend_to_csv(trend)
        assert "数据中断" in csv_text

    def test_csv_headers_and_rows(self, store):
        seed_day(store, "2026-10-01", [("a", 10)])
        seed_day(store, "2026-10-02", [("a", 15)])
        trend = item_trend(store, "a", reference_date="2026-10-02")
        rows = list(csv.reader(io.StringIO(trend_to_csv(trend))))
        assert rows[0][:3] == ["snapshot_date", "want_count", "view_count"]
        assert len(rows) == 3
        assert rows[2][6] == "5"                       # delta_want_1d

    def test_view_deltas(self, store):
        # 浏览量：列表页暂不可得 → 快照值为 None → 增量保持 None（FR-4 补齐后自动生效）
        seed_day(store, "2026-10-01", [("a", 10)])
        seed_day(store, "2026-10-02", [("a", 15)])
        trend = item_trend(store, "a", reference_date="2026-10-02")
        assert all(p.view_count is None for p in trend.points)
        assert all(p.delta_view_1d is None for p in trend.points)


# ---- 飙升榜交叉核对（T10 验收标准 2）----

class TestRisingCrossCheck:
    def test_rising_list_matches_sql_max_delta(self, store):
        seed_day(store, "2026-10-08", [("a", 10), ("b", 50), ("c", 5)])
        seed_day(store, "2026-10-09", [("a", 30), ("b", 60), ("c", 4)])
        # SQL 独立口径
        top = store_top_deltas(store, "2026-10-09")
        assert top[0]["item_id"] == "a" and top[0]["delta_1d"] == 20
        assert top[1]["delta_1d"] == 10 and top[2]["delta_1d"] == -1
        # T06 飙升榜（同一库数据）：基线为昨日快照
        items = []
        for iid, want, base in (("a", 30, 10), ("b", 60, 50), ("c", 4, 5)):
            r = rec(iid, want)
            r.source_keyword = "k"
            items.append(ScoreInput(r, [("2026-10-08", base, None)]))
        out = build_rankings({"k": items}, "2026-10-09", RankingCfg())
        rising = [s.item_id for s in out["rising"]]
        assert rising == [t["item_id"] for t in top]


# ---- 归档（T10 任务 4）----

class TestArchive:
    def test_archive_moves_old_keeps_recent_idempotent(self, store, tmp_path):
        store.upsert_items([rec("old", 1)], "2026-07-01")    # ~100 天前
        store.upsert_items([rec("new", 2)], "2026-10-08")    # 近期
        archive_dir = tmp_path / "archive"

        r1 = archive_old_snapshots(store, retain_days=90,
                                   archive_dir=archive_dir, today="2026-10-09")
        assert r1["archived"] == 1
        assert store._conn.execute("SELECT COUNT(*) FROM item_snapshot").fetchone()[0] == 1
        assert store._conn.execute(
            "SELECT item_id FROM item_snapshot").fetchone()[0] == "new"

        # CSV 内容完整（表头 + 1 行）
        text = (archive_dir / "snapshots_archive.csv").read_text(encoding="utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0][0] == "item_id" and rows[1][0] == "old"

        # 幂等：再次归档为 0，CSV 不重复追加
        r2 = archive_old_snapshots(store, retain_days=90,
                                   archive_dir=archive_dir, today="2026-10-09")
        assert r2["archived"] == 0
        assert len(list(csv.reader(io.StringIO(text)))) == 2

    def test_archive_appends_across_runs(self, store, tmp_path):
        archive_dir = tmp_path / "archive"
        store.upsert_items([rec("x1", 1)], "2026-07-01")
        archive_old_snapshots(store, 90, archive_dir, today="2026-10-09")
        store.upsert_items([rec("x2", 2)], "2026-07-02")
        archive_old_snapshots(store, 90, archive_dir, today="2026-10-09")
        rows = list(csv.reader(io.StringIO(
            (archive_dir / "snapshots_archive.csv").read_text(encoding="utf-8"))))
        assert len(rows) == 3                                # 表头 + 2 行
        assert {rows[1][0], rows[2][0]} == {"x1", "x2"}
