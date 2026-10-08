"""T06 单元测试：归一化 / 权重归并 / 增量 / 评分排名 / 三张榜单 / 确定性。"""
from __future__ import annotations

import json

import pytest

from goofishradar.config import RankingCfg
from goofishradar.parser import ItemRecord
from goofishradar.ranking import (
    ScoreInput,
    build_rankings,
    compute_deltas,
    effective_weights,
    normalize,
    rankings_to_dict,
)

CFG = RankingCfg()  # 0.40/0.20/0.25/0.15, TopN 50/20/20


def rec(item_id: str, want: int | None, view: int | None = None,
        keyword: str = "iPhone", status: str = "在售", price: float = 100.0):
    return ItemRecord(
        item_id=item_id, title=f"商品{item_id}", price=price, want_count=want,
        view_count=view, publish_time=None, status=status, seller_nick="s",
        seller_id="1", location="上海", pic_url="p",
        item_url=f"https://www.goofish.com/item?id={item_id}", tags=[],
        source_keyword=keyword, source_sort="", page_number=1,
        crawled_at="2026-10-08 08:00:00")


# ---- 归一化（T06 任务 1）----

class TestNormalize:
    def test_min_max(self):
        assert normalize([10, 20, 30]) == [0.0, 0.5, 1.0]

    def test_constant_distribution_returns_half(self):
        assert normalize([7, 7, 7]) == [0.5, 0.5, 0.5]

    def test_single_item_returns_half(self):
        assert normalize([42]) == [0.5]

    def test_empty_returns_all_none(self):
        assert normalize([]) == []

    def test_none_preserved(self):
        assert normalize([None, 5]) == [None, 0.5]   # 单值分布=常量分布 → 0.5


# ---- 权重归并（T06 任务 2：首日/缺失浏览量）----

class TestEffectiveWeights:
    def test_full_data_uses_configured_weights(self):
        w, notes = effective_weights(CFG, has_view=True, has_history=True)
        assert w == {"want_count": 0.40, "view_count": 0.20,
                     "want_delta_7d": 0.25, "view_delta_7d": 0.15}
        assert len(notes) == 1  # 仅基础口径

    def test_first_day_merges_delta_into_absolute(self):
        w, notes = effective_weights(CFG, has_view=True, has_history=False)
        assert w == {"want_count": 0.65, "view_count": 0.35,
                     "want_delta_7d": 0.0, "view_delta_7d": 0.0}
        assert any("首日" in n for n in notes)

    def test_missing_view_merges_into_want(self):
        w, notes = effective_weights(CFG, has_view=False, has_history=True)
        assert w == {"want_count": 0.60, "view_count": 0.0,
                     "want_delta_7d": 0.40, "view_delta_7d": 0.0}
        assert any("浏览量" in n for n in notes)

    def test_first_day_and_no_view_all_weight_on_want(self):
        """当前真实场景：首日 + 列表页无浏览量 → 全部权重落在想要数。"""
        w, notes = effective_weights(CFG, has_view=False, has_history=False)
        assert w == {"want_count": 1.0, "view_count": 0.0,
                     "want_delta_7d": 0.0, "view_delta_7d": 0.0}
        assert any("首日" in n for n in notes) and any("浏览量" in n for n in notes)
        assert sum(w.values()) == pytest.approx(1.0)


# ---- 增量 ----

class TestDeltas:
    def test_exact_one_day(self):
        series = [("2026-10-07", 10, 100)]
        assert compute_deltas(15, series, "2026-10-08", 1) == 5
        assert compute_deltas(15, series, "2026-10-08", 7) == 5   # 不足7天取最早

    def test_seven_day_uses_nearest_at_or_before_target(self):
        series = [("2026-10-01", 5, 50), ("2026-10-05", 8, 80)]
        assert compute_deltas(18, series, "2026-10-08", 7) == 13  # 基线 10-01
        assert compute_deltas(18, series, "2026-10-08", 1) == 10  # 基线 10-05

    def test_no_history_returns_none(self):
        assert compute_deltas(10, [], "2026-10-08", 1) is None

    def test_none_current_returns_none(self):
        assert compute_deltas(None, [("2026-10-07", 10, 100)],
                              "2026-10-08", 1) is None


# ---- 评分与榜单 ----

class TestScoreAndRankings:
    def groups(self, day2_history=False):
        history = [("2026-10-07", 10, 100)] if day2_history else []
        g1 = [ScoreInput(rec("a", want=30, keyword="iPhone"), history),
              ScoreInput(rec("b", want=10, keyword="iPhone"), history),
              ScoreInput(rec("c", want=20, keyword="iPhone", status="已售出"),
                         history)]
        g2 = [ScoreInput(rec("d", want=50, keyword="乐高"), history)]
        return {"iPhone": g1, "乐高": g2}

    def test_first_day_score_is_normalized_want_only(self):
        out = build_rankings(self.groups(), "2026-10-08", CFG)
        # 首日 + 无浏览量 → 全权重在想要数：归一化 [30,10,20] → [1.0, 0.0, 0.5]
        iphone = {s.item_id: s.score for s in out["per_keyword"]["iPhone"]}
        assert iphone == {"a": 1.0, "c": 0.5, "b": 0.0}
        # 总榜：a(1.0) 第一；c(0.5,want20) 与 d(0.5,want50) 同分 → 想要数降序 d 在前
        assert [s.item_id for s in out["overall"]] == ["a", "d", "c", "b"]

    def test_sold_items_retained_with_status(self):
        out = build_rankings(self.groups(), "2026-10-08", CFG)
        sold = [s for s in out["overall"] if s.item_id == "c"]
        assert len(sold) == 1 and sold[0].status == "已售出"

    def test_cross_task_normalization_within_task(self):
        """归一化按任务内分布：乐高 d 单条 → 0.5，不因 want=50 高于 iPhone 全组。"""
        out = build_rankings(self.groups(), "2026-10-08", CFG)
        top = out["overall"][0]
        assert top.item_id == "a" and top.score == 1.0

    def test_day2_weights_and_rising_list(self):
        out = build_rankings(self.groups(day2_history=True), "2026-10-08", CFG)
        assert out["weights_applied"]["iPhone"]["want_delta_7d"] == pytest.approx(0.40)
        # 日增量：d: 50-10=+40; a: 30-10=+20; c: 20-10=+10; b: 10-10=0(被排除)
        rising = out["rising"]
        assert [s.item_id for s in rising] == ["d", "a", "c"]
        assert rising[0].delta_want_1d == 40
        assert out["rising_note"] == ""

    def test_rising_empty_with_note_when_no_history(self):
        out = build_rankings(self.groups(), "2026-10-08", CFG)
        assert out["rising"] == []
        assert "≥2 天" in out["rising_note"]

    def test_determinism_same_data_same_output(self):
        """T06 验收标准 3：同一数据重复计算结果一致。"""
        d1 = rankings_to_dict(build_rankings(self.groups(day2_history=True),
                                             "2026-10-08", CFG), CFG)
        d2 = rankings_to_dict(build_rankings(self.groups(day2_history=True),
                                             "2026-10-08", CFG), CFG)
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

    def test_tie_break_stable_by_want_then_id(self):
        g = {"k": [ScoreInput(rec("x2", want=10)),
                   ScoreInput(rec("x1", want=10)),
                   ScoreInput(rec("x3", want=10))]}
        out = build_rankings(g, "2026-10-08", CFG)   # 全同值 → 全 0.5，按 item_id 升序
        assert [s.item_id for s in out["overall"]] == ["x1", "x2", "x3"]

    def test_missing_want_treated_as_zero(self):
        g = {"k": [ScoreInput(rec("has", want=10)),
                   ScoreInput(rec("none", want=None))]}
        out = build_rankings(g, "2026-10-08", CFG)
        assert out["overall"][0].item_id == "has"
        assert out["overall"][1].score == 0.0

    def test_rank_assigned_per_list_independently(self):
        out = build_rankings(self.groups(day2_history=True), "2026-10-08", CFG)
        d = rankings_to_dict(out, CFG)
        kw_items = {x["item_id"]: x["rank"] for x in d["per_keyword"]["iPhone"]}
        ov_items = {x["item_id"]: x["rank"] for x in d["overall"]}
        assert kw_items["a"] == 1                       # 分类榜内第 1
        assert ov_items["a"] == ov_items["a"]           # 总榜名次独立存在
        assert d["rising"][0]["rank"] == 1              # 飙升榜名次独立枚举

    def test_topn_truncation_in_serialization(self):
        g = {"k": [ScoreInput(rec(f"i{n:02d}", want=n)) for n in range(30)]}
        out = build_rankings(g, "2026-10-08", CFG)
        d = rankings_to_dict(out, RankingCfg(topn_overall=50, topn_per_keyword=20,
                                             topn_rising=20))
        assert len(d["per_keyword"]["k"]) == 20
        assert len(d["overall"]) == 30
