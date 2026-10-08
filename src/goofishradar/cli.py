"""GoofishRadar 命令入口（T02：login/check；T03：keyword/config-check；T08 将扩展 run/status/report）。

退出码约定：
- login：0 成功（含免扫码复用）；10 扫码超时/失效；4 风控拦截；2 配置非法
- check：0 有效；1 失效（采集任务应终止）；4 风控拦截；2 配置非法
- config-check：0 有效；1 配置非法
- keyword：0 成功；1 失败（含配置非法）
"""
from __future__ import annotations

import argparse
import sys

from .config import (
    ConfigError, Settings, enabled_tasks, keyword_add, keyword_remove,
    load_keywords, load_settings,
)
from .logsetup import get_logger
from .session import SessionState, SessionManager


def _load_or_exit(log) -> Settings:
    """加载全局配置；非法配置立即报错退出（T03 验收标准 3）。"""
    try:
        settings, warnings = load_settings()
    except ConfigError as exc:
        log.error("配置无效：%s", exc)
        raise SystemExit(2)
    for w in warnings:
        log.warning("配置：%s", w)
    settings.paths.ensure()
    return settings


def _cmd_keyword(args) -> int:
    log = get_logger()
    from pathlib import Path
    from .paths import CONFIG_DIR
    kw_file = Path(args.file) if args.file else CONFIG_DIR / "keywords.yaml"

    if args.kw_command == "list":
        try:
            tasks, warnings = load_keywords(kw_file)
        except ConfigError as exc:
            log.error("配置无效：%s", exc)
            return 1
        for w in warnings:
            log.warning("配置：%s", w)
        print(f"{'关键词':<14}{'状态':<6}{'页数':<6}{'排序':<14}筛选")
        for t in tasks:
            filters = ", ".join(filter(None, [
                f"价格 {t.price_min}-{t.price_max}" if t.price_min or t.price_max else None,
                f"成色 {t.stuff}" if t.stuff else None,
                f"所在地 {t.location}" if t.location else None])) or "—"
            print(f"{t.keyword:<14}{t.enabled_or_disabled_label():<6}{t.max_pages:<6}"
                  f"{t.sort:<14}{filters}")
        print(f"\n共 {len(tasks)} 个任务（启用 {len(enabled_tasks(tasks))} 个）")
        return 0

    if args.kw_command == "add":
        try:
            warnings = keyword_add(
                kw_file, args.keyword, max_pages=args.pages, sort=args.sort,
                price_min=args.price_min, price_max=args.price_max,
                stuff=args.stuff, location=args.location, enabled=not args.disabled)
        except ConfigError as exc:
            log.error("添加失败：%s", exc)
            return 1
        for w in warnings:
            log.warning("配置：%s", w)
        log.info("已添加关键词「%s」（注意：命令方式会重写 %s，手工注释以仓库示例为准）",
                 args.keyword, kw_file)
        return 0

    if args.kw_command == "remove":
        try:
            removed = keyword_remove(kw_file, args.keyword)
        except ConfigError as exc:
            log.error("移除失败：%s", exc)
            return 1
        if removed:
            log.info("已移除关键词「%s」", args.keyword)
            return 0
        log.error("关键词「%s」不存在", args.keyword)
        return 1

    return 2


def _cmd_config_check(log) -> int:
    try:
        settings, s_warn = load_settings()
        tasks, k_warn = load_keywords()
    except ConfigError as exc:
        log.error("配置无效：%s", exc)
        return 1
    for w in s_warn + k_warn:
        log.warning("配置：%s", w)
    lim, rk = settings.limits, settings.ranking
    print("✅ 配置合法，生效值：")
    print(f"  执行时刻: 工作日 {settings.schedule.resolve_time(False)} / "
          f"周末 {settings.schedule.resolve_time(True)}（随机 ±{settings.schedule.randomize_minutes} 分钟）")
    print(f"  延时: {lim.page_delay_seconds[0]}–{lim.page_delay_seconds[1]}s/页 | "
          f"单关键词 ≤ {lim.max_pages_per_keyword} 页 | 单日 ≤ {lim.max_pages_per_day} 页")
    print(f"  TopN: 总榜 {rk.topn_overall} / 分类榜 {rk.topn_per_keyword} / 飙升榜 {rk.topn_rising}")
    print(f"  权重: 想要 {rk.weight_want_count} + 浏览 {rk.weight_view_count} + "
          f"想要增量 {rk.weight_want_delta_7d} + 浏览增量 {rk.weight_view_delta_7d}")
    print(f"  报告保留: {settings.report.retain_days} 天 | 数据目录: {settings.paths.data_dir}")
    print(f"  关键词任务: {len(tasks)} 个（启用 {len(enabled_tasks(tasks))} 个）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="goofishradar", description="GoofishRadar — 闲鱼畅销榜自动化工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="扫码登录并持久化会话（必须有头，弹出浏览器窗口）")
    p_login.add_argument("--wait", type=int, default=300, help="等待扫码秒数（默认 300）")

    p_check = sub.add_parser("check", help="校验会话有效性（默认有头轻量访问首页）")
    p_check.add_argument("--headless", action="store_true",
                         help="无头模式校验（T02 实测 headless 可能被风控拦截首页，故默认有头）")

    p_kw = sub.add_parser("keyword", help="关键词任务管理（list/add/remove）")
    kw_sub = p_kw.add_subparsers(dest="kw_command", required=True)
    kw_sub.add_parser("list", help="列出全部关键词任务")
    p_add = kw_sub.add_parser("add", help="添加关键词任务（会重写 keywords.yaml）")
    p_add.add_argument("keyword")
    p_add.add_argument("--pages", type=int, default=3, help="采集页数（硬顶 50）")
    p_add.add_argument("--sort", default="most_want",
                       help="most_want|comprehensive|newest|price_asc|price_desc")
    p_add.add_argument("--price-min", type=float, default=None)
    p_add.add_argument("--price-max", type=float, default=None)
    p_add.add_argument("--stuff", default=None, help="成色筛选")
    p_add.add_argument("--location", default=None, help="所在地筛选")
    p_add.add_argument("--disabled", action="store_true", help="添加为停用状态")
    p_rm = kw_sub.add_parser("remove", help="移除关键词任务")
    p_rm.add_argument("keyword")
    for sub_p in kw_sub.choices.values():
        sub_p.add_argument("--file", default=None, help=argparse.SUPPRESS)

    sub.add_parser("config-check", help="校验两个配置文件并打印生效值")

    args = parser.parse_args(argv)
    log = get_logger()

    if args.command == "keyword":
        return _cmd_keyword(args)

    if args.command == "config-check":
        return _cmd_config_check(log)

    settings = _load_or_exit(log)

    if args.command == "login":
        mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                             marker_path=settings.paths.session_expired_marker)
        verdict = mgr.login(wait_seconds=args.wait)
        if verdict.valid:
            return 0
        log.error("登录失败：%s", verdict.reason)
        return 4 if verdict.state is SessionState.RISK_BLOCKED else 10

    if args.command == "check":
        mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                             marker_path=settings.paths.session_expired_marker)
        verdict = mgr.check(headless=not args.headless)
        if verdict.valid:
            log.info("会话有效（%s）", verdict.reason)
            return 0
        if verdict.state is SessionState.RISK_BLOCKED:
            log.error("风控拦截：%s —— 请稍后重试或更换网络环境", verdict.reason)
            return 4
        log.error("会话失效：%s —— 请运行 goofishradar login 重新扫码", verdict.reason)
        return 1

    return 2  # 不可达（argparse required=True）


if __name__ == "__main__":
    sys.exit(main())
