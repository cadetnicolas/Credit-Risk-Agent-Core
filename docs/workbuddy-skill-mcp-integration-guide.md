# WorkBuddy 接入信贷分析 SKILL 与 MCP：介绍及验证指南

版本：0.4（对应 pyproject 版本 0.5.0）  
适用对象：WorkBuddy 使用者、桌面端管理员、信贷分析验证人员  
对应代码：`credit_risk_agent/skills/`、`credit_risk_agent/src/credit_agent/mcp_server.py`

## 1. 目标与组成

本次接入用于在 WorkBuddy 中验证信贷数据准入、组合监控、策略研究和评分卡建模工作流。WorkBuddy 负责对话、加载 SKILL 和调用工具；信贷 MCP 服务负责在本机读取授权数据、运行计算、写入快照与分析账本。

| 组件 | 本项目内容 | 用途 |
| --- | --- | --- |
| SKILL | `credit-data-admission`、`portfolio-anomaly-attribution`、`policy-cutoff-simulation`、`scorecard-modeling-pipeline` | 规定适用场景、分析步骤、工具使用、阻断条件和结论边界 |
| MCP 服务 | `python -m credit_agent.mcp_server` | 通过 stdio 暴露数据检查、监控、策略、建模和报告工具 |
| 数据目录 | WorkBuddy 配置中的 `--data-root` | 允许 MCP 读取的 CSV/Parquet 文件 |
| 口径登记目录 | WorkBuddy 配置中的 `--registry-root` | 管理员维护的已签收 JSON/YAML 口径文件 |
| 分析账本 | WorkBuddy 配置中的 `--ledger-root` | 保存快照、成熟度、审批决定、运行摘要与产物 |

四个 SKILL 共声明十个业务工具，MCP 另提供 `list_credit_datasets`，共十一项。MCP 使用本地 stdio，不开放 HTTP 端口。数据访问由管理员配置的根目录限定；MCP 不提供文件任意路径参数。

WorkBuddy 文档说明，连接器包可以包含 `connector-meta.json`、`mcp.json` 与 `skills/{name}/SKILL.md`；MCP 支持 stdio，Skill 支持本地导入。不同 WorkBuddy 版本的菜单名称和导入方式可能不同，按当前客户端“本地 MCP 服务管理”和“技能”页面操作。参考[WorkBuddy Connector 文档](https://open.workbuddy.cn/en/docs/connector)和[WorkBuddy 技能文档](https://cloud.tencent.com/document/product/1831/134432)。

## 2. 开始前

1. 确认 WorkBuddy 桌面端安装了本地 MCP/连接器和 Skill 管理功能。记录客户端版本。
2. 选择专用于验证的本地数据目录、口径目录和账本目录。不要把整个用户目录或包含无关文件的共享目录设为数据根目录。
3. 在仓库环境安装 MCP 依赖，并把信贷工具安装到同一个 Python 环境：

   ```bash
   cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
   ../.venv/bin/python -m pip install -e '.[mcp]'
   ```

   开发工具还需 `pip install -e '.[mcp,dev]'`；生成的 Connector 包已在 `mcp.json` 的 `env.PYTHONPATH` 中显式声明项目 `src/`，兼容目标 Python 未执行 `pip install` 的场景。

4. 确认 WorkBuddy 启动 MCP 子进程时能执行该 Python，并能访问数据、口径、账本目录。不要依赖只在交互式终端中设置的临时 `PYTHONPATH`。
5. 本地端口或网络策略不需要为 stdio MCP 开放额外入站端口。
6. 三个根目录（`--data-root` / `--registry-root` / `--ledger-root`）必须是**两两互不相交的独立目录**；账本根不得放在数据根（或口径根）内，否则账本中的授权明细可能被 `list_credit_datasets` 当作授权数据文件列出。账本目录权限建议设为 `0700`。

本机安装 LightGBM 时还需可用的原生 OpenMP 库。若无法加载，工具会保留逻辑回归基线并返回降级状态，不应把它报告为完成双模型比较。

## 3. 构建 WorkBuddy Connector 包

`credit_risk_agent/` 是独立安装的信贷工具项目；`credit_risk_agent/workbuddy/` 是 WorkBuddy Connector 发布目录。运行打包器会读取当前 SKILL 源文件、生成 `connector-meta.json`、`mcp.json`、图标和 SKILL 包，避免复制维护两套 SKILL。先确保目标路径不存在：

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
python workbuddy/build_connector.py \
  --python /absolute/path/to/venv/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-connector
```

命令生成 Connector 目录和同名 ZIP。它不会覆盖已有目标路径。Windows 环境将 `--python` 改为虚拟环境中的 `python.exe`，并传入当前系统的绝对目录。数据根目录和口径目录必须已经存在；账本目录由 MCP 首次启动时创建。生成的 `mcp.json` 使用 stdio，由 WorkBuddy 启动本机 Python 子进程。若 WorkBuddy 运行在远程或云端环境，本机路径不会自动可见，需要另行部署远程 MCP 服务。

生成的 `mcp.json` 中 `timeout: 30000` 是**连接超时（毫秒）**，不是单次工具执行超时；训练、网格搜索等耗时工具可能超过该时长，应关注客户端自身的单次调用超时策略，不要把该值当作工具运行时限。`cwd` 字段需客户端 `minWorkbuddyVersion >= 4.22.15`，`name_zh` / `examples_*` 等字段需 `>= 4.24.0`；本包已在 `connector-meta.json` 声明 `minWorkbuddyVersion: 4.24.0`。

本地试点可直接导入生成的 ZIP；也可在 WorkBuddy 自定义 MCP 页面读取生成的 `mcp.json`，并独立导入 `skills/` 下的目录。WorkBuddy 连接器市场发布还需按企业发布流程复核 `source` 唯一性、图标、文案与服务部署方式。

配置后，在 WorkBuddy 的 MCP/连接器管理页面启用该服务，检查服务状态并查看工具列表。应能看到：

- 数据：`list_credit_datasets`、`inspect_and_bind_snapshot`、`check_label_maturity`、`resolve_metric_and_dimensions`
- 监控：`run_portfolio_monitoring`、`decompose_and_locate_anomaly`
- 策略：`simulate_policy_rules`
- 建模：`prepare_modeling_sample`、`screen_features`、`profile_binning`、`train_and_evaluate_candidate`
- 报告：`verify_and_commit_report`

v0.3 契约要点（SKILL 均为 v1.3.0）：

- 全部 14 个工具返回统一 envelope；`blocked` 必带 `block_code`，17 个码由 `src/credit_agent/tools/codes.py` 单一定义，SKILL 的停机码表与之对齐。
- `check_label_maturity` 新增 `observation_mode`（`monthly_cohort` / `latest_observed`）与 `maturity_choice_review_id`；`simulate_policy_rules` 新增 `economics`、`observation_mode`、`latest_review_id`，且 `rule_mining` 的 `features` 必须是登记表中 `point_in_time=decision` 的维度子集。
- `run_portfolio_monitoring` 的 `base_periods` / `comp_periods` 为必填；`decompose_and_locate_anomaly`、`screen_features` 暴露登记阈值参数；`screen_features` / `train_and_evaluate_candidate` 暴露分箱口径参数，`profile_binning` 单独出单特征分箱档；`prepare_modeling_sample` 暴露 `random_state`。
- `list_credit_datasets` 与其它工具一致返回 envelope（`summary.datasets`）。数据文件上限 100 MiB、口径文件 1 MiB；口径 YAML 禁止锚点/别名。
- `verify_and_commit_report` 支持整数与日期型 citation，并输出 `signoff_status`；未签收却写「已批准/建议上线」返回 `SIGNOFF_REQUIRED`。

## 4. 安装 SKILL

Connector 包中的 `skills/` 目录含四个源 SKILL 及 `references/` 文件。若 WorkBuddy 客户端使用单独的 Skill 管理页面，逐个导入并启用：

- `credit_risk_agent/skills/credit-data-admission/`
- `credit_risk_agent/skills/portfolio-anomaly-attribution/`
- `credit_risk_agent/skills/policy-cutoff-simulation/`
- `credit_risk_agent/skills/scorecard-modeling-pipeline/`

若客户端要求 ZIP 包，压缩每个 Skill 文件夹自身，保留 `SKILL.md` 位于 Skill 包根目录的结构，不要只上传 Markdown 文件。企业管理端可能对本地 Skill 导入、MCP 来源和用户启用范围施加策略；若看不到导入入口，请让 WorkBuddy 管理员检查相关策略。

安装后确认 Skill 名称和版本：数据准入、组合归因、策略阈值、评分卡建模共四个，各 Skill 版本以各自 `SKILL.md` frontmatter 的 `metadata.version` 为准。通过自然语言任务触发相应 Skill；不要假定安装 Skill 会自动授权未启用的 MCP 工具。

## 5. 口径文件和数据准备

MCP 服务允许读取管理员目录内的 JSON、YAML 或 YML 口径登记文件。登记文件必须同时包含 `metrics` 和 `dimensions` 两个列表。示例：

```yaml
metrics:
  - metric_id: mob3_dpd30_bal
    version: v1
    flag_column: dpd30_flag
    weight_column: principal
    period_column: mob
    signed_by: risk_owner
dimensions:
  - column: channel
    point_in_time: decision
    kind: categorical
    max_cardinality: 30
    min_cell_n: 50
```

口径由数据/风险负责人维护；Agent 不能临时编造指标版本、分子分母或签收人。文件只登记示意字段时，绑定工具会按真实输入和登记内容返回未签收/缺字段结果。YAML 解析对文件大小与别名数量设有限制，登记文件禁止使用锚点（`&`）/ 别名（`*`）等扩展写法，避免解析被拒绝或被放大。

数据文件应放在 `--data-root` 下，并具有可解释的主键、决策时点、观察/表现字段和目标标签。执行前通过 `list_credit_datasets` 确认文件名，再按实际字段调用准入流程。

## 6. 推荐验证流程

### 6.1 连接验证

在 WorkBuddy 中询问：

> 列出当前 MCP 服务可访问的信贷数据集，并说明各文件名。

检查 `list_credit_datasets` 是否返回授权目录中的 CSV/Parquet 文件。再选择一份测试样本执行快照绑定，确认返回 `snapshot_id` 和质量摘要。

### 6.2 SKILL 调度验证

依次提交以下验证任务，观察 WorkBuddy 是否选择对应 Skill、是否只调用其声明工具：

1. **数据准入**：“检查 `sample.csv` 的主键、决策日期和表现期成熟度；使用登记文件中的 `mob3_dpd30_bal v1` 口径。”
2. **存量监控与归因**：“对已准入快照比较两个指定账期，并定位有数据支撑的结构/组内变化。”
3. **策略研究**：“对已准入样本计算指定评分的分档、边际坏账率和调入调出人数；不要估算被拒客户表现或放宽收益。”
4. **评分卡建模**：“对有成熟标签和时间列的样本执行 OOT 评分卡流程，先展示特征审查清单，等待确认后再训练。”

每个任务检查：字段和指标是否来自真实登记；工具状态、样本数和阻断码是否被保留；是否把工具摘要数字用于报告；是否出现未经数据支持的建议。

### 6.3 条件不足时的交互验证

- **建模/监控成熟度（默认 `observation_mode="monthly_cohort"`）**：按放款批次月末 + W 个完整账龄月判定，符合 Vintage/MOB 惯例；`decision_time_column` 用于逐笔校验 T0 与登记批次月份一致，FPD 类窗口按自然日判定。
- **策略分析要尽可能用最新样本**：用 `check_label_maturity(observation_mode="latest_observed")` 生成含未成熟批次的最新可观察掩码，再以 `simulate_policy_rules(observation_mode="latest_observed", latest_review_id=...)` 做描述性测算。结果必须是 `degraded`，所有坏账率/Lift/正式推荐为空，正文不得写成目标表现期坏账率，也不得用于建模或上线收益测算。
- **没有成熟样本**：`check_label_maturity` 返回 `needs_human` 与三种选项：改用更短且已签收的表现期、仅做无标签描述分析、等待成熟。用户选择由受信任审批卡（`gate=maturity_choice`）落账后，把 `maturity_choice_review_id` 传回工具；未落账前不自动切换指标。
- **有成熟样本但不能形成 OOT**：建模工具应返回 `needs_human`，给出随机留出、仅交叉验证或停止选项。策略规则工具给出随机留出、描述性探索或停止选项。只有用户选择记录成功写入同一账本后，才能用 `fallback_review_id` 重试；确认单为一次性消费，不能重放。
- **回退结果**：建模与策略结果必须为 `degraded`；策略候选应留在 `candidates`，不得伪装成已通过 OOT 的 `recommended`。报告应说明验证方式、未覆盖的结论和所用确认记录。
- **工具异常**：任何失败都返回 `blocked` + `block_code` 的结构化 envelope（如 `RESOURCE_UNAVAILABLE`、`UNSUPPORTED_SCOPE`、`PERIOD_MISMATCH`），不会再抛裸异常；检查客户端是否按 SKILL 的停机码表处置，而不是重试或自行编造结论。

## 7. 审批闭环与当前集成限制

MCP 服务故意不把 `record_human_decision` 发布给 Agent，避免模型替用户确认。项目现已提供**受信任宿主侧参考适配器** `scripts/host_approval.py`：它在审批控件确认后把决定写入同一 `credit ledger`，并强制 `channel=approval_card`、`actor` 为真人标识。约定审批卡的 `review_id` 等于对应工具返回的 `run_id` / `review_id`：

```bash
python scripts/host_approval.py --ledger-root /absolute/credit-tool-ledger \
  --gate feature_list --review-id run_xxxxxxxxxxxx --actor risk_owner --features feat_a,feat_b
```

六个 gate：`feature_list`、`validation_fallback`、`maturity_choice`、`anomaly_disposition`、`policy_trial`、`model_signoff`。特征与验证回退确认单为一次性消费，不可重放；`verify_and_commit_report` 会输出 `signoff_status`，未签收却在正文写「已批准 / 建议上线 / 已立项」会返回 `SIGNOFF_REQUIRED`。

仍需宿主完成的部署工作：

- WorkBuddy 对话确认/审批控件到该适配器的接线（本项目不内置 WorkBuddy UI 控件）；未接线前，`needs_human` 回退路径与模型签收会停在待审批状态。
- 生产环境的一租户一进程/独立三个根目录、账本目录权限、工具发布范围与身份认证。
- 在客户端完成一次真实「needs_human → 审批卡 → 继续」的端到端验收；未接线前把回退验证记录为「交互提示已验证、确认闭环未验证」，不要手工伪造账本批准。

WorkBuddy 的“Skill”配置和本仓库用于 Agent 平台的 SkillRegistry 不是同一个运行时。导入结果须以 WorkBuddy 客户端实际解析的 Frontmatter、Skill 启用状态和工具调用记录为准。WorkBuddy 官方文档提示，Skill 可能访问授权信息或向第三方传递输入；验证前审阅 Skill 文件、MCP 数据根目录和 WorkBuddy 数据处理策略。

## 8. 故障排查

| 现象 | 检查项 |
| --- | --- |
| MCP 服务无法启动 | `command` 是否为绝对 Python 路径；该环境是否安装 `.[mcp]`（或 `credit-risk-agent-core[mcp]`）；`cwd`、`args` 和三个根目录是否存在 |
| 能启动但报 `No module named credit_agent` | 检查生成包的 `mcp.json` 是否带 `env.PYTHONPATH` 指向项目 `src/`，或目标 Python 是否已 `pip install -e '.[mcp]'` |
| 能启动但没有工具 | 查看 WorkBuddy MCP 日志；确认连接已启用；检查 MCP 子进程 stderr 和超时设置 |
| 数据列表为空 | 核对 `--data-root` 指向正确目录、文件扩展名为 CSV/Parquet、WorkBuddy 进程有读取权限 |
| 口径无法绑定 | 确认登记文件在 `--registry-root` 内，扩展名为 JSON/YAML/YML，且含 `metrics` 与 `dimensions` |
| 返回 `ADMISSION_REQUIRED` | 先执行快照、成熟度和已签收指标绑定；不能通过手写准入记录跳过 |
| 返回 `LABEL_IMMATURE` | 用户需选择已签收的短期口径、无标签描述分析或等待；不要把当前未逾期样本视为成熟好样本 |
| 返回 `RANDOM_SPLIT_FORBIDDEN` / `needs_human` | 确认时间列格式和成熟批次数；标准 OOT 不可用时展示选项并等待确认闭环 |
| 评分卡只有基线 | 查看是否为 `degraded`、`unavailable_algorithms` 是否包含 LightGBM，以及运行环境是否有 OpenMP |
| 启动时报「互不相交」 | 三个根目录配置了包含关系或相同路径；改成两两独立的目录，账本根不得放在数据根内 |
| `RESOURCE_UNAVAILABLE` | 文件不在授权根内、后缀不符、超过大小上限、符号链接越界，或口径文件含锚点/别名 |
| `PERIOD_MISMATCH` | 基期/对比期为空或与数据期次不匹配；显式传入已登记的期次列表，工具不会静默换期 |
| `DIMENSION_UNREGISTERED` / `TIME_POINT_VIOLATION` | 规则挖掘或下钻使用了未登记的维度，或使用了观察时点/贷后字段；只传决策时点登记列 |
| `SIGNOFF_REQUIRED` | 报告正文写了「已批准/建议上线/已立项」但审批卡未签收；先用 `scripts/host_approval.py` 落账，或改成待确认措辞 |
| `FEATURE_REVIEW_REQUIRED` | 缺少审批卡确认的入模特征清单；确认单一次性消费，重复训练需重新申请 |

## 9. 验收记录

每轮验证至少记录 WorkBuddy 客户端版本（应 ≥ 4.24.0）、操作系统、MCP 配置版本、四个 Skill 的导入/启用状态、工具发现结果（11 个）、样本文件及校验和、调用工具与状态/阻断码、用户选择与审批卡 `review_id`、`signoff_status`、`observation_mode` 与 `latest_review_id`（若走最新可观察样本）、账本 `run_id`、失败日志和最终结论。

开发侧的测试结果与公开样本边界见[SKILL、MCP 与公开数据功能复核](./skill-mcp-validation-2026-11-11.md)。它不能代替 WorkBuddy 客户端中的实际导入和交互验收。
