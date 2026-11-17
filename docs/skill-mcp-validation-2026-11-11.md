# SKILL、MCP 与公开数据功能复核

> 状态：第 1–4 节是 v0.2（修复前）的复核记录，保留作为历史证据；v0.3 发布前修复与重新复核见第 5 节。第 1 节的「142 passed」与第 4 节的「宿主审批闭环仍需接线」描述的是修复前状态。
>
> **本文是 2026-11-11 当天的快照，其中的「四个 SKILL」「11 个工具」是当时的规模。**现行规模是六个 SKILL、16 个工具（v0.7，新增 `describe_population` / `compare_population_distribution` 与 `population-distribution-analysis` 规程）。要看现状请以 `README.md` 与 `scripts/verify_workbuddy_install.py` 为准，不要引本文的数量。

日期：2026-11-11。范围：`credit_risk_agent/skills`、`credit_agent.tools`、独立 stdio MCP 服务、信用卡欺诈与 FICO HELOC 本地文件。以下是开发与功能验证结果，不是模型或策略的业务准出意见。

## 1. 接口与实现

| 范围 | 验证结果 | 证据 |
| --- | --- | --- |
| 四个 SKILL | `SkillRegistry` 全部加载成功；声明的工具名和引用文件均存在 | `test_mcp_server.py`、平台注册表加载检查 |
| MCP 服务 | 补齐 `credit_agent.mcp_server`；11 个工具可经 stdio 发现，包含 10 个 SKILL 工具与 `list_credit_datasets` | 真实 MCP 客户端的发现和调用测试；平台 `MultiServerMCPClient` 发现 11 个工具 |
| 数据边界 | CSV/Parquet 限于宿主设定的 `data_root`；JSON/YAML 登记文件限于 `registry_root`；`../` 与越界符号链接被拒绝 | `test_data_root_cannot_be_escaped` |
| 口径文件 | 修复 SKILL 写 YAML、服务只读 JSON 的不一致；YAML 使用 `safe_load`，同一文件须有 `metrics` 和 `dimensions` | stdio 调用 `resolve_metric_and_dimensions` 绑定 YAML 测试 |
| 人工审批 | `record_human_decision` 仅在受信任宿主 API，不作为 MCP 工具发布；聊天文本和非审批卡记录不能放行特征训练 | `test_tool_modeling.py`、`test_mcp_server.py` |
| 可选模型 | OpenMP 可用时完成逻辑回归与 LightGBM 双模型比较；不可用时保留逻辑回归基线，并返回 `degraded`、不可用算法和比较未完成状态 | 两种运行环境下的 `test_tool_modeling.py`，以及 `test_optional_model_dependency.py` |
| 模型产物 | 补齐基线 `scorecard.json` 与 `model_validation.json`，含 PDO 分箱点值、时间切分、开发/OOT 指标与十分位表；数字可经报告引证工具核对 | `test_tool_modeling.py` 的产物和 `validation.baseline_scorecard.base_points` 引证检查 |

MCP 接入目前验证到服务发现、工具调用、路径限制和平台客户端兼容性。**宿主审批卡到 `record_human_decision` 的实际接线仍需在部署的宿主完成**；否则模型训练在 `FEATURE_REVIEW_REQUIRED` 停止。生产租户还需分别配置授权目录、账本目录、工具发布与权限。

## 2. 自动化验证

在仓库根目录运行：

```bash
# 在仓库上层目录执行；开发/测试依赖：pip install -e '.[mcp,dev]'
PYTHONPATH=credit_risk_agent/src .venv/bin/python -m pytest -q credit_risk_agent/tests
PYTHONPATH=credit_risk_agent/src .venv/bin/python \
  credit_risk_agent/scripts/verify_public_datasets.py \
  --fraud /path/to/creditcard.csv \
  --heloc /path/to/heloc_dataset_v1.csv
```

本次结果为 **142 passed**。同一套测试在本机缺少 OpenMP 的默认环境与预加载 OpenMP 的环境中均通过；后者实际训练了 LightGBM 候选模型，并断言模型比较包含 `lightgbm`。本机使用 `DYLD_INSERT_LIBRARIES=/opt/miniconda3/envs/dm/lib/libomp.dylib` 预加载另一环境已有的库，不修改项目依赖或系统目录。测试覆盖快照、表现期、口径、监控与异常归因、策略规则与阈值、特征筛选、人工审批闸门、候选训练、报告引证，以及 MCP stdio 协议。带日期的合成面板贯通准入→监控→归因→建模→审批→训练→引证提交；单独测试覆盖规则挖掘的开发/OOT 方向一致性、经用户选择的随机留出与仅交叉验证路径，以及阈值换挡的已观察样本口径。

## 2.1 标准条件不足时的用户选择

`check_label_maturity` 在目标表现期没有成熟样本时返回 `needs_human` 与三项选择；账本准入仍为阻断，不生成目标坏账率。`prepare_modeling_sample` 在成熟样本无法形成 OOT 时返回 `validation_fallback` 确认单；只有受信任的用户选择落账后，才允许随机留出或仅交叉验证。`simulate_policy_rules` 的规则挖掘同样优先 OOT，回退后只输出 `candidates`，不输出 `recommended`。这些路径保留选择记录与 `degraded` 标识，测试覆盖未确认不得继续。

## 3. 两份公开数据的实际结果

| 数据 | 文件 SHA-256 | 实测 |
| --- | --- | --- |
| ULB 信用卡欺诈 `creditcard.csv` | `33a178be65174943a07d1736acf5898f6c8e1fa80950dbbe598c17eda968ec59` | 按 `Time` 稳定排序后每 8 行取 1 行，共 35,601 行、64 笔欺诈；`V14/V4/V12` 独立评分卡按交易时间 OOT 切分，留出 AUC 0.974513、KS 0.822966 |
| FICO HELOC `heloc_dataset_v1.csv` | `e5a914f742ad7f8472c5417902ec06b321ebc5de1d68ec13f136d62c87a7fbf3` | 过滤外部风险分的负数特殊码后 9,861 行；该挑战样本内坏标签率 52.0028%；5 个分档、20 个二维网格单元；70→60 的研究性放宽换入 3,118 行 |

欺诈数据的 `Time` 是相对首笔交易的秒数。脚本只为时间排序和 OOT 切分添加固定日历锚点；它不是信贷放款日期。该数据约两天跨度，三个月信贷表现期检查返回 `LABEL_IMMATURE`。FICO HELOC 没有决策日期，因此正式策略工具返回 `ADMISSION_REQUIRED`；分档、网格和换挡结果来自独立研究函数，不能作为时间外策略验证或预期收益。挑战样本的坏标签率不能外推为机构组合坏账率。

此前 [旧验证报告](archive/model-strategy-validation-report.md)（已归档并标记取代）的反欺诈抽样正例数和指标与本次不同。本次脚本采用稳定排序以固定相同 `Time` 值的行顺序，并提供可复运行命令；不要混用两次抽样指标。

## 4. 尚未由当前环境或数据证明的部分

1. **公开信贷策略的时间外规则复核**：FICO HELOC 没有决策日期，欺诈数据也没有信贷表现期。当前两份公开数据无法证明策略规则在真实放款批次的 OOT Lift 与成熟坏账率；合成测试证明代码路径和闸门行为，但不是业务有效性证据。需要有申请/放款日、观察截止日与表现标签的授权样本。
2. **真实宿主审批闭环**：服务刻意不允许模型直接写人工决定。需要宿主把已认证审批卡的决定写进相同 ledger，再验证特征、策略试验和模型签收的实际交互。
3. **部署环境依赖**：LightGBM 需要可用的原生 OpenMP 库；若运行环境缺少它，工具会明确降级，不能把基线结果当作完整的双模型比较。

因此，本次可以确认 **SKILL/MCP 接口可运行、双模型训练及降级路径已验证、基础策略与评分卡研究函数已在指定公开数据上运行、缺字段时的闸门生效**；真实带日期信贷策略回测仍需相应数据，宿主审批闭环仍需部署接线。

---

## 5. v0.3 发布前修复复核（2026-11-12）

### 5.1 业务口径决策：成熟度按场景分化

信贷建模按月份观察，策略分析尽可能拿最新样本测算。因此 P0-3 不是把实现改成逐笔 `T0 + W`，而是：

- **建模 / 存量监控**（`observation_mode="monthly_cohort"`，默认）：保留「放款批次月末 + W 个完整账龄月 ≤ 数据截止日」的 Vintage/MOB 口径；`decision_time_column` 用于逐笔校验 T0 与登记批次月份一致；FPD 类窗口按自然日 `T0 + days ≤ cutoff` 判定。未成熟批次不混入分母、不当作 Good。
- **策略分析**（`observation_mode="latest_observed"`）：`check_label_maturity` 生成含未成熟批次的「最新可观察样本」掩码，`simulate_policy_rules` 用它做描述性测算，返回 `degraded`，所有坏账率 / Lift / 正式推荐为空，明确不构成目标表现期坏账率、不可用于建模或上线收益测算。

### 5.2 修复范围（对照 [完备性复核](credit-skill-mcp-audit-2026-11-11.html)）

| 级别 | 修复内容 |
| --- | --- |
| P0 | 调入集坏账率固定为空并补齐 2×2 置换矩阵；规则挖掘强制登记的决策时点维度、绝对命中门槛与真实 trials；成熟度双模式与 T0 一致性；11 个工具异常统一转结构化 envelope；Connector 安装预检与 `env.PYTHONPATH`；审批 gate 消费与宿主适配器 `scripts/host_approval.py`；账本三根互不相交、id 白名单、0700/0600 权限、确认单一次性消费 |
| P1 | 17 个 block_code 由 `tools/codes.py` 单一定义并与 SKILL 对齐；`split.method` 等摘要契约、整数/日期 citation、签收状态与 `SIGNOFF_REQUIRED`；模式 C 盈亏平衡、三档候选与试验设计单、Swap Set 完整矩阵；登记维度分档/阈值逐维生效、期次不匹配不静默回退；Shift-Share 闭合残差校验；版本/元数据/依赖/wheel 资源/LICENSE 合规；历史文档归档与冲突报告标记 |
| P2 | MCP facade 与 11 工具 stdio 覆盖、打包脚本与发布契约测试、业务负例；mock 数仓回归；文件大小与 YAML 别名防护、日期与规则解析健壮性；打包器校验顺序与 ZIP 命名；SKILL 触发/反例与非交付面模块说明 |

### 5.3 复核命令

```bash
cd credit_risk_agent
PYTHONPATH=src ../.venv/bin/python -m pytest -q tests
PYTHONPATH=src ../.venv/bin/python scripts/verify_public_datasets.py \
  --fraud /path/to/creditcard.csv --heloc /path/to/heloc_dataset_v1.csv
python -m credit_agent.mcp_server --data-root <A> --registry-root <B> --ledger-root <C>
python workbuddy/build_connector.py --python <venv/bin/python> \
  --data-root <A> --registry-root <B> --ledger-root <C> --output <external-dir>
```

发布契约测试 `tests/test_release_contract.py` 校验：11 个工具名与单一常量源一致、SKILL 声明的码全部在码表内、三个根目录互不相交、异常返回 envelope、新参数已暴露、Connector 元数据与版本合规、业务红线的源码护栏。

### 5.4 最终验收记录（2026-11-12，v0.3 发布判定）

**结论：P0 / P1 / P2 缺陷已全部修复，达到可发布状态。** 下表为本机实测，不是代理自报结果。

| 验收项 | 命令 / 方法 | 结果 |
| --- | --- | --- |
| 全量回归 | `PYTHONPATH=src ../.venv/bin/python -m pytest -q tests` | **182 passed, 0 failed**（原 142 个用例全部保留通过） |
| 发布关键子集 | `pytest -q tests/test_generated_connector_stdio.py tests/test_build_connector.py tests/test_release_contract.py tests/test_mcp_facade_stdio.py tests/test_approval_loop.py` | **28 passed** |
| 生成包真实握手 | `test_generated_connector_stdio.py`：用生成包自身 `mcp.json` 启动 stdio | 发现 11 个工具，通过 |
| 真实打包 | `python workbuddy/build_connector.py --python … --data-root … --registry-root … --ledger-root … --output <项目外目录>` | 版本清单四处均 0.3.0；ZIP 12 个条目；4 个 `SKILL.md` + 4 份 references 齐全且为最新内容 |
| 静态契约复核 | 脚本核对工具名 / 码表 / 版本 / 链接 | 11 个工具与 4 份 SKILL 的 `allowed-tools` 对齐（并集为 10 个业务工具），`record_human_decision` 未暴露；17 个 `BLOCK_CODES`、6 个 `GATES`、4 个 `status` 值；`pyproject` / `__init__` / `connector-meta.json` 均 0.3.0；`minWorkbuddyVersion=4.24.0`；`description_en` 98 字符；现行交付面无 TODO 占位符、本地链接全部有效 |
| 资源分发 | `pyproject.toml` 的 `[tool.setuptools.data-files]` + `MANIFEST.in` | wheel/sdist 均带 SKILL 与 references，按目录分项声明规避 `SKILL.md` 同名覆盖 |

对照 `README.md` 发布前自检清单：全量测试、版本一致性、三根互不相交、生成包 stdio 握手、审批适配器接线（含一次性消费与 `SIGNOFF_REQUIRED` 负例）均已有自动化用例覆盖；剩余两项属部署/业务准出动作，见下。

**已记录的设计取舍（有意保留，非缺陷）**

- **成熟度按场景分化**：建模/监控默认 `monthly_cohort`（放款批次月末 + W 个完整账龄月），策略分析用 `latest_observed` 只做描述性测算并返回 `degraded`，目标表现期坏账率、Lift、正式推荐恒为空；FPD 按自然日 `T0 + days <= cutoff`。这是依据「信贷建模按月观察、策略尽可能拿最新样本」的业务口径，而非逐笔 `T0 + W`。
- **C3 视图降级边界**：vintage / migration 未配置或缺少绑定列时保持 `status=ok`，但写入明确的缺失列 note 并收窄 `allowed_conclusions`；只有「已尝试但结果不可比」或 `closure_error >= 1e-9` 才 `degraded`。`ok` 表示工具执行成功，不代表所有视图可用。
- **审批确认单消费时机**：先 `get_decision` 校验合法性、仅在成功训练时 `consume_decision`，避免无效审批尝试烧掉确认单；一次性消费与防重放仍成立。
- **账本与返回摘要同结构**：`screen_features` 落账与 envelope `summary` 使用同一 dict，避免「账本记的与 Agent 看到的结论不一致」；WOE 分箱明细随 `selected_details` 进入 Agent 可见 envelope，属聚合统计、无个人标识。
- **SKILL 停机表粒度**：每份 SKILL 只列本规程工具真实会返回的码，17 码权威单一源为 `src/credit_agent/tools/codes.py`，全局落点在 `README.md:51` 与接入指南 `:72`；不要求每份展开全部 17 码。

**残留限制 / 需人工完成的发布动作（不阻塞代码发布）**

1. `scripts/verify_public_datasets.py --fraud … --heloc …` 需要外部公开数据集，仓库内不含这两个文件，**本次环境未执行**，未伪造结果。README 已注明「公开数据脚本只能证明代码路径」，真实申请/放款日与表现标签的 OOT 复核仍需在授权样本上完成。
2. `docs/archive/` 内 8 处链接为归档前既有的历史稿互引，归档 README 已声明「不作为实现依据」；现行交付面（README、docs 顶层、workbuddy、skills）链接全部有效。
3. **git 仓库根位于父目录** `credit_data_analysis_and_auto_model_agent`，当前 `credit_risk_agent/` 整体为未跟踪状态（`?? ./`），父仓库另有旧 `credit/` 目录的删除记录。若走 git 发布流程，需由用户确认后 `git add` 并提交；本次未擅自暂存或提交。
4. `license = { text = "MIT" }` 在 setuptools >= 77 下会有一条 PEP 639 弃用告警（2027-02-18 前需迁移到 `license = "MIT"` + `license-files`）；构建成功且 METADATA 正确，保留表形式是为兼容 `setuptools>=68` 声明。
5. LightGBM 运行环境需具备 OpenMP，否则候选对比返回 `degraded`，不得当作完整双模型比较；此项属部署环境检查。
