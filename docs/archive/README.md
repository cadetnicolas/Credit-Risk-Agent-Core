# 归档说明（历史设计稿 / 作废文档）

本目录存放从 `docs/` 根目录移入的历史文档。**这些不是交付面文档，不作为实现依据。**

现行交付面为 **4 个 SKILL + 11 个 MCP 工具 + stdio Connector**：

- 交付面与目录结构以根目录 `README.md` 为准；
- WorkBuddy 接入步骤以 `docs/workbuddy-skill-mcp-integration-guide.md` 为准；
- 功能验证结论以 `docs/skill-mcp-validation-2026-11-11.md` 为准；
- 审查证据以 `docs/credit-skill-mcp-audit-2026-11-11.html` 为准。

本目录文件仅作历史决策与验证记录追溯，**保留原文（含已失效的命令、接口与内部相对链接），请勿据此实现或验收**。移动仅改变位置，不改文件名、不改正文（仅各文件顶部新增状态抬头）。

## 归档清单

| 文件名 | 状态 | 被哪份现行文档取代 | 为何保留 |
| --- | --- | --- | --- |
| `CREDIT_AGENT_DEVELOPMENT_PLAN_V3.md` | 未实施 | `README.md`、`docs/workbuddy-skill-mcp-integration-guide.md` | 记录 V3 阶段划分、Gate F / Gate P 里程碑与人力/周期假设；正文自认「F3 实施中，F4 及后续尚未实施」，并引用 6 个不存在的 `requirements/*.md` 与 `AGENT_ARCHITECTURE_V2_ROADMAP.md`，且 :108 写「五项现有 Skill」（实际 4 个）。供追溯历史规划，非已交付功能。 |
| `CREDIT_AGENT_PRODUCT_DESIGN_V3.md` | 未实施 | `README.md`、`docs/workbuddy-skill-mcp-integration-guide.md` | 记录 Agent Platform 1.0 / Credit Studio 1.0 目标设计；:182 写「五项现有 Skill」（实际 4 个）。目标设计未落地，供追溯。 |
| `credit-analysis-modeling-prd.md` | 未实施（待确认） | `README.md`、`docs/skill-mcp-validation-2026-11-11.md` | 记录「存量监控归因 / 建模回测」两个一级产品的需求设想；:294、:297 承诺的 AgentProfile、React 前端、`cutoff_optimize` / `ModelingAnalyzer` 封装未落地。 |
| `credit-risk-workbench-product-design.md` | 未实施（待评审草案） | `README.md`、`docs/workbuddy-skill-mcp-integration-guide.md` | 记录统一研究工作台的产品草案、角色权限与指标阈值建议，均未实施，供追溯。 |
| `DATA.md` | 已作废 | `README.md`、`docs/skill-mcp-validation-2026-11-11.md` | 记录 mock 数仓规模与字段设想；:3、:81 的 `credit-agent init` 与 `tests/test_e2e.py::test_report_identifies_the_injected_anomaly` 从未存在。现行数据输入以 stdio MCP 的 `--data-root` 与公开数据验证为准。 |
| `DATA_INPUT.md` | 已作废 | `README.md`、`docs/workbuddy-skill-mcp-integration-guide.md`（第 2、5 节） | 记录「三种数据输入方式」设想；`POST /topics`、`POST /datasets/upload`、`credit-agent run`、`code_sandbox` 均无实现，现行 MCP 不开放 HTTP 端口。 |
| `MODELING_TOOLKIT.md` | 被取代 | `README.md`、`docs/skill-mcp-validation-2026-11-11.md` | 记录 `src/credit_agent/modeling/` 的模块结构及「尚未接成 `ToolDefinition`」的历史状态；现行建模能力已通过 MCP 工具暴露，以复核报告为准。 |
| `model-strategy-validation-report.md` | 被取代 | `docs/skill-mcp-validation-2026-11-11.md` | 记录同日一次旧抽样的功能验证；:15、:28、:40 的反欺诈指标（62 笔 / 留出 AUC 0.9796 / KS 0.8509）与现行报告（64 笔 / AUC 0.974513 / KS 0.822966）冲突，现行报告已注明「不要混用两次抽样指标」，可复现脚本为 `scripts/verify_public_datasets.py`。 |

> 补充：`model-strategy-validation-report.md` 顶部已加「已被取代」抬头，写清两次抽样指标不同、不可混用及可复现脚本；其余 7 份顶部均加「历史规划 / 作废」抬头，指明现行交付以 `README.md` 与 `docs/workbuddy-skill-mcp-integration-guide.md` 为准。

## 移动后残留的失效引用 / 断链清单

移动后，`docs/` 内**其它现行文档**对这 8 个文件没有 Markdown/HTML 相对超链接，因此没有新增真正会 404 的超链接；但存在以下**路径引用 / 文件名提及**会因位置改变而失效或误导。按分工，本目录不修改这些现行文档，交由主代理处理：

| 现行文档 | 位置 | 引用形式 | 指向的归档文件 | 移动后应指向 |
| --- | --- | --- | --- | --- |
| `docs/skill-mcp-validation-2026-11-11.md` | :45 | 行内代码（文件名提及，非超链接） | `model-strategy-validation-report.md` | `docs/archive/model-strategy-validation-report.md`；该句说明「不要混用两次抽样指标」，语义仍成立，仅文件名路径过时 |
| `docs/credit-skill-mcp-audit-2026-11-11.html` | :196 | `<code>` 证据路径引用 | `docs/CREDIT_AGENT_DEVELOPMENT_PLAN_V3.md:108`、`CREDIT_AGENT_PRODUCT_DESIGN_V3.md:182` | `docs/archive/CREDIT_AGENT_DEVELOPMENT_PLAN_V3.md`、`docs/archive/CREDIT_AGENT_PRODUCT_DESIGN_V3.md` |
| `docs/credit-skill-mcp-audit-2026-11-11.html` | :197 | `<code>` 证据路径引用 | `docs/DATA.md:3, 81`、`docs/DATA_INPUT.md` | `docs/archive/DATA.md`、`docs/archive/DATA_INPUT.md` |
| `docs/credit-skill-mcp-audit-2026-11-11.html` | :199 | `<code>` 证据路径引用 | `model-strategy-validation-report.md:15,28,40` | `docs/archive/model-strategy-validation-report.md` |

以下为**移动未影响**或**原本即已断链**的项，供主代理核对，无需因本次移动处理：

- `docs/workbuddy-skill-mcp-integration-guide.md` 未引用这 8 个文件；其 :156 的 `./skill-mcp-validation-2026-11-11.md` 指向现行文档，链接仍有效。
- 根目录 `README.md`、`workbuddy/README.md` 经全仓检索未引用这 8 个文件。
- 归档文件之间的相对链接（`CREDIT_AGENT_DEVELOPMENT_PLAN_V3.md:3` ⇄ `CREDIT_AGENT_PRODUCT_DESIGN_V3.md:3` 的 `[产品设计]` / `[开发计划]`）因两者同在 `archive/`，**链接仍有效**。
- 归档文件中原本就指向不存在目标的引用（如 `OPEN_SOURCE_AGENT_FRAMEWORK_SELECTION.md`、`requirements/*.md`、`AGENT_ARCHITECTURE_V2_ROADMAP.md`、`credit_risk_agent/docs/CREDIT_AGENT_PRODUCT_DESIGN_V3.md` 在 `credit-analysis-modeling-prd.md:7` 的行内路径），属于移动前既有问题，非本次移动引入。
