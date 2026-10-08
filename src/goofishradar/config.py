"""配置加载与校验（T03 / FR-1）：settings.yaml + keywords.yaml，改配置不改代码。

设计要点：
- 硬顶在代码层强制（配置超限自动截断并告警）：单关键词页数 ≤ 50，单日总页数 ≤ 300；
- 非法配置抛 ConfigError，报错信息含具体字段路径（如 limits.max_pages_per_keyword）；
- 未知配置键告警（防拼写错误静默失效）；
- 所有告警以列表形式随加载结果返回，便于测试与报告。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_DIR, DATA_DIR, LOG_DIR, REPORTS_DIR, ROOT

HARD_MAX_PAGES_PER_KEYWORD = 50   # 代码硬顶：单关键词页数（FR-10）
HARD_MAX_PAGES_PER_DAY = 300      # 代码硬顶：单日总页数（FR-10）
ALLOWED_SORTS = ("most_want", "comprehensive", "recent_active", "newest",
                 "price_asc", "price_desc")

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


class ConfigError(ValueError):
    """配置非法：报错信息包含具体字段路径。"""


# ---- 基础类型 helpers（bool 是 int 子类，整数校验必须排除 bool）----

def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _as_int(v: Any, where: str) -> int:
    _expect(isinstance(v, int) and not isinstance(v, bool),
            f"{where}: 期望整数，得到 {v!r}")
    return int(v)


def _as_num(v: Any, where: str) -> float:
    _expect(isinstance(v, (int, float)) and not isinstance(v, bool),
            f"{where}: 期望数字，得到 {v!r}")
    return float(v)


def _as_str(v: Any, where: str) -> str:
    _expect(isinstance(v, str) and v.strip(), f"{where}: 期望非空字符串，得到 {v!r}")
    return v.strip()


def _as_bool(v: Any, where: str) -> bool:
    _expect(isinstance(v, bool), f"{where}: 期望 true/false，得到 {v!r}")
    return v


def _as_hhmm(v: Any, where: str) -> str:
    s = _as_str(v, where)
    m = _HHMM.match(s)
    _expect(m is not None, f"{where}: 时刻格式应为 HH:MM，得到 {v!r}")
    h, mi = int(m.group(1)), int(m.group(2))  # type: ignore[union-attr]
    _expect(0 <= h < 24 and 0 <= mi < 60, f"{where}: 时刻超出范围，得到 {v!r}")
    return f"{h:02d}:{mi:02d}"


def _parse_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML 语法错误: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: 无法读取: {exc}") from exc


def _section(data: dict, name: str, warnings: list[str]) -> dict:
    sec = data.get(name, {})
    _expect(isinstance(sec, dict), f"{name}: 应为键值映射，得到 {sec!r}")
    for k in sec:
        if k not in SETTINGS_KEYS[name]:
            warnings.append(f"{name}.{k}: 未知配置项（可能拼写错误），已忽略")
    return sec


def _warn_unknown_keys(data: dict, known: set[str], prefix: str,
                       warnings: list[str]) -> None:
    for k in data:
        if k not in known:
            warnings.append(f"{prefix}.{k}: 未知配置项（可能拼写错误），已忽略")


# ---- 数据模型 ----

@dataclass(slots=True)
class ScheduleCfg:
    time: str = "08:00"                 # 每日默认执行时刻
    weekday_time: str | None = None     # 工作日时刻（缺省用 time）
    weekend_time: str | None = None     # 周末时刻（缺省用 time）
    randomize_minutes: int = 15         # 启动随机化窗口（±分钟）

    def resolve_time(self, weekend: bool) -> str:
        return (self.weekend_time if weekend else self.weekday_time) or self.time


@dataclass(slots=True)
class LimitsCfg:
    page_delay_seconds: tuple[float, float] = (8.0, 20.0)  # 页间随机延时（T01 实测修订，原 5–15）
    max_pages_per_keyword: int = 3
    max_pages_per_day: int = 300


@dataclass(slots=True)
class RankingCfg:
    topn_overall: int = 50
    topn_per_keyword: int = 20
    topn_rising: int = 20
    weight_want_count: float = 0.40
    weight_view_count: float = 0.20
    weight_want_delta_7d: float = 0.25
    weight_view_delta_7d: float = 0.15


@dataclass(slots=True)
class ReportCfg:
    retain_days: int = 90


@dataclass(slots=True)
class PathsCfg:
    data_dir: Path = DATA_DIR
    reports_dir: Path = REPORTS_DIR
    logs_dir: Path = LOG_DIR

    @property
    def user_data_dir(self) -> Path:
        return self.data_dir / "user_data"

    @property
    def session_expired_marker(self) -> Path:
        return self.data_dir / "SESSION_EXPIRED"

    def ensure(self) -> None:
        for d in (self.data_dir, self.user_data_dir, self.reports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class Settings:
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    limits: LimitsCfg = field(default_factory=LimitsCfg)
    ranking: RankingCfg = field(default_factory=RankingCfg)
    report: ReportCfg = field(default_factory=ReportCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)


@dataclass(slots=True)
class KeywordTask:
    keyword: str
    enabled: bool = True
    max_pages: int = 3
    sort: str = "most_want"
    price_min: float | None = None
    price_max: float | None = None
    stuff: str | None = None        # 成色筛选（可选）
    location: str | None = None     # 所在地筛选（可选）

    def enabled_or_disabled_label(self) -> str:
        return "启用" if self.enabled else "停用"


# ---- settings.yaml 加载 ----

SETTINGS_KEYS = {
    "schedule": {"time", "weekday_time", "weekend_time", "randomize_minutes"},
    "limits": {"page_delay_seconds", "max_pages_per_keyword", "max_pages_per_day"},
    "ranking": {"topn_overall", "topn_per_keyword", "topn_rising", "weights"},
    "report": {"retain_days"},
    "paths": {"data_dir", "reports_dir", "logs_dir"},
}
WEIGHT_KEYS = {"want_count", "view_count", "want_delta_7d", "view_delta_7d"}
_WEIGHT_FIELD = {
    "want_count": "weight_want_count", "view_count": "weight_view_count",
    "want_delta_7d": "weight_want_delta_7d", "view_delta_7d": "weight_view_delta_7d",
}


def _load_schedule(sec: dict, warnings: list[str]) -> ScheduleCfg:
    cfg = ScheduleCfg(time=_as_hhmm(sec.get("time", "08:00"), "schedule.time"))
    if "weekday_time" in sec:
        cfg.weekday_time = _as_hhmm(sec["weekday_time"], "schedule.weekday_time")
    if "weekend_time" in sec:
        cfg.weekend_time = _as_hhmm(sec["weekend_time"], "schedule.weekend_time")
    rm = _as_int(sec.get("randomize_minutes", 15), "schedule.randomize_minutes")
    _expect(0 <= rm <= 120, f"schedule.randomize_minutes: 应在 0–120 之间，得到 {rm}")
    if rm > 30:
        warnings.append(f"schedule.randomize_minutes={rm} 偏大（默认 15），请确认")
    cfg.randomize_minutes = rm
    return cfg


def _load_limits(sec: dict, warnings: list[str]) -> LimitsCfg:
    delay = sec.get("page_delay_seconds", [8, 20])
    _expect(isinstance(delay, (list, tuple)) and len(delay) == 2,
            f"limits.page_delay_seconds: 应为 [最小值, 最大值]，得到 {delay!r}")
    lo = _as_num(delay[0], "limits.page_delay_seconds[0]")
    hi = _as_num(delay[1], "limits.page_delay_seconds[1]")
    _expect(0 < lo <= hi, f"limits.page_delay_seconds: 应满足 0 < 最小值 ≤ 最大值，得到 [{lo}, {hi}]")
    if lo < 5:
        warnings.append(f"limits.page_delay_seconds 最小值 {lo}s < 5s，过快易触发风控（T01 实测建议 ≥ 8s）")
    if hi < 8:
        warnings.append(f"limits.page_delay_seconds 最大值 {hi}s 低于 T01 实测建议（8–20s），请确认")

    mpk = _as_int(sec.get("max_pages_per_keyword", 3), "limits.max_pages_per_keyword")
    _expect(mpk >= 1, f"limits.max_pages_per_keyword: 至少为 1，得到 {mpk}")
    if mpk > HARD_MAX_PAGES_PER_KEYWORD:
        warnings.append(f"limits.max_pages_per_keyword={mpk} 超过硬顶 {HARD_MAX_PAGES_PER_KEYWORD}，已自动截断")
        mpk = HARD_MAX_PAGES_PER_KEYWORD

    mpd = _as_int(sec.get("max_pages_per_day", 300), "limits.max_pages_per_day")
    _expect(mpd >= 1, f"limits.max_pages_per_day: 至少为 1，得到 {mpd}")
    if mpd > HARD_MAX_PAGES_PER_DAY:
        warnings.append(f"limits.max_pages_per_day={mpd} 超过硬顶 {HARD_MAX_PAGES_PER_DAY}，已自动截断")
        mpd = HARD_MAX_PAGES_PER_DAY

    return LimitsCfg(page_delay_seconds=(lo, hi), max_pages_per_keyword=mpk,
                     max_pages_per_day=mpd)


def _load_ranking(sec: dict, warnings: list[str]) -> RankingCfg:
    cfg = RankingCfg()
    for key, attr in (("topn_overall", "topn_overall"),
                      ("topn_per_keyword", "topn_per_keyword"),
                      ("topn_rising", "topn_rising")):
        v = _as_int(sec.get(key, getattr(cfg, attr)), f"ranking.{key}")
        _expect(v >= 1, f"ranking.{key}: 至少为 1，得到 {v}")
        setattr(cfg, attr, v)

    weights = sec.get("weights", {
        "want_count": 0.40, "view_count": 0.20,
        "want_delta_7d": 0.25, "view_delta_7d": 0.15})
    _expect(isinstance(weights, dict), f"ranking.weights: 应为键值映射，得到 {weights!r}")
    _warn_unknown_keys(weights, WEIGHT_KEYS, "ranking.weights", warnings)
    total = 0.0
    for k in WEIGHT_KEYS:
        w = _as_num(weights.get(k, getattr(cfg, _WEIGHT_FIELD[k])), f"ranking.weights.{k}")
        _expect(0 <= w <= 1, f"ranking.weights.{k}: 应在 [0, 1]，得到 {w}")
        setattr(cfg, _WEIGHT_FIELD[k], w)
        total += w
    _expect(abs(total - 1.0) <= 0.01,
            f"ranking.weights: 四项权重之和应为 1.0，当前为 {total:.2f}")
    return cfg


def _load_paths(sec: dict) -> PathsCfg:
    def _dir(key: str, default: Path) -> Path:
        v = sec.get(key, str(default))
        p = Path(_as_str(v, f"paths.{key}"))
        return p if p.is_absolute() else (ROOT / p)

    cfg = PathsCfg(data_dir=_dir("data_dir", DATA_DIR),
                   reports_dir=_dir("reports_dir", REPORTS_DIR),
                   logs_dir=_dir("logs_dir", LOG_DIR))
    cfg.ensure()
    return cfg


def load_settings(path: Path | None = None) -> tuple[Settings, list[str]]:
    """加载全局配置。文件缺失 → 内置默认值 + 告警；非法 → ConfigError。"""
    path = Path(path) if path else CONFIG_DIR / "settings.yaml"
    warnings: list[str] = []
    if not path.exists():
        warnings.append(f"配置文件不存在，使用内置默认值: {path}")
        data: dict = {}
    else:
        data = _parse_yaml(path)
        _expect(isinstance(data, dict), f"{path}: 顶层应为键值映射")
        _warn_unknown_keys(data, set(SETTINGS_KEYS), "settings", warnings)

    return Settings(
        schedule=_load_schedule(_section(data, "schedule", warnings), warnings),
        limits=_load_limits(_section(data, "limits", warnings), warnings),
        ranking=_load_ranking(_section(data, "ranking", warnings), warnings),
        report=ReportCfg(retain_days=_as_int(
            _section(data, "report", warnings).get("retain_days", 90),
            "report.retain_days")),
        paths=_load_paths(_section(data, "paths", warnings)),
    ), warnings


# ---- keywords.yaml 加载 ----

TASK_KEYS = {"keyword", "enabled", "max_pages", "sort",
             "price_min", "price_max", "stuff", "location"}


def _load_task(raw: Any, idx: int, warnings: list[str]) -> KeywordTask:
    where = f"tasks[{idx}]"
    _expect(isinstance(raw, dict), f"{where}: 应为键值映射，得到 {raw!r}")
    _warn_unknown_keys(raw, TASK_KEYS, f"keywords.{where}", warnings)

    keyword = _as_str(raw.get("keyword"), f"{where}.keyword")
    max_pages = _as_int(raw.get("max_pages", 3), f"{where}.max_pages")
    _expect(max_pages >= 1, f"{where}.max_pages: 至少为 1，得到 {max_pages}")
    if max_pages > HARD_MAX_PAGES_PER_KEYWORD:
        warnings.append(f"{where}（{keyword}）max_pages={max_pages} "
                        f"超过硬顶 {HARD_MAX_PAGES_PER_KEYWORD}，已自动截断")
        max_pages = HARD_MAX_PAGES_PER_KEYWORD

    sort = _as_str(raw.get("sort", "most_want"), f"{where}.sort")
    _expect(sort in ALLOWED_SORTS,
            f"{where}.sort: 允许值 {list(ALLOWED_SORTS)}，得到 {sort!r}")

    price_min = price_max = None
    if "price_min" in raw:
        price_min = _as_num(raw["price_min"], f"{where}.price_min")
        _expect(price_min >= 0, f"{where}.price_min: 不能为负，得到 {price_min}")
    if "price_max" in raw:
        price_max = _as_num(raw["price_max"], f"{where}.price_max")
        _expect(price_max > 0, f"{where}.price_max: 应为正数，得到 {price_max}")
    if price_min is not None and price_max is not None:
        _expect(price_min <= price_max,
                f"{where}: price_min({price_min}) 不能大于 price_max({price_max})")

    return KeywordTask(
        keyword=keyword,
        enabled=_as_bool(raw.get("enabled", True), f"{where}.enabled"),
        max_pages=max_pages,
        sort=sort,
        price_min=price_min, price_max=price_max,
        stuff=_as_str(raw["stuff"], f"{where}.stuff") if "stuff" in raw else None,
        location=_as_str(raw["location"], f"{where}.location") if "location" in raw else None,
    )


def load_keywords(path: Path | None = None) -> tuple[list[KeywordTask], list[str]]:
    """加载关键词任务。文件缺失或任务为空 → ConfigError。"""
    path = Path(path) if path else CONFIG_DIR / "keywords.yaml"
    warnings: list[str] = []
    _expect(path.exists(), f"关键词配置文件不存在: {path}")
    data = _parse_yaml(path)
    _expect(isinstance(data, dict), f"{path}: 顶层应为键值映射")
    _warn_unknown_keys(data, {"tasks"}, "keywords", warnings)
    raw_tasks = data.get("tasks")
    _expect(isinstance(raw_tasks, list), f"keywords.tasks: 必须是列表，得到 {raw_tasks!r}")
    _expect(len(raw_tasks) > 0, "keywords.tasks: 至少配置 1 个关键词任务")

    tasks: list[KeywordTask] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_tasks):
        task = _load_task(raw, i, warnings)
        if task.keyword in seen:
            warnings.append(f"tasks[{i}]: 关键词「{task.keyword}」重复，后项将被忽略")
            continue
        seen.add(task.keyword)
        tasks.append(task)

    if not any(t.enabled for t in tasks):
        warnings.append("keywords.tasks: 全部任务处于停用状态，采集将无事可做")
    return tasks, warnings


def enabled_tasks(tasks: list[KeywordTask]) -> list[KeywordTask]:
    return [t for t in tasks if t.enabled]


# ---- keyword 增删命令的文件操作（会重写文件，注释以仓库内示例为准）----

_KEYWORDS_HEADER = (
    "# GoofishRadar 关键词任务列表\n"
    "# 字段说明：keyword 关键词 / enabled 启用开关 / max_pages 采集页数(硬顶50)\n"
    "#           sort 排序(most_want|comprehensive|newest|price_asc|price_desc)\n"
    "#           可选筛选：price_min/price_max/stuff(成色)/location(所在地)\n"
)


def write_keywords(path: Path, tasks: list[KeywordTask]) -> None:
    data = {"tasks": [
        {"keyword": t.keyword, "enabled": t.enabled, "max_pages": t.max_pages,
         "sort": t.sort, **({"price_min": t.price_min} if t.price_min is not None else {}),
         **({"price_max": t.price_max} if t.price_max is not None else {}),
         **({"stuff": t.stuff} if t.stuff else {}),
         **({"location": t.location} if t.location else {})}
        for t in tasks]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_KEYWORDS_HEADER + yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def keyword_add(path: Path, keyword: str, max_pages: int = 3, sort: str = "most_want",
                price_min: float | None = None, price_max: float | None = None,
                stuff: str | None = None, location: str | None = None,
                enabled: bool = True) -> list[str]:
    tasks, warnings = load_keywords(path)
    if any(t.keyword == keyword.strip() for t in tasks):
        raise ConfigError(f"keywords.tasks: 关键词「{keyword}」已存在")
    new_task = KeywordTask(keyword=keyword.strip(), enabled=enabled,
                           max_pages=max_pages, sort=sort, price_min=price_min,
                           price_max=price_max, stuff=stuff, location=location)
    tasks.append(new_task)
    # 新任务先过一遍校验（非法 sort/页数等在这里报错）
    _load_task(_task_to_raw(new_task), len(tasks) - 1, warnings)
    write_keywords(path, tasks)
    return warnings


def keyword_remove(path: Path, keyword: str) -> bool:
    tasks, _ = load_keywords(path)
    remaining = [t for t in tasks if t.keyword != keyword.strip()]
    if len(remaining) == len(tasks):
        return False
    _expect(remaining, "keywords.tasks: 不能移除最后一个任务（至少保留 1 个）")
    write_keywords(path, remaining)
    return True


def _task_to_raw(t: KeywordTask) -> dict:
    return {"keyword": t.keyword, "enabled": t.enabled, "max_pages": t.max_pages,
            "sort": t.sort, **({"price_min": t.price_min} if t.price_min is not None else {}),
            **({"price_max": t.price_max} if t.price_max is not None else {}),
            **({"stuff": t.stuff} if t.stuff else {}),
            **({"location": t.location} if t.location else {})}
