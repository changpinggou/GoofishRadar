"""GoofishRadar 命令入口（T02 login/check；T03 keyword/config-check；T08 run/status/report）。

退出码约定：
- run：0 成功；1 会话失效；2 采集失败（含风控中止，已成功部分照常入库出报告）；3 报告失败；配置非法加载即退 2
- login：0 成功（含免扫码复用）；10 扫码超时/失效；4 风控拦截；2 配置非法
- check：0 有效；1 失效（采集任务应终止）；4 风控拦截；2 配置非法
- report：0 成功；3 报告失败；2 配置非法
- config-check：0 有效；1 配置非法
- keyword：0 成功；1 失败（含配置非法）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def _cmd_schedule(args, log) -> int:
    from pathlib import Path
    from . import scheduler
    if args.sched_command == "status":
        exists = scheduler.plist_path().exists()
        loaded = scheduler.is_loaded()
        print(f"plist: {scheduler.plist_path()} {'存在' if exists else '不存在'}")
        print(f"launchd 加载: {'是' if loaded else '否'}")
        if exists:
            import plistlib
            data = plistlib.loads(scheduler.plist_path().read_bytes())
            print(f"触发时刻: 每天 {data['StartCalendarInterval']['Hour']:02d}:"
                  f"{data['StartCalendarInterval']['Minute']:02d}（实际执行由错峰入口二次校正）")
            print(f"日志: {data['StandardOutPath']}")
        return 0 if exists and loaded else 1

    settings = _load_or_exit(log)
    if args.sched_command == "install":
        pfile = scheduler.install_schedule(settings)
        hour, minute = scheduler.earliest_fire(settings)
        log.info("调度已安装：%s（每天 %02d:%02d 拉起，错峰入口按工作日/周末时刻 ±%d 分钟执行）",
                 pfile, hour, minute, settings.schedule.randomize_minutes)
        log.info("launchd 日志: %s", Path(settings.paths.logs_dir) / "launchd_daily.log")
        return 0
    if args.sched_command == "uninstall":
        if scheduler.uninstall_schedule():
            log.info("调度已移除")
            return 0
        log.warning("未发现已安装的调度")
        return 1
    return 2


def _cmd_status(log) -> int:
    from .pipeline import recent_status
    from .storage import Store
    settings = _load_or_exit(log)
    store = Store(settings.paths.data_dir / "goofishradar.db")
    try:
        rows = recent_status(store, days=7)
        counts = store.counts()
    finally:
        store.close()

    print("近 7 天运行概况：")
    if not rows:
        print("  （暂无运行记录）")
    else:
        # 按日聚合 + 逐关键词明细（T12 任务 2：含报告路径与告警状态）
        by_day: dict[str, list[dict]] = {}
        for r in rows:
            by_day.setdefault(r["run_date"], []).append(r)
        for day in sorted(by_day, reverse=True):
            day_rows = by_day[day]
            ok = sum(1 for r in day_rows if r["result"] == "ok")
            items = sum(r["items_total"] or 0 for r in day_rows)
            report = settings.paths.reports_dir / day / "report.html"
            report_mark = "报告✓" if report.exists() else "报告✗"
            print(f"  ▍{day}  任务 {ok}/{len(day_rows)} ok  采集 {items} 条  {report_mark}")
            for r in sorted(day_rows, key=lambda x: x["keyword"]):
                mark = {"ok": "✓", "failed": "✗", "risk_blocked": "⚠"}.get(
                    r["result"], "?")
                print(f"    {mark} {r['keyword']:<14} {r['result']:<14}"
                      f"页 {r['pages_ok']}/{r['pages_attempted']}"
                      f"  {r['items_total'] or 0} 条"
                      f"  {(str(r['duration_seconds']) + 's') if r['duration_seconds'] else '—'}")
                if r["error"]:
                    print(f"      └─ {r['error'][:80]}")

    # 告警状态（T12：三处标记文件）
    from .paths import DATA_DIR
    alerts = []
    for name in ("ALERT", "SESSION_EXPIRED", "RISK_COOLDOWN_UNTIL"):
        p = DATA_DIR / name
        if p.exists():
            first = p.read_text(encoding="utf-8").splitlines()[:1]
            alerts.append(f"{name}（{(first[0] if first else '').strip()[:60]}）")
    print("\n告警标记：")
    if alerts:
        for a in alerts:
            print(f"  ⚠ {a}")
    else:
        print("  无")

    print(f"\n库内：商品 {counts['items']} | 快照 {counts['snapshots']} | "
          f"来源关系 {counts['item_keywords']} | 运行记录 {counts['runs']}")
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

    p_run = sub.add_parser("run", help="执行每日任务：会话检查→采集→入库→评分→报告")
    p_run.add_argument("--keyword", action="append", default=None,
                       help="仅运行指定关键词（可多次传入）")
    p_run.add_argument("--force", action="store_true",
                       help="忽略断点续采，重采当日已完成关键词")
    p_run.add_argument("--skip-session-check", action="store_true",
                       help=argparse.SUPPRESS)

    sub.add_parser("status", help="近 7 天运行概况与库内计数")

    p_rep = sub.add_parser("report", help="基于已有库内数据（重）出报告")
    p_rep.add_argument("--date", default=None, help="报告日期（缺省=今天）")

    p_sched = sub.add_parser("schedule", help="每日定时调度（macOS launchd）")
    sched_sub = p_sched.add_subparsers(dest="sched_command", required=True)
    sched_sub.add_parser("install", help="安装/刷新调度（幂等）")
    sched_sub.add_parser("uninstall", help="移除调度")
    sched_sub.add_parser("status", help="查看调度安装状态")

    p_ih = sub.add_parser("item-history", help="商品逐日快照趋势（CSV 输出）")
    p_ih.add_argument("item_id", help="商品 ID")
    p_ih.add_argument("--date", default=None, help="参考日（缺省=今天，用于中断判定）")
    p_ih.add_argument("--out", default=None, help="写入文件（缺省打印到终端）")

    p_arch = sub.add_parser("snapshot-archive",
                            help="归档超期明细快照（导出 CSV 后清理）")
    p_arch.add_argument("--days", type=int, default=None,
                        help="保留天数（缺省=配置 report.retain_days）")

    args = parser.parse_args(argv)
    log = get_logger()

    if args.command == "keyword":
        return _cmd_keyword(args)

    if args.command == "schedule":
        return _cmd_schedule(args, log)

    if args.command == "config-check":
        return _cmd_config_check(log)

    if args.command == "status":
        return _cmd_status(log)

    if args.command == "report":
        settings = _load_or_exit(log)
        from datetime import datetime
        from .report import generate_report
        from .storage import Store
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            out = generate_report(date_str, store, settings)
        except Exception as exc:
            log.error("报告生成失败：%s", exc)
            return 3
        finally:
            store.close()
        log.info("报告已生成：%s | %s", out["html"], out["excel"])
        return 0

    settings = _load_or_exit(log)

    if args.command == "item-history":
        settings = _load_or_exit(log)
        from .storage import Store
        from .trend import item_trend, trend_to_csv
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            trend = item_trend(store, args.item_id, args.date)
        finally:
            store.close()
        if trend is None:
            log.error("商品不存在：%s", args.item_id)
            return 1
        csv_text = trend_to_csv(trend)
        if args.out:
            Path(args.out).write_text(csv_text, encoding="utf-8")
            log.info("趋势已写入 %s（%d 天，中断=%s）",
                     args.out, trend.days, trend.interrupted_now)
        else:
            print(csv_text, end="")
            if trend.interrupted_now:
                log.warning("该商品采集已中断（最后快照 %s）", trend.last_snapshot)
        return 0

    if args.command == "snapshot-archive":
        settings = _load_or_exit(log)
        from .storage import Store
        from .trend import archive_old_snapshots
        retain = args.days if args.days is not None else settings.report.retain_days
        store = Store(settings.paths.data_dir / "goofishradar.db")
        try:
            result = archive_old_snapshots(
                store, retain, Path(settings.paths.data_dir) / "archive")
        finally:
            store.close()
        if result["archived"]:
            log.info("已归档 %d 条快照（保留 %d 天，截点 %s）→ %s",
                     result["archived"], retain, result["cutoff"],
                     result["archive_file"])
        else:
            log.info("没有超期快照需要归档（保留 %d 天，截点 %s）",
                     retain, result["cutoff"])
        return 0

    if args.command == "run":
        from .pipeline import run_daily
        summary = run_daily(settings, keyword_filter=args.keyword,
                            skip_session_check=args.skip_session_check,
                            force=args.force, log=log)
        if summary.report:
            pool = summary.report.get("pool_size")
            if pool is not None:
                log.info("报告已生成（榜池 %d 商品）", pool)
        return summary.exit_code()

    if args.command == "login":
        mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                             marker_path=settings.paths.session_expired_marker)
        verdict = mgr.login(wait_seconds=args.wait)
        if verdict.valid:
            return 0
        log.error("登录失败：%s", verdict.reason)
        return 4 if verdict.state is SessionState.RISK_BLOCKED else 10

    if args.command == "check":
        from .throttle import RiskControl
        risk = RiskControl(settings.paths.data_dir,
                           settings.limits.risk_cooldown_minutes)
        until = risk.active_until()
        if until:
            log.warning("风控冷却中（至 %s），跳过网络校验", until.strftime("%H:%M"))
            return 4
        mgr = SessionManager(user_data_dir=settings.paths.user_data_dir,
                             marker_path=settings.paths.session_expired_marker)
        verdict = mgr.check(headless=not args.headless)
        if verdict.valid:
            log.info("会话有效（%s）", verdict.reason)
            return 0
        if verdict.state is SessionState.RISK_BLOCKED:
            risk.trigger(verdict.reason)
            log.error("风控拦截：%s —— 已进入 %d 分钟冷却", verdict.reason,
                      settings.limits.risk_cooldown_minutes)
            return 4
        log.error("会话失效：%s —— 请运行 goofishradar login 重新扫码", verdict.reason)
        return 1

    return 2  # 不可达（argparse required=True）


if __name__ == "__main__":
    sys.exit(main())
