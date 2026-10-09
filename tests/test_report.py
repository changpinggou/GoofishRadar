"""T07 单元测试：报告产出 / 口径置顶 / Excel 4 Sheet / 过期清理 / 昨日对比。"""
from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from goofishradar.config import PathsCfg, Settings
from goofishradar.parser import ItemRecord
from goofishradar.report import cleanup_old_reports, generate_report
from goofishradar.storage import Store

CALIBER = "畅销度为热度代理指标"


def make_record(item_id: str, want: int, keyword: str = "iPhone") -> ItemRecord:
    return ItemRecord(
        item_id=item_id, title=f"测试商品{item_id}", price=100.0 + want,
        want_count=want, view_count=None, publish_time=None, status="在售",
        seller_nick="卖家甲", seller_id="1", location="上海", pic_url="p",
        item_url=f"https://www.goofish.com/item?id={item_id}", tags=[],
        source_keyword=keyword, source_sort="most_want", page_number=1,
        crawled_at="2026-10-08 08:00:00")


@pytest.fixture
def env(tmp_path):
    settings = Settings(paths=PathsCfg(
        data_dir=tmp_path / "data", reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs"))
    settings.paths.ensure()
    store = Store(settings.paths.data_dir / "db.sqlite")
    records = ([make_record(f"a{i:02d}", want=100 - i) for i in range(12)]
               + [make_record(f"b{i:02d}", want=50 - i, keyword="乐高")
                  for i in range(8)])
    store.upsert_items(records, "2026-10-08")
    store.record_run("2026-10-08", "iPhone", pages_attempted=2, pages_ok=2,
                     missing_field_count={"seller_id": 3}, duration_seconds=30.0)
    yield settings, store
    store.close()


class TestGenerateReport:
    def test_creates_html_and_excel_with_expected_content(self, env):
        settings, store = env
        out = generate_report("2026-10-08", store, settings)
        assert out["html"].exists() and out["excel"].exists()
        html = out["html"].read_text(encoding="utf-8")

        # 验收标准 3：口径说明固定出现在报告顶部（位于第一张榜单之前）
        assert CALIBER in html
        assert html.index("口径说明") < html.index("总榜 Top")
        assert "测试商品a00" in html and "测试商品b00" in html
        assert out["html"].name == "report.html"
        assert out["html"].parent.name == "2026-10-08"

    def test_excel_four_sheets_complete_and_loadable(self, env):
        """T07 验收标准 2：4 个 Sheet 数据完整、可正常打开。"""
        settings, store = env
        out = generate_report("2026-10-08", store, settings)
        wb = load_workbook(out["excel"])
        assert wb.sheetnames == ["总榜", "分类榜", "飙升榜", "当日全量"]

        overall = wb["总榜"]
        assert overall.max_row == 1 + 20          # 表头 + 跨关键词全部 20 条（< Top50 不截断）
        assert overall.cell(2, 2).value.startswith("a")

        per_kw = wb["分类榜"]
        assert per_kw.max_row == 1 + 12 + 8       # 两个关键词全量
        assert per_kw.cell(2, 1).value == "iPhone"

        rising = wb["飙升榜"]
        assert "≥2 天" in str(rising.cell(2, 1).value)   # 首日空榜 + 说明

        full = wb["当日全量"]
        assert full.max_row == 1 + 20             # 当日快照 20 条

    def test_rankings_archived_for_next_day_comparison(self, env):
        settings, store = env
        generate_report("2026-10-08", store, settings)
        archive = settings.paths.data_dir / "parsed" / "2026-10-08" / "_rankings.json"
        data = json.loads(archive.read_text(encoding="utf-8"))
        assert len(data["overall"]) == 20         # 跨关键词全部入档
        assert data["caliber"]


class TestComparison:
    def test_no_yesterday_archive_shows_note(self, env):
        settings, store = env
        out = generate_report("2026-10-08", store, settings)
        html = out["html"].read_text(encoding="utf-8")
        assert "暂无可比数据" in html

    def test_with_yesterday_archive_shows_new_and_dropped(self, env):
        settings, store = env
        # 伪造昨日榜单存档：只含 a00（今日跌出）；今日其余全部为"新进"
        ydir = settings.paths.data_dir / "parsed" / "2026-10-07"
        ydir.mkdir(parents=True)
        (ydir / "_rankings.json").write_text(json.dumps({
            "overall": [{"item_id": "a00"}]}, ensure_ascii=False), encoding="utf-8")
        out = generate_report("2026-10-08", store, settings)
        html = out["html"].read_text(encoding="utf-8")
        assert "较 2026-10-07" in html
        assert "新进" in html and "跌出" in html
        assert "测试商品a00" in html               # 跌出商品展示标题


class TestCleanup:
    def test_old_report_dirs_removed_within_retention(self, tmp_path):
        reports = tmp_path / "reports"
        for name in ("2020-01-01", "2020-06-30", "2099-12-31"):
            (reports / name).mkdir(parents=True)
        (reports / "not-a-date").mkdir()
        removed = cleanup_old_reports(reports, retain_days=90)
        assert sorted(removed) == ["2020-01-01", "2020-06-30"]
        assert (reports / "2099-12-31").exists()
        assert (reports / "not-a-date").exists()      # 非日期目录不动

    def test_missing_dir_is_noop(self, tmp_path):
        assert cleanup_old_reports(tmp_path / "nope", 90) == []
