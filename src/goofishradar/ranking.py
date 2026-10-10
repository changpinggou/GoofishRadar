"""畅销度评分与榜单生成（T06 / 需求文档第 8 节）。

口径（重要）：闲鱼不公开真实销量，本模块得分为「热度代理指标」，报告须固定展示口径说明。

    Score = w_want×norm(想要数) + w_view×norm(浏览量)
          + w_wantΔ×norm(想要数Δ7d) + w_viewΔ×norm(浏览量Δ7d)

- 归一化：min-max，按「当日同一关键词任务内」分布（避免跨品类量纲差异）；
  分布相同（含单条）→ 全部 0.5；
- 权重归并（顺序固定，见 effective_weights）：
  ① 无历史快照（首日）→ 增量权重并入对应绝对值指标，口径标注「首日」；
  ② 该类指标整列缺失（当前列表页无浏览量）→ 并入同类指标（浏览→想要）；
- 榜单：总榜（跨词 TopN）/ 分类榜（每词 TopN）/ 飙升榜（Δ1d TopN，不足两天为空榜+说明）；
- 确定性：同分按 (想要数 desc, item_id asc) 稳定排序，同一数据重复计算结果一致。

纯函数核心 + score_day_from_store 薄适配器（从 Store 取数）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import RankingCfg
from .parser import ItemRecord
from .storage import Store

# 权重键（有效权重字典的固定顺序，保证序列化稳定）
W_KEYS = ("want_count", "view_count", "want_delta_7d", "view_delta_7d")
CALIBER_BASE = "畅销度为热度代理指标（想要数/浏览量及其增量加权），非实际成交量"


# ---- 纯函数：归一化与权重 ----

def normalize(values: list[float | int | None]) -> list[float | None]:
    """min-max 归一化到 [0,1]。全同值（含单条）→ 0.5；None 保持 None。"""
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return [None] * len(values)
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return [0.5 if v is not None else None for v in values]
    return [None if v is None else (float(v) - lo) / (hi - lo) for v in values]


def effective_weights(weights: RankingCfg, *, has_view: bool, has_history: bool
                      ) -> tuple[dict[str, float], list[str]]:
    """计算有效权重（归并顺序：①首日并入绝对值 → ②浏览缺失并入想要），返回 (权重, 口径标注)。"""
    w = {
        "want_count": weights.weight_want_count,
        "view_count": weights.weight_view_count,
        "want_delta_7d": weights.weight_want_delta_7d,
        "view_delta_7d": weights.weight_view_delta_7d,
    }
    notes: list[str] = [CALIBER_BASE]
    if not has_history:
        w["want_count"] += w["want_delta_7d"]
        w["view_count"] += w["view_delta_7d"]
        w["want_delta_7d"] = w["view_delta_7d"] = 0.0
        notes.append("首日无历史快照：增量权重并入对应绝对值指标")
    if not has_view:
        w["want_count"] += w["view_count"]
        w["want_delta_7d"] += w["view_delta_7d"]
        w["view_count"] = w["view_delta_7d"] = 0.0
        notes.append("浏览量列表页不可得（FR-4 详情采集补齐前）：浏览类权重并入想要类")
    w = {k: round(v, 6) for k, v in w.items()}   # 消除浮点漂移，保证序列化稳定
    return w, notes


# ---- 纯函数：增量 ----

def compute_deltas(current: int | None,
                   series: list[tuple[str, int | None, int | None]],
                   today: str, lookback_days: int) -> int | None:
    """当前值 - 基线（基线=≤ today-lookback 的最近一条，否则最早可得一条）。

    series 升序、严格早于 today；current 为 None 或无历史 → None。
    """
    if current is None or not series:
        return None
    target = (date.fromisoformat(today) - timedelta(days=lookback_days)).isoformat()
    base = next((s for s in reversed(series) if s[0] <= target), series[0])
    base_val = base[1]
    if base_val is None:
        return None
    return int(current) - int(base_val)


# ---- 纯函数：评分与排名 ----

@dataclass
class ScoreInput:
    record: ItemRecord
    # (date, want_count, view_count) 升序、严格早于 today（来自 item_snapshot）
    prev_snapshots: list[tuple[str, int | None, int | None]] = field(default_factory=list)


@dataclass
class ScoredItem:
    item_id: str
    title: str | None
    price: float | None
    want_count: int | None
    view_count: int | None
    delta_want_1d: int | None
    delta_want_7d: int | None
    delta_view_1d: int | None
    delta_view_7d: int | None
    status: str
    seller_nick: str | None
    seller_id: str | None
    location: str | None
    pic_url: str | None
    item_url: str
    source_keyword: str
    score: float

    def to_dict(self, rank: int = 0) -> dict:
        d = dict(self.__dict__)
        d["rank"] = rank
        return d


def _score_one_group(inputs: list[ScoreInput], today: str, weights: RankingCfg
                     ) -> tuple[list[ScoredItem], dict[str, float], list[str]]:
    """同一关键词任务内的归一化、加权、排序（不含 TopN 截断）。"""
    has_history = any(i.prev_snapshots for i in inputs)
    has_view = any(i.record.view_count is not None for i in inputs)
    eff, notes = effective_weights(weights, has_view=has_view, has_history=has_history)

    want_now = [i.record.want_count if i.record.want_count is not None else 0
                for i in inputs]
    view_now = [i.record.view_count if i.record.view_count is not None else 0
                for i in inputs]
    d_want7 = [compute_deltas(i.record.want_count, i.prev_snapshots, today, 7)
               for i in inputs]
    d_view7 = [compute_deltas(i.record.view_count, i.prev_snapshots, today, 7)
               for i in inputs]
    # 缺失增量（首日/无基线）按 0 参与加权（权重已归并时本列为 0 权重，不影响结果）
    n_want = normalize(want_now)
    n_view = normalize(view_now)
    n_dw7 = normalize([d if d is not None else 0 for d in d_want7])
    n_dv7 = normalize([d if d is not None else 0 for d in d_view7])

    scored: list[ScoredItem] = []
    for idx, inp in enumerate(inputs):
        r = inp.record
        d1w = compute_deltas(r.want_count, inp.prev_snapshots, today, 1)
        d1v = compute_deltas(r.view_count, inp.prev_snapshots, today, 1)
        score = (eff["want_count"] * (n_want[idx] or 0.0)
                 + eff["view_count"] * (n_view[idx] or 0.0)
                 + eff["want_delta_7d"] * (n_dw7[idx] or 0.0)
                 + eff["view_delta_7d"] * (n_dv7[idx] or 0.0))
        scored.append(ScoredItem(
            item_id=r.item_id, title=r.title, price=r.price,
            want_count=r.want_count, view_count=r.view_count,
            delta_want_1d=d1w, delta_want_7d=d_want7[idx],
            delta_view_1d=d1v, delta_view_7d=d_view7[idx],
            status=r.status, seller_nick=r.seller_nick, seller_id=r.seller_id,
            location=r.location, pic_url=r.pic_url, item_url=r.item_url,
            source_keyword=r.source_keyword, score=round(score, 6)))

    scored.sort(key=lambda s: (-s.score, -(s.want_count or 0), s.item_id))
    return scored, eff, notes


def build_rankings(groups: dict[str, list[ScoreInput]], today: str, weights: RankingCfg,
                   ) -> dict:
    """三张榜单（T06 任务 3/4）：总榜 / 分类榜 / 飙升榜。已售商品保留并带状态标记。"""
    per_keyword: dict[str, list[ScoredItem]] = {}
    all_scored: list[ScoredItem] = []
    weights_applied: dict[str, float] = {}
    notes: list[str] = []
    group_hist_states: dict[str, bool] = {}

    for keyword, inputs in sorted(groups.items()):
        scored, eff, group_notes = _score_one_group(inputs, today, weights)
        per_keyword[keyword] = scored
        all_scored.extend(scored)
        weights_applied[keyword] = eff
        group_hist_states[keyword] = any(i.prev_snapshots for i in inputs)
        for n in group_notes:
            if n not in notes:
                notes.append(n)

    # 各任务历史状态不一致时补充澄清（口径备注为并集，避免"首日"标注以偏概全）
    if len(set(group_hist_states.values())) > 1:
        notes.append("各关键词任务历史状态不同：权重按任务内商品自身历史独立归并"
                     "（逐任务有效权重见 weights_applied）")

    overall = sorted(all_scored, key=lambda s: (-s.score, -(s.want_count or 0),
                                                s.item_id))

    # 飙升榜：按想要数日增量；需 ≥2 天历史，不足为空榜并注明
    has_history = any(i.prev_snapshots for g in groups.values() for i in g)
    rising_pool = [s for s in all_scored if s.delta_want_1d is not None
                   and s.delta_want_1d != 0]
    rising_pool.sort(key=lambda s: (-s.delta_want_1d, -(s.want_count or 0), s.item_id))
    rising_note = ""
    if not rising_pool:
        rising_note = ("数据不足：飙升榜需要 ≥2 天历史快照计算日增量"
                       if not has_history else "今日无任何商品想要数日增量变化")
    return {
        "date": today,
        "caliber": notes,
        "weights_applied": weights_applied,
        "overall": overall,
        "per_keyword": per_keyword,
        "rising": rising_pool,
        "rising_note": rising_note,
    }


def rankings_to_dict(rankings: dict, weights_cfg: RankingCfg | None = None) -> dict:
    """序列化（可选 TopN 截断）；rank 按各榜单分别枚举；不含时间戳，同数据输出一致。"""
    overall = rankings["overall"]
    per_kw = rankings["per_keyword"]
    rising = rankings["rising"]
    if weights_cfg is not None:
        overall = overall[:weights_cfg.topn_overall]
        per_kw = {k: v[:weights_cfg.topn_per_keyword]
                  for k, v in per_kw.items()}
        rising = rising[:weights_cfg.topn_rising]

    def with_rank(items: list[ScoredItem]) -> list[dict]:
        return [s.to_dict(rank=i) for i, s in enumerate(items, 1)]

    return {
        "date": rankings["date"],
        "caliber": rankings["caliber"],
        "weights_applied": rankings["weights_applied"],
        "rising_note": rankings["rising_note"],
        "overall": with_rank(overall),
        "per_keyword": {k: with_rank(v) for k, v in sorted(per_kw.items())},
        "rising": with_rank(rising),
    }


def score_day_from_store(store: Store, today: str, weights: RankingCfg) -> dict:
    """从库中取当日快照+商品主档+历史序列，产出三张榜单（T07/T08 直接消费）。"""
    rows = store._conn.execute(
        """SELECT s.item_id, s.want_count AS snap_want, s.price AS snap_price,
                  s.status AS snap_status, s.source_keyword, s.page_number,
                  s.crawled_at, i.title, i.view_count, i.publish_time,
                  i.seller_nick, i.seller_id, i.location, i.pic_url, i.item_url,
                  i.tags
           FROM item_snapshot s JOIN item i ON i.item_id = s.item_id
           WHERE s.snapshot_date = ?""", (today,)).fetchall()

    history = store._conn.execute(
        """SELECT item_id, snapshot_date, want_count, view_count
           FROM item_snapshot WHERE snapshot_date < ? ORDER BY snapshot_date""",
        (today,)).fetchall()
    hist_by_item: dict[str, list[tuple[str, int | None, int | None]]] = {}
    for h in history:
        hist_by_item.setdefault(h["item_id"], []).append(
            (h["snapshot_date"], h["want_count"], h["view_count"]))

    groups: dict[str, list[ScoreInput]] = {}
    for row in rows:
        rec = ItemRecord(
            item_id=row["item_id"], title=row["title"], price=row["snap_price"],
            want_count=row["snap_want"], view_count=row["view_count"],
            publish_time=row["publish_time"], status=row["snap_status"],
            seller_nick=row["seller_nick"], seller_id=row["seller_id"],
            location=row["location"], pic_url=row["pic_url"],
            item_url=row["item_url"], tags=json.loads(row["tags"] or "[]"),
            source_keyword=row["source_keyword"], source_sort="",
            page_number=row["page_number"], crawled_at=row["crawled_at"])
        groups.setdefault(row["source_keyword"] or "未分组", []).append(
            ScoreInput(record=rec,
                       prev_snapshots=hist_by_item.get(row["item_id"], [])))
    return build_rankings(groups, today, weights)
