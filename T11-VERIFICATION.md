# T11 验收文档 — 限流、重试与合规风控策略

| 项目 | 内容 |
| --- | --- |
| 需求单 | T11 限流、重试与合规风控策略（里程碑 M3，优先级 P0，预估 1 天） |
| 验收日期 | 2026-10-09 |
| 验收环境 | macOS / Python 3.14.5 / 真实风控拦截状态（17:13 实况触发冷却） |
| 验收结论 | ✅ **通过** —— 三条验收标准全部达成（含实况风控冷却链路演示）；合规自查清单入 README；重试/冷却参数统一入配置 |

---

## 一、任务清单完成情况

| # | 任务（需求单原文） | 状态 | 证据 |
| --- | --- | --- | --- |
| 1 | 全局节流器（任意相邻请求强制随机延时）+ 单日总页数硬上限 + "部分完成"报告 | ✅ 完成 | `throttle.py`：`GlobalThrottle`（跨关键词全局生效，首次放行即计时基线）+ `RequestAuditor`（逐条 JSONL 审计，日预算与间隔统计的唯一事实源）+ `DailyBudget`；预算用尽/风控中止 → 报告口径盒红色"⚠ 部分完成"标注（`extra_notes`，实况链路见 3.3） |
| 2 | 单任务页数硬顶 50（双保险） | ✅ 完成 | 配置层截断（T03）+ 代码层常量 `HARD_MAX_PAGES_PER_KEYWORD=50`；压测见 3.2 |
| 3 | 启动随机化与错峰 | ✅ 完成 | T09 `_daily` 已实现（±15 分钟，工作日/周末区分）；本单无改动 |
| 4 | 验证码/风控页识别：立即中止、置告警标记、报告标注不完整 | ✅ 完成 | 识别：`detect_risk_block`（T02 起共用）；中止：`RiskBlockedError` 全局中止；标记：`data/RISK_COOLDOWN_UNTIL`（含冷却截止时刻）+ SESSION_EXPIRED；报告标注：extra_notes 实况链路验证（见 3.3） |
| 5 | 退避重试统一实现并与 T04/T09 参数合并为一处配置 | ✅ 完成 | `limits.retry_backoff_seconds: [60, 300]` 入 settings.yaml（校验：两元素、0≤lo≤hi）；pipeline 缺省读配置（显式参数仍可覆盖）；T04 的 RateLimiter 收敛至 throttle 模块（collector 再导出保持兼容） |
| 6 | 合规自查清单写入 README | ✅ 完成 | README「合规自查清单（T11）」8 项：不绕过验证码/登录墙、不逆向接口、仅公开数据、仅个人自用、全局限速硬约束、错峰启动、仅 headed 真实浏览器、日志脱敏 |

## 二、交付物清单

| 交付物 | 位置 |
| --- | --- |
| 节流与风控模块 | `src/goofishradar/throttle.py`（GlobalThrottle / RequestAuditor / DailyBudget / RiskControl；RateLimiter 迁入） |
| 配置统一 | `limits.retry_backoff_seconds`、`limits.risk_cooldown_minutes`（含校验与告警） |
| 采集器接线 | collector 全局节流（跨关键词）+ 审计记录 + 审计制预算 + 风控触发/清除 |
| pipeline 增强 | 冷却门控（零网络拒绝）、RISK_BLOCKED→exit 2+触发冷却、部分完成 extra_notes |
| 报告增强 | 口径盒 `extra_notes` 警示区（红色加粗） |
| 单元测试 | `tests/test_throttle.py` **16 用例**（项目累计 **144/144**） |

## 三、验收标准核验（需求单 DoD）

### 3.1 抓包/日志统计确认任意相邻请求间隔 ≥ 配置下限 —— ✅ 通过

- **真实时序测量**（非模拟时钟）：GlobalThrottle(0.2, 0.35) 两次放行实测总耗时 ≥ 0.2s（单测断言）；固定 50ms 档连续 5 次放行，第 2 次起全部间隔 ≥ 50ms（跨调用全局生效）；
- **审计日志统计**：`RequestAuditor.gaps_seconds()` 以 JSONL 时间戳计算相邻间隔（单测构造 [9.0, 16.0] 精确断言）；实况运行的审计文件 `logs/request_audit_YYYY-MM-DD.jsonl` 随每次采集自动累积，随时可复核。

### 3.2 配置页数超限时代码层仍按硬顶执行 —— ✅ 通过（压测记录）

极限参数压测（2026-10-09 17:15，配置全部配到越界值后 `config-check` 实况输出）：

```
limits.max_pages_per_keyword=999  → WARNING 超过硬顶 50，已自动截断 → 生效 ≤ 50
limits.max_pages_per_day=99999    → WARNING 超过硬顶 300，已自动截断 → 生效 ≤ 300
page_delay_seconds=[1,3]          → WARNING 过快易触发风控（生效但告警，自担风险）
risk_cooldown_minutes=5           → WARNING 偏短（默认 120）
```

- 单测 `test_stress_config_over_limit_clamped_in_code` 固化（99999→300、999→50）；
- 运行时双保险：`_page_budget` 以审计日志计数为准（`300 - used`），审计不可被配置绕过。

### 3.3 模拟验证码页面触发时任务立即中止并告警 —— ✅ 通过

- **mock 验证码**：FakePage 返回"请完成 安全验证"文本 → `_check_risk` 抛 `RiskBlockedError`（单测）；正常页面不误报（单测）；
- **实况冷却链路**（17:13，真实风控拦截状态）：
  1. `check` → 真实页面命中"非法访问" → `RISK_BLOCKED` → **触发 120 分钟冷却**（`data/RISK_COOLDOWN_UNTIL = 19:13:25`）→ exit 4；
  2. **立即再次 `check` → 零网络请求**，门控直接拒绝（"风控冷却中（至 19:13），跳过网络校验"）exit 4；
  3. pipeline 同源门控：冷却期 `run` 直接 exit 2（不拉起浏览器），报告标注"部分完成"（单测 `test_report_carries_partial_complete_note`）；
- 告警标记：RISK_COOLDOWN_UNTIL（含截止时刻）+ SESSION_EXPIRED 双标记 + ALERT 日志。

## 四、设计说明

1. **审计日志作为唯一事实源**：日预算从"归档文件计数"升级为"审计请求计数"——包含归档失败的页，预算不可因落盘失败而虚高；
2. **冷却门控位置**：放在 pipeline/check 的**最前端**（网络请求为零），而不是采集失败后的被动退避——这是 T08/T09 复验输入"小时级冷却档位"的落地形态（`risk_cooldown_minutes` 可配）；
3. **冷却解除**：全程无风控事件（`not aborted`）自动清除标记；或冷却期自然过期（读取时惰性清理）；
4. **退出码语义修正**：会话检查命中风控页 → exit 2（采集失败类别）+ 冷却触发，不再误报为 exit 1（会话失效）——会话 Cookie 实际有效（T08 截图实证）；
5. **遗留微瑕**：RISK_BLOCKED 时 SESSION_EXPIRED 标记也会被写入（`_after_verdict` 对所有非有效判定写标记），两种标记并存不影响功能，语义精细化归 T12。

## 五、对后续需求单的输入

| 需求单 | 输入 |
| --- | --- |
| T12 | 三处告警源（ALERT / SESSION_EXPIRED / RISK_COOLDOWN_UNTIL）+ 审计日志 + crawl_run，推送层直接消费；`completeness()` 负值口径展示 |
| T13 | 合规自查清单（README）作为 UAT 走查基准；审计日志作为限速合规证据 |
| 二期 | FR-4 详情采集须复用 GlobalThrottle/审计/预算（节流已全局化，天然覆盖） |

## 六、遗留事项

1. SESSION_EXPIRED 与 RISK_COOLDOWN 双标记并存的语义精细化（T12）；
2. 无阻塞遗留，本需求单可关闭。

## 七、执行记录时间线（2026-10-09）

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 16:40 | throttle.py + config 扩展 + collector/pipeline/report/CLI 接线 | 完成 |
| 16:50 | 单测编写 | 14/16 → 修测试缺 import → **16/16，全量 144/144** |
| 17:13 | 实况冷却链路演示 | ✅ 触发→120 分钟冷却→零网络门控拒绝，全链符合设计 |
| 17:15 | 极限参数压测 | ✅ 四类越界全部告警/截断，硬顶不可突破 |
