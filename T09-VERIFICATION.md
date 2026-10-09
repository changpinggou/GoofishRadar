# T09 验收文档 — 每日定时调度与断点续采

| 项目 | 内容 |
| --- | --- |
| 需求单 | T09 每日定时调度与断点续采（里程碑 M3，优先级 P0，预估 1 天） |
| 验收日期 | 2026-10-09 |
| 验收环境 | macOS (darwin 24.6.0, arm64) / launchd / Python 3.14.5 |
| 验收结论 | ⚠️ **有条件通过** —— 调度已真实安装并加载（launchd 确认）、断点续采/重试/告警全部单测通过（120/120）；「连续 3 天自动产出报告」为观察期指标（自动累积中），实况断点续采联调待风控冷却后随每日运行自然完成 |

---

## 一、任务清单完成情况

| # | 任务（需求单原文） | 状态 | 证据 |
| --- | --- | --- | --- |
| 1 | launchd 安装/卸载脚本，按配置时刻注册，日志重定向 logs/ | ✅ 完成 | `scheduler.py`（plistlib 生成 + launchctl load/unload）+ CLI `schedule install/uninstall/status`；**已真实安装**：`~/Library/LaunchAgents/com.goofishradar.daily.plist` 存在、`launchctl list` 可见 `com.goofishradar.daily`、stdout/stderr 重定向 `logs/launchd_daily.log`；重复安装幂等（单测） |
| 2 | 启动时刻随机化（±15 分钟错峰） | ✅ 完成 | plist 按最早配置时刻（08:00）拉起；`_daily.py` 按工作日/周末解析实际时刻（weekend_time 独立可配）±randomize_minutes 随机；50 次抽样单测证明落窗；时刻已过时顺延 10–90 秒（实况验证：15:18 手动拉起 → 计划 15:18:43，钳制生效） |
| 3 | 断点续采：当日已完成关键词默认跳过（--force 重采） | ✅ 完成 | `_resume_filter()`：查当日 `crawl_run result='ok'` 去重跳过；单测两场景（跳过 + force 重采）；CLI `run --force` |
| 4 | 单关键词失败重试 2 次（指数退避 1min/5min） | ✅ 完成 | `run_daily` 退避循环：仅重试非风控失败词（结果按词合并，crawl_run 记最终态）；单测三场景（重试成功合并 / 风控不重试 / 失败隔离）；测试可注入零退避 |
| 5 | 连续 2 天失败告警（data/ALERT 标记 + 日志） | ✅ 完成 | `_consecutive_failure_alert()`：今日与昨日运行记录全部无 ok → 写 `data/ALERT` + ERROR 日志；单测两场景（触发/成功日不触发） |

## 二、交付物清单

| 交付物 | 位置 |
| --- | --- |
| 调度模块 | `src/goofishradar/scheduler.py`（plist 生成/装卸）+ `src/goofishradar/_daily.py`（错峰入口） |
| 安装/卸载命令 | `goofishradar schedule install / uninstall / status` |
| pipeline 增强 | 断点续采、重试退避、告警（`run --force` CLI） |
| 单元测试 | `tests/test_scheduler.py` **13 用例**（项目累计 **120/120**）+ `tests/test_pipeline.py` 全部离线用例改为零退避（重试默认退避仅实况使用） |
| 已安装调度 | `~/Library/LaunchAgents/com.goofishradar.daily.plist`（launchd 已加载，明早 08:00 首次自动运行） |

## 三、验收标准核验（需求单 DoD）

### 3.1 无人工干预连续 3 天按配置时刻（±15 分钟）自动产出报告 —— ⏳ 观察期

- 调度已真实安装并加载（安装即验证：plist 存在 + launchctl 可见 + status 命令 exit 0）；
- 观察期自明日（2026-10-10）08:00±15 分钟首次自动运行起算，连续 3 天的产出记录将在 T09 文档补记（`logs/launchd_daily.log` + `crawl_run` + `reports/` 三方留痕，可自动核验）；
- 前置依赖：风控冷却（见 3.2）。若明晨仍被拦截，调度会按重试策略自动重试并在报告中如实记录——无人值守语义本身不受影响。

### 3.2 手动中断 run 后重跑，已完成关键词被跳过，总耗时明显缩短 —— ✅ 通过（单测）/ ⏳ 实况待复验

- 单测 `test_done_keywords_skipped_today`：当日 iPhone 已 ok → 重跑仅采 [乐高, 佳能相机]，且不重复记录 iPhone；`test_force_recollects_done_keywords`：`--force` 重采；
- 实况复验并入 T08 待复验链（同一风控阻塞），复验命令见第五节。

### 3.3 `uninstall schedule` 可干净移除定时项 —— ✅ 通过

- 单测：卸载后 plist 文件移除、重复卸载返回 False；
- 实况：安装态经 `launchctl list` 与 `schedule status` 双重确认；卸载路径与安装对称（unload→unlink），T09 交付保留安装态（产品意图），交付前未执行卸载——验收方可在任一时点运行 `goofishradar schedule uninstall` 验证。

## 四、设计说明

1. **plist 时刻与实际时刻分离**：launchd StartCalendarInterval 固定触发在「工作日/周末目标时刻的较早者」，实际时刻由 `_daily` 按当日属性二次校正（±randomize_minutes 随机）——launchd 不支持原生随机/按日区分时刻，此结构以最小复杂度实现 PRD 两项要求；
2. **重试不放大风控**：`risk_blocked` 失败一律不重试（避免火上浇油，T11 输入）；普通失败（超时/解析）按 60s/300s 退避重试 2 次；重试期间再次风控则立即中止；
3. **告警口径**：连续 2 天「运行记录全部失败」——会话失效导致零运行的天由 SESSION_EXPIRED 标记独立告警（T02），两套信号互补；
4. **离线测试零退避**：默认退避仅实况使用，全部离线用例显式传 `retry_backoff_seconds=(0, 0)`（本单曾因旧测试未传参导致套件挂起 6 分钟，已全量修正——教训：新增全局默认行为时必须回归全部既有调用点）。

## 五、复验清单

**实况断点续采（随 T08 复验一并完成，约 12 分钟）**：
```bash
.venv/bin/goofishradar check                                # ① exit 0 后继续
.venv/bin/goofishradar run --keyword iPhone                 # ② 单词采集（~2 分钟）
.venv/bin/goofishradar run                                  # ③ 期望日志「断点续采：跳过今日已完成 ['iPhone']」并只采其余 4 词
.venv/bin/goofishradar status                               # ④ 今日 5 行 ok
```

**3 天观察期核验（第 3 天后执行一次）**：
```bash
.venv/bin/goofishradar status                               # 三个日期各有 5 行 ok
tail logs/launchd_daily.log                                 # 三次「计划 … 执行 run」且时刻在 08:00±15 分钟内
ls reports/                                                 # 三个日期目录各有 report.html
```

## 六、对后续需求单的输入

| 需求单 | 输入 |
| --- | --- |
| T11 | 风控顽固性持续实证（10-09 全天拦截）；重试与退避已按「风控不重试」实现，T11 需补充小时级冷却档位与 check exit 4 的告警联动 |
| T12 | `data/ALERT` 标记 + `crawl_run` + `launchd_daily.log` 三处告警/运行数据源已就绪，推送层直接消费 |
| T10 | 断点续采下商品可能只在天内有部分词快照——增量口径不受影响（按 item_id 序列计算） |

## 七、执行记录时间线（2026-10-09）

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 14:30 | scheduler/_daily/pipeline 重试与续采/CLI + 单测编写 | 套件挂起 → 定位旧测试未传零退避触发生产默认 60/300s → 全量修正 |
| 14:45 | 全量测试 | ✅ 120/120 |
| 14:50 | 风控探测 | 仍拦截（exit 4），确认冷却未完成 |
| 15:07 | 真实安装调度 + launchctl/status 双确认 | ✅ 已加载，明晨 08:00 首跑 |
| 15:18 | 错峰入口实况拉起测试 | ✅ 计划时刻计算与钳制正确（15:18:43，+28s） |
