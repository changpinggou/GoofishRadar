"""搜索结果解析器单元测试（T04）：固定夹具 + T01 真实样本回归。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from goofishradar.collector import RateLimiter, sanitize_keyword
from goofishradar.config import KeywordTask
from goofishradar.parser import apply_filters, parse_search_payload
from goofishradar.session import detect_risk_block

FIXTURE = Path(__file__).parent / "fixtures" / "search_payload_sample.json"
T01_SAMPLE = Path(__file__).parent.parent / "data" / "samples" / "raw" / "search_api_04.json"


def parse_fixture(**kwargs):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_search_payload(payload, keyword="iPhone", sort="most_want",
                                page_number=1, crawled_at="2026-10-08 12:00:00",
                                **kwargs)


# ---- 基本解析 ----

class TestParseFixture:
    def test_dedup_and_counts(self):
        out = parse_fixture()
        assert out.raw_item_count == 5
        assert out.duplicate_count == 1
        assert [i.item_id for i in out.items] == ["1001", "1002", "1003", "1004"]

    def test_full_item_fields(self):
        it = parse_fixture().items[0]
        assert it.title == "iPhone 12 Pro 128G 金色 国行"
        assert it.price == 833.0
        assert it.want_count == 5
        assert it.status == "在售"
        assert it.seller_nick == "蒲公英苹果店"
        assert it.seller_id == "2221425617053"
        assert it.location == "天津"
        assert it.pic_url.startswith("http://img.alicdn.com")
        assert it.item_url == "https://www.goofish.com/item?id=1001"
        assert "几乎全新" in it.tags and "卖家信用极好" in it.tags

    def test_want_tag_semantics(self):
        items = parse_fixture().items
        assert items[1].want_count == 0      # 有标签但无「N人想要」→ 0
        assert items[3].want_count is None   # 完全无标签 → None（缺失计数）

    def test_sold_status_via_tag(self):
        assert parse_fixture().items[2].status == "已售出"

    def test_missing_fields_tolerated_and_counted(self):
        out = parse_fixture()
        it4 = out.items[3]
        assert it4.pic_url is None and it4.location is None and it4.price is None
        assert it4.seller_nick is None and it4.seller_id is None
        assert out.missing_field_count["pic_url"] == 1
        assert out.missing_field_count["price"] == 1
        assert out.missing_field_count["seller_nick"] == 2  # 1003 与 1004 均无昵称

    def test_idempotent_parse(self):
        """T04 验收标准 2：同一原始样本解析结果稳定一致。"""
        first = [i.to_dict() for i in parse_fixture().items]
        second = [i.to_dict() for i in parse_fixture().items]
        assert first == second
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---- 筛选 ----

class TestApplyFilters:
    def items(self):
        return parse_fixture().items

    def test_price_range(self):
        task = KeywordTask(keyword="x", price_min=900, price_max=1500)
        assert [i.item_id for i in apply_filters(self.items(), task)] == ["1002"]

    def test_stuff_matches_tags(self):
        task = KeywordTask(keyword="x", stuff="几乎全新")
        assert [i.item_id for i in apply_filters(self.items(), task)] == ["1001"]

    def test_location_substring(self):
        task = KeywordTask(keyword="x", location="京")
        assert [i.item_id for i in apply_filters(self.items(), task)] == ["1003"]

    def test_no_filters_returns_all(self):
        assert len(apply_filters(self.items(), KeywordTask(keyword="x"))) == 4


# ---- 采集器纯逻辑 ----

class TestCollectorPure:
    def test_rate_limiter_within_configured_range(self):
        rate = RateLimiter(8, 20)
        delays = [rate.next_delay() for _ in range(300)]
        assert all(8 <= d <= 20 for d in delays)
        assert min(delays) < 10 and max(delays) > 18  # 覆盖区间两端

    def test_sanitize_keyword(self):
        assert sanitize_keyword('a/b:c*d?"<>| e') == "a_b_c_d_e"
        assert sanitize_keyword("  ") == "unnamed"
        assert len(sanitize_keyword("词" * 100)) == 40

    def test_detect_risk_block(self):
        assert detect_risk_block("非法访问 为了保障您的体验")
        assert detect_risk_block("请完成 安全验证")
        assert not detect_risk_block("闲鱼 - 闲不住？上闲鱼！")


# ---- T01 真实样本回归（样本本地留存，缺失则跳过）----

class TestT01RealSample:
    def test_regression_against_real_captured_payload(self):
        if not T01_SAMPLE.exists():
            pytest.skip("T01 真实样本不在本机（gitignored）")
        payload = json.loads(T01_SAMPLE.read_text(encoding="utf-8"))
        out = parse_search_payload(payload, keyword="iPhone", page_number=1,
                                   crawled_at="t")
        # 已知行为：外层条目 + 内嵌 detailParams 片段各命中一次 → 原始 60，去重后 30
        assert out.raw_item_count == 2 * 30
        assert len(out.items) == 30
        assert out.duplicate_count == out.raw_item_count - len(out.items)
        assert all(i.item_id and i.title for i in out.items)
        assert all(i.pic_url for i in out.items)  # 去重保留的必是富外层条目
        priced = [i for i in out.items if i.price is not None]
        assert len(priced) / len(out.items) >= 0.98   # 真实数据价格覆盖 ≥98%
