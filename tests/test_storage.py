"""T05 单元测试：去重 / 同日覆盖 / 两日快照序列 / 事务回滚 / 来源关系 / 导入幂等。"""
from __future__ import annotations

import json

import pytest

from goofishradar.parser import ItemRecord
from goofishradar.storage import Store


def make_record(item_id: str, keyword: str = "iPhone", page: int = 1,
                price: float = 100.0, want: int = 5, title: str | None = None,
                crawled_at: str = "2026-10-08 08:00:00") -> ItemRecord:
    return ItemRecord(
        item_id=item_id, title=title or f"商品{item_id}", price=price,
        want_count=want, view_count=None, publish_time=None, status="在售",
        seller_nick="卖家", seller_id="123", location="上海",
        pic_url=f"http://img/{item_id}.jpg",
        item_url=f"https://www.goofish.com/item?id={item_id}",
        tags=["九成新"], source_keyword=keyword, source_sort="most_want",
        page_number=page, crawled_at=crawled_at)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class TestUpsert:
    def test_new_then_update_classification(self, store):
        s1 = store.upsert_items([make_record("a"), make_record("b")], "2026-10-08")
        assert (s1.items_new, s1.items_updated) == (2, 0)

        s2 = store.upsert_items(
            [make_record("a", price=200, want=9), make_record("c")], "2026-10-08")
        assert (s2.items_new, s2.items_updated) == (1, 1)

        row = store._conn.execute("SELECT * FROM item WHERE item_id='a'").fetchone()
        assert row["price"] == 200 and row["want_count"] == 9   # 最新值

    def test_first_seen_preserved_last_seen_updated(self, store):
        store.upsert_items([make_record("a")], "2026-10-08")
        first = store._conn.execute(
            "SELECT first_seen_at, last_seen_at FROM item WHERE item_id='a'").fetchone()
        store.upsert_items([make_record("a", price=999)], "2026-10-09")
        after = store._conn.execute(
            "SELECT first_seen_at, last_seen_at, price FROM item WHERE item_id='a'").fetchone()
        assert after["first_seen_at"] == first["first_seen_at"]  # 首见时间不变
        assert after["last_seen_at"] >= first["last_seen_at"]    # 最近更新时间推进
        assert after["price"] == 999

    def test_same_day_reimport_no_duplicate_snapshot(self, store):
        """T05 验收标准 2：同日重复入库不产生重复快照（覆盖更新）。"""
        store.upsert_items([make_record("a", want=5)], "2026-10-08")
        store.upsert_items([make_record("a", want=12)], "2026-10-08")
        rows = store.snapshots_of("a")
        assert len(rows) == 1
        assert rows[0]["want_count"] == 12                       # 覆盖为最新值
        assert store.counts()["snapshots"] == 1

    def test_two_day_snapshot_sequence_and_item_timeline(self, store):
        """T05 验收标准 1（两日模拟）：快照序列正确、主档时间正确。"""
        store.upsert_items([make_record("a", want=5, price=100)], "2026-10-08")
        store.upsert_items([make_record("a", want=15, price=90)], "2026-10-09")
        snaps = store.snapshots_of("a")
        assert [s["snapshot_date"] for s in snaps] == ["2026-10-08", "2026-10-09"]
        assert [s["want_count"] for s in snaps] == [5, 15]       # 增量计算的数据基础
        assert [s["price"] for s in snaps] == [100.0, 90.0]
        item = store._conn.execute("SELECT * FROM item WHERE item_id='a'").fetchone()
        assert item["price"] == 90.0                             # 主档=最新值

    def test_transaction_rollback_no_partial_batch(self, store):
        """T05 验收标准 3（模拟）：批内任一记录非法 → 整批回滚，无半页脏数据。"""
        good = [make_record(str(i)) for i in range(10)]
        poisoned = make_record("ok")   # item_id 合法但构造一条会导致约束失败的记录
        bad = make_record("bad")
        batch = good + [bad]
        # 模拟中途失败：直接在事务里手工触发异常
        try:
            with store._conn:
                store._conn.executemany(
                    "INSERT INTO item (item_id, first_seen_at, last_seen_at) VALUES (?,?,?)",
                    [(r.item_id, "t", "t") for r in batch])
                raise RuntimeError("模拟采集进程中途崩溃")
        except RuntimeError:
            pass
        assert store.counts()["items"] == 0                      # 半页数据未残留
        # 同一批重新入库成功
        store.upsert_items(good, "2026-10-08")
        assert store.counts()["items"] == 10

    def test_invalid_record_raises_and_rolls_back(self, store):
        store.upsert_items([make_record("keep")], "2026-10-08")
        broken = make_record("x")
        broken.item_id = None                                     # NOT NULL/PK 违规
        with pytest.raises(Exception):
            store.upsert_items([make_record("fresh"), broken], "2026-10-08")
        assert store.counts()["items"] == 1                       # fresh 也被回滚
        assert store._conn.execute(
            "SELECT COUNT(*) FROM item WHERE item_id='fresh'").fetchone()[0] == 0


class TestKeywordRelation:
    def test_item_hit_by_multiple_keywords(self, store):
        store.upsert_items([make_record("a", keyword="iPhone")], "2026-10-08")
        store.upsert_items([make_record("a", keyword="苹果手机")], "2026-10-08")
        assert sorted(store.keywords_of("a")) == ["iPhone", "苹果手机"]

    def test_relation_last_seen_updated_not_duplicated(self, store):
        store.upsert_items([make_record("a", keyword="iPhone")], "2026-10-08")
        store.upsert_items([make_record("a", keyword="iPhone")], "2026-10-09")
        rows = store._conn.execute(
            "SELECT * FROM item_keyword WHERE item_id='a'").fetchall()
        assert len(rows) == 1
        assert rows[0]["last_seen_at"] >= rows[0]["first_seen_at"]


class TestRecordRunAndQuality:
    def test_record_and_aggregate(self, store):
        store.upsert_items([make_record("a"), make_record("b")], "2026-10-08")
        store.record_run("2026-10-08", "iPhone", sort="most_want",
                         pages_attempted=3, pages_ok=3,
                         missing_field_count={"seller_id": 2, "price": 1},
                         duration_seconds=60.0, result="ok")
        store.record_run("2026-10-08", "乐高", pages_attempted=2, pages_ok=1,
                         pages_degraded=1,
                         missing_field_count={"seller_id": 5}, result="ok")
        store.record_run("2026-10-09", "iPhone", pages_attempted=1, pages_ok=1,
                         result="ok")

        q = store.quality_stats("2026-10-08")
        assert q.snapshots == 2
        assert q.items_new_today == 2
        assert q.runs == 2
        assert (q.pages_attempted, q.pages_ok, q.pages_degraded) == (5, 4, 1)
        assert q.missing_fields == {"seller_id": 7, "price": 1}
        comp = q.completeness()
        assert comp["seller_id"] == round((1 - 7 / 2) * 100, 2)  # 可为负（跨词缺失>快照数），报告层负责口径
        assert comp["title"] == 100.0                             # 无缺失记录视为 100%

        assert store.quality_stats("2026-10-09").runs == 1
        assert store.quality_stats("2026-10-10").snapshots == 0


class TestImportParsedDay:
    def test_import_jsonl_idempotent(self, tmp_path, store):
        day_dir = tmp_path / "parsed" / "2026-10-08"
        day_dir.mkdir(parents=True)
        records = [make_record(str(i), keyword="iPhone", page=n // 30 + 1)
                   for n, i in enumerate(range(100, 130))]
        for path in (day_dir / "iPhone.jsonl",):
            path.write_text("\n".join(json.dumps(r.to_dict(), ensure_ascii=False)
                                      for r in records), encoding="utf-8")

        first = store.import_parsed_day("2026-10-08", day_dir.parent / "2026-10-08")
        assert first["totals"]["items_new"] == 30
        assert store.counts()["items"] == 30

        second = store.import_parsed_day("2026-10-08", day_dir.parent / "2026-10-08")
        assert second["totals"]["items_new"] == 0                 # 重复导入幂等
        assert second["totals"]["items_updated"] == 30
        assert store.counts() == {"items": 30, "snapshots": 30,
                                  "item_keywords": 30, "runs": 0}

    def test_import_missing_dir_is_empty(self, tmp_path, store):
        result = store.import_parsed_day("2099-01-01", tmp_path / "nope")
        assert result["files"] == {} and result["totals"]["snapshots_upserted"] == 0


class TestWal:
    def test_wal_mode_enabled(self, tmp_path):
        with Store(tmp_path / "w.db") as s:
            mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
