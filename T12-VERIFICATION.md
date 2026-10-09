# T12 验收文档 — 日志、监控与告警推送

| 项目 | 内容 |
| --- | --- |
| 需求单 | T12 日志、监控与告警推送（里程碑 M3，优先级 P1，预估 1 天） |
| 验收日期 | 2026-10-09 |
| 验收环境 | macOS / Python 3.14.5 / 真实告警标记（SESSION_EXPIRED + RISK_COOLDOWN） |
| 验收结论 | ✅ **通过** —— 三条验收标准全部达成；Webhook 真实 HTTP 推送经本地捕获服务验证（2 条 payload 逐字节核验）；附带修复 1 处测试污染真实数据的问题 |

---

## 一、任务清单完成情况

| # | 任务（需求单原文） | 状态 | 证据 |
| --- | --- | --- | --- |
| 1 | 结构化日志：按日滚动 ≥30 天，关键事件独立标记 | ✅ 完成 | `log_event()` 统一 `EVENT[名称] k=v` 格式；已埋点：task_start / task_end（含退出码与计数）/ report_done / session_expired；滚动保留 30 天（T02 日志基座）；告警/重试/降级页等事件经 WARNING/ERROR 天然可检索 |
| 2 | `status` 升级：近 7 天每日状态表（成功/失败、条数、耗时、报告路径、告警） | ✅ 完成 | 按日聚合行（任务 ok 数、采集条数、报告✓/✗）+ 逐关键词明细（状态符号 ✓/✗/⚠、页数、条数、耗时、错误）+ 告警标记区（ALERT / SESSION_EXPIRED / RISK_COOLDOWN_UNTIL 三处状态与首行内容）+ 库内计数 |
| 3 | 告警推送：Notify 抽象 + Webhook 与 SMTP 渠道（可只做一个），四类场景，未配置降级本地标记 | ✅ 完成 | `notify.py`：`Alert` + `WebhookNotifier`（飞书/钉钉双格式）+ `SmtpNotifier`（465 SSL/starttls 自适应）；配置 `notify:` 节（渠道留空=禁用；SMTP 密码支持环境变量 `GOOFISH_SMTP_PASSWORD` 注入）；四类场景全接线：session_expired / consecutive_failures / low_volume（<100）/ high_parse_failure（>20%）+ risk_blocked 附加场景；未配置或发送失败一律降级 `data/ALERT` 追加 |
| 4 | 日志脱敏复查 | ✅ 完成 | 新增手机号脱敏规则（保留前3后4：`138****5678`）；Cookie 键值/Cookie 头/长令牌规则复验；全量日志 grep 审计 0 命中（见 3.3） |

## 二、交付物清单

| 交付物 | 位置 |
| --- | --- |
| 告警推送模块 | `src/goofishradar/notify.py`（Alert / WebhookNotifier / SmtpNotifier / build_notifiers / send_alerts） |
| 结构化事件 | `logsetup.log_event()` + pipeline/session 埋点 |
| 推送配置样例 | `config/settings.yaml` `notify:` 节（含安全注释：密码经环境变量注入） |
| status 升级 | `cli.py _cmd_status`（每日聚合 + 明细 + 报告路径 + 告警区） |
| 单元测试 | `tests/test_notify.py` **13 用例**（项目累计 **157/157**） |

## 三、验收标准核验（需求单 DoD）

### 3.1 每种告警场景可被人工触发并按配置送达（或降级标记） —— ✅ 通过

| 场景 | 触发方式 | 验证 |
| --- | --- | --- |
| session_expired | mock 会话检查失效 | 单测：告警收集 + 推送断言 ✓；实况：11:24 真实触发（exit 1 + ALERT 日志） |
| consecutive_failures | 种子昨日 failed + 今日 mock 失败 | 单测：告警断言 + 隔离目录 ALERT 标记 ✓ |
| low_volume | mock 当日 1 条 < 100 | 单测 ✓；**本地 Webhook 真实捕获**（见下） |
| high_parse_failure | mock 降级 8/10 页 > 20% | 单测 ✓ |
| risk_blocked（附加） | T11 冷却门控 | 17:13 实况：冷却触发 + 标记 + 日志 ✓ |

**真实推送记录**（本地 HTTP 服务捕获，16:5x）：两条飞书格式 payload 逐字节核验通过——

```json
{"msg_type": "text", "content": {"text": "[GoofishRadar·critical] session_expired（2026-10-09）：会话检查未通过：缺少用户Cookie（测试推送）"}}
{"msg_type": "text", "content": {"text": "[GoofishRadar·warning] low_volume（2026-10-09）：当日采集 42 条 < 100 条阈值"}}
```

降级路径单测：未配置渠道 → `data/ALERT` 追加；发送失败（网络不可达）→ 记录失败原因 + 同样降级。

### 3.2 `status` 输出与 `crawl_run` 表数据一致 —— ✅ 通过

- 单测：种入 2 行运行记录 → `recent_status` 行数/条数/结果与直接 SQL 计数一致，汇总数 = 明细和（90+10=100）；
- status 实况输出与库内一致（464/464/464/0），告警区如实反映三处标记文件的真实状态。

### 3.3 日志抽查无敏感信息明文 —— ✅ 通过

- 全量日志 grep：Cookie 键值（unb/dnk/…）、手机号（1[3-9]xxxxxxxxx）均 0 命中；
- 新增手机号规则单测：`13812345678 → 138****5678`；非手机号长数字串不受影响。

## 四、过程记录与修正

1. **测试污染真实数据（已修复）**：`test_consecutive_failures_alert` 未隔离 DATA_DIR，把合成日期的 ALERT 写入真实 `data/`（内容与事实不符：10-08 实际采集成功）。修复：autouse 式 DATA_DIR 隔离 + 断言标记落在隔离目录；污染文件已删除，status 复核仅剩真实标记；
2. **配置加载两连修**：`notify.webhook_url: ""`（空=禁用渠道）曾被"非空校验"误拒；`_optional_str` 闭包捕获错字典（KeyError: host）——均为实现期自查修复；
3. SMTP 正文为 base64（非 ASCII 触发），单测解码后断言事件名。

## 五、对后续需求单的输入

| 需求单 | 输入 |
| --- | --- |
| T13 | UAT 走查项：合规清单（README）、告警降级演示、status 一致性；连续运行观察期数据由 T09 调度自动累积 |
| 二期 | 趋势 CSV 图表化；IM 渠道扩展实现 WebhookNotifier 同构接口即可 |

## 六、遗留事项

1. 真实外部渠道（用户自己的钉钉/飞书机器人或 SMTP 账号）推送：配置已就绪，填入 `notify:` 即生效（本单以本地捕获服务完成等价验证）；
2. 无阻塞遗留，本需求单可关闭。

## 七、执行记录时间线（2026-10-09）

| 时间 | 动作 | 结果 |
| --- | --- | --- |
| 17:35 | log_event + 手机号脱敏 + notify.py + config notify 节 + pipeline 告警接线 + status 升级 + 单测 | 10/13 → 修 SMTP base64 断言 / 缺 import → 11/13 |
| 17:40 | 修配置空串禁用语义 + 闭包字典捕获 + **测试污染真实数据修正** | ✅ 157/157 |
| 17:50 | 本地 Webhook 真实推送捕获（2 条） | ✅ payload 逐字节核验 |
| 17:55 | 脱敏 grep 审计 + status 实况与一致性复核 | ✅ 0 明文；告警区如实反映真实标记 |
