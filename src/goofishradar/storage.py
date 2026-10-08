"""数据存储与去重入库（T05 / FR-5）：SQLite（WAL）双层存储。

层次：
- item            商品主档（item_id 主键，最新值 + first_seen/last_seen）
- item_snapshot   每日快照（(item_id, snapshot_date) 唯一，同日重采覆盖不重复）
- item_keyword    商品-关键词来源关系（一商品可被多词命中，榜单按来源任务分组）
- crawl_run       每次任务运行记录（页数/条数/耗时/结果/质量计数）

事务：按批（每次 upsert_items 调用）单事务提交，失败整体回滚 —— 中途 kill 进程
重跑不会留下半页脏数据（T05 验收标准 3，实测见 T05-VERIFICATION.md）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .parser import CORE_FIELDS, ItemRecord
from .paths import DATA_DIR

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS item (
    item_id        TEXT PRIMARY KEY,
    title          TEXT,
    price          REAL,
    want_count     INTEGER,
    view_count     INTEGER,
    publish_time   TEXT,
    status         TEXT,
    seller_nick    TEXT,
    seller_id      TEXT,
    location       TEXT,
    pic_url        TEXT,
    item_url       TEXT,
    tags           TEXT,          -- JSON 数组
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    last_keyword   TEXT
);

CREATE TABLE IF NOT EXISTS item_snapshot (
    item_id        TEXT NOT NULL,
    snapshot_date  TEXT NOT NULL,           -- YYYY-MM-DD
    price          REAL,
    want_count     INTEGER,
    view_count     INTEGER,
    status         TEXT,
    source_keyword TEXT,
    page_number    INTEGER,
    crawled_at     TEXT,
    PRIMARY KEY (item_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS item_keyword (
    item_id        TEXT NOT NULL,
    keyword        TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    PRIMARY KEY (item_id, keyword)
);

CREATE TABLE IF NOT EXISTS crawl_run (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date           TEXT NOT NULL,
    keyword            TEXT NOT NULL,
    sort               TEXT,
    pages_attempted    INTEGER DEFAULT 0,
    pages_ok           INTEGER DEFAULT 0,
    pages_degraded     INTEGER DEFAULT 0,
    items_new          INTEGER DEFAULT 0,
    items_updated      INTEGER DEFAULT 0,
    items_total        INTEGER DEFAULT 0,
    missing_field_count TEXT,                -- JSON
    duration_seconds   REAL,
    result             TEXT DEFAULT 'ok',    -- ok / failed / risk_blocked / partial
    error              TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_date ON item_snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_item_keyword_kw ON item_keyword(keyword);
CREATE INDEX IF NOT EXISTS idx_run_date ON crawl_run(run_date);
"""

_ITEM_COLUMNS = ("item_id", "title", "price", "want_count", "view_count",
                 "publish_time", "status", "seller_nick", "seller_id",
                 "location", "pic_url", "item_url", "tags")


@dataclass
class UpsertStats:
    items_new: int = 0
    items_updated: int = 0
    snapshots_upserted: int = 0
    keyword_links_upserted: int = 0


@dataclass
class DayQuality:
    date: str
    snapshots: int = 0                     # 当日快照数 = 采集条数
    items_total: int = 0                   # 库内商品总数
    items_new_today: int = 0               # 当日首次入库
    runs: int = 0
    pages_attempted: int = 0
    pages_ok: int = 0
    pages_degraded: int = 0                # 解析失败页数
    missing_fields: dict = field(default_factory=dict)

    def completeness(self) -> dict[str, float]:
        """字段完整率（分母=当日快照数；无缺失记录的字段视为 100%）。"""
        if self.snapshots == 0:
            return {}
        fields = set(CORE_FIELDS) | {"seller_nick", "seller_id"}
        return {f: round((1 - self.missing_fields.get(f, 0) / self.snapshots) * 100, 2)
                for f in sorted(fields)}


def _chunks(seq: list, size: int = 500):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class Store:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else DATA_DIR / "goofishradar.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # ---- 上下文管理 ----

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 入库（T05 任务 1/2/3）----

    def upsert_items(self, records: list[ItemRecord], snapshot_date: str) -> UpsertStats:
        """主档更新最新值（first_seen 不变）+ 当日快照覆盖 + 来源关系更新。

        整批单事务：任一记录非法导致异常时整体回滚（T05 验收标准 3）。
        """
        stats = UpsertStats()
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn:  # 事务：异常自动回滚
            ids = [r.item_id for r in records]
            existing: set[str] = set()
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                existing |= {row[0] for row in self._conn.execute(
                    f"SELECT item_id FROM item WHERE item_id IN ({marks})", chunk)}
            stats.items_new = len([i for i in ids if i not in existing])
            stats.items_updated = len(existing)

            self._conn.executemany(
                """INSERT INTO item (item_id, title, price, want_count, view_count,
                     publish_time, status, seller_nick, seller_id, location,
                     pic_url, item_url, tags, first_seen_at, last_seen_at, last_keyword)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     title=excluded.title, price=excluded.price,
                     want_count=excluded.want_count, view_count=excluded.view_count,
                     publish_time=excluded.publish_time, status=excluded.status,
                     seller_nick=excluded.seller_nick, seller_id=excluded.seller_id,
                     location=excluded.location, pic_url=excluded.pic_url,
                     item_url=excluded.item_url, tags=excluded.tags,
                     last_seen_at=excluded.last_seen_at,
                     last_keyword=excluded.last_keyword""",
                [(r.item_id, r.title, r.price, r.want_count, r.view_count,
                  r.publish_time, r.status, r.seller_nick, r.seller_id,
                  r.location, r.pic_url, r.item_url,
                  json.dumps(r.tags, ensure_ascii=False), now, now,
                  r.source_keyword) for r in records])

            self._conn.executemany(
                """INSERT INTO item_snapshot (item_id, snapshot_date, price,
                     want_count, view_count, status, source_keyword, page_number, crawled_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id, snapshot_date) DO UPDATE SET
                     price=excluded.price, want_count=excluded.want_count,
                     view_count=excluded.view_count, status=excluded.status,
                     source_keyword=excluded.source_keyword,
                     page_number=excluded.page_number, crawled_at=excluded.crawled_at""",
                [(r.item_id, snapshot_date, r.price, r.want_count, r.view_count,
                  r.status, r.source_keyword, r.page_number, r.crawled_at)
                 for r in records])
            stats.snapshots_upserted = len(records)

            self._conn.executemany(
                """INSERT INTO item_keyword (item_id, keyword, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(item_id, keyword) DO UPDATE SET
                     last_seen_at=excluded.last_seen_at""",
                [(r.item_id, r.source_keyword, now, now) for r in records
                 if r.source_keyword])
            stats.keyword_links_upserted = sum(1 for r in records if r.source_keyword)
        return stats

    def record_run(self, run_date: str, keyword: str, sort: str = "",
                   pages_attempted: int = 0, pages_ok: int = 0,
                   pages_degraded: int = 0, items_new: int = 0,
                   items_updated: int = 0, items_total: int = 0,
                   missing_field_count: dict | None = None,
                   duration_seconds: float | None = None, result: str = "ok",
                   error: str | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO crawl_run (run_date, keyword, sort, pages_attempted,
                 pages_ok, pages_degraded, items_new, items_updated, items_total,
                 missing_field_count, duration_seconds, result, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_date, keyword, sort, pages_attempted, pages_ok, pages_degraded,
             items_new, items_updated, items_total,
             json.dumps(missing_field_count or {}, ensure_ascii=False),
             duration_seconds, result, error,
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()
        return int(cur.lastrowid)

    # ---- 质量统计（T05 任务 4，供报告与监控使用）----

    def quality_stats(self, date: str) -> DayQuality:
        q = DayQuality(date=date)
        q.snapshots = self._conn.execute(
            "SELECT COUNT(*) FROM item_snapshot WHERE snapshot_date=?",
            (date,)).fetchone()[0]
        q.items_total = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        q.items_new_today = self._conn.execute(
            "SELECT COUNT(*) FROM item WHERE substr(first_seen_at,1,10)=?",
            (date,)).fetchone()[0]
        rows = self._conn.execute(
            "SELECT pages_attempted, pages_ok, pages_degraded, missing_field_count"
            " FROM crawl_run WHERE run_date=?", (date,)).fetchall()
        q.runs = len(rows)
        for row in rows:
            q.pages_attempted += row["pages_attempted"]
            q.pages_ok += row["pages_ok"]
            q.pages_degraded += row["pages_degraded"]
            for k, v in json.loads(row["missing_field_count"] or "{}").items():
                q.missing_fields[k] = q.missing_fields.get(k, 0) + v
        return q

    # ---- 便捷查询（T10 增量计算 / 榜单将复用）----

    def snapshots_of(self, item_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM item_snapshot WHERE item_id=? ORDER BY snapshot_date",
            (item_id,)).fetchall()

    def keywords_of(self, item_id: str) -> list[str]:
        return [row[0] for row in self._conn.execute(
            "SELECT keyword FROM item_keyword WHERE item_id=? ORDER BY keyword",
            (item_id,)).fetchall()]

    def counts(self) -> dict:
        return {
            "items": self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0],
            "snapshots": self._conn.execute(
                "SELECT COUNT(*) FROM item_snapshot").fetchone()[0],
            "item_keywords": self._conn.execute(
                "SELECT COUNT(*) FROM item_keyword").fetchone()[0],
            "runs": self._conn.execute("SELECT COUNT(*) FROM crawl_run").fetchone()[0],
        }

    # ---- T04 产出回填 ----

    def import_parsed_day(self, day: str, parsed_dir: Path | None = None) -> dict:
        """导入 data/parsed/{day}/*.jsonl（多次运行已 append 的文件天然被 upsert 去重）。"""
        root = Path(parsed_dir) if parsed_dir else DATA_DIR / "parsed" / day
        files = sorted(root.glob("*.jsonl")) if root.exists() else []
        totals = UpsertStats()
        per_file = {}
        for f in files:
            records = [ItemRecord(**json.loads(line))
                       for line in f.read_text(encoding="utf-8").splitlines() if line]
            stats = self.upsert_items(records, snapshot_date=day)
            per_file[f.name] = vars(stats)
            totals.items_new += stats.items_new
            totals.items_updated += stats.items_updated
            totals.snapshots_upserted += stats.snapshots_upserted
            totals.keyword_links_upserted += stats.keyword_links_upserted
        return {"files": per_file, "totals": vars(totals)}
