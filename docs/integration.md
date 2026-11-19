# 跨平台接入指南

本项目的接入面只有一个：**标准 stdio MCP 服务**（外加一个可直接导入的 Python 库）。所以换平台不需要改代码，只需要把「怎么启动一个进程」这件事按目标平台的格式描述清楚。

本文按「先决策、再配置、后验证」组织。只想快速跑通的话，跳到 [六步验证清单](#六步验证清单)。

---

## 一、配置之前先定三件事

这三件事决定了配置文件长什么样，先想清楚再动手。

### 1. 谁拥有三个根目录

服务需要三个**互不相交**的绝对路径：

| 根目录 | 放什么 | 权限要求 |
| --- | --- | --- |
| `--data-root` | 只读数据集（CSV / Parquet） | 宿主可读；单文件 ≤ 100 MiB |
| `--registry-root` | 已签收的业务口径登记（JSON / YAML） | 宿主可读；≤ 1 MiB |
| `--ledger-root` | 追加式分析账本（SQLite + 明细 frames + artifacts） | 服务可写；目录 0700、文件 0600 |

**不要**把账本根放进数据根。账本里会落 `frames/*.parquet` 快照明细，如果它在数据根内，`list_credit_datasets` 会把这些明细当成新数据集列出来——准入与成熟度闸门就被绕过了。服务启动时会检测并拒绝相交配置，但更省事的是从一开始就分开。

### 2. 工具怎么暴露给模型

| 平台能力 | 走哪条路 |
| --- | --- |
| 支持 stdio MCP | 方式 A：写 JSON 配置，零代码 |
| 不支持 MCP，但能注册 Python 函数 | 方式 B：`import credit_agent.tools` 包装 |
| 想直接复用一份完整适配器 | 方式 C：参考 `workbuddy/`、`cursor/` 或 `codex/` |

### 3. 审批卡归谁管

有六个节点需要**真人**决策：`validation_fallback`、`maturity_choice`、`feature_list`、`policy_trial`、`anomaly_disposition`、`model_signoff`。

工具遇到这些节点会返回 `needs_human` 与一个 `review_id`，然后把决定权交回宿主。宿主侧需要做两件事：

1. 把审批卡呈现给人（对话卡片、工单、内部审批系统都行）；
2. 人确认后，由**宿主进程**（不是 Agent）把决定写进同一个账本。

`scripts/host_approval.py` 是一个可直接用的参考实现：

```bash
python scripts/host_approval.py \
  --ledger-root /absolute/credit-tool-ledger \
  --gate feature_list --review-id run_xxxxxxxxxxxx \
  --actor risk_owner --features feat_a,feat_b
```

约定：`review_id` 必须等于对应工具返回的 `run_id` / `review_id`；`actor` 是真人标识；`channel` 固定 `approval_card`。特征清单与验证回退确认单是**一次性消费**，不可重放。

如果宿主暂时没有审批 UI，可以让工具返回 `needs_human` 后停下——这是预期行为，不是故障。**不要让模型自己写审批卡**。

---

## 二、方式 A：标准 MCP 客户端

### 通用配置骨架

```json
{
  "mcpServers": {
    "credit-risk-analysis": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": [
        "-m", "credit_agent.mcp_server",
        "--data-root", "/absolute/path/to/datasets",
        "--registry-root", "/absolute/path/to/registries",
        "--ledger-root", "/absolute/path/to/ledger"
      ],
      "env": {
        "PYTHONPATH": "/absolute/path/to/credit_risk_agent/src"
      }
    }
  }
}
```

三个要点：

- **`command` 用绝对路径**。不要依赖 `PATH` 里的 `python`——客户端启动的服务进程往往不是你 shell 里的那个解释器，踩到系统 Python 就会缺依赖。
- **`PYTHONPATH` 与 `pip install -e .` 二选一**。源码检出但没装包时必须有 `PYTHONPATH`；已经 `pip install -e .` 到那个 venv 就可以省掉。
- **不要用 `python -m credit_agent` 之外的花哨启动方式**。`credit-risk-mcp` 这个 console script 等价，但如果客户端不继承 shell PATH，绝对路径更稳。

### 各客户端的落点

配置文件位置随版本演进，以各客户端官方文档为准；常见位置如下：

| 客户端 | 典型位置 |
| --- | --- |
| Claude Desktop | macOS `~/Library/Application Support/Claude/claude_desktop_config.json`；Windows `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | 全局 `~/.cursor/mcp.json`，或项目内 `.cursor/mcp.json` |
| Cline（VS Code） | 扩展设置里的 MCP Servers 面板，落地为 `cline_mcp_settings.json` |
| Continue | `config.yaml` 的 `mcpServers` 段 |
| 自研 / 内部平台 | 只要用官方 MCP SDK 拉起 stdio 子进程即可，字段就是 `command` / `args` / `env` |

### 字段差异（踩坑集中区）

| 字段 | 说明 |
| --- | --- |
| `cwd` | 部分客户端支持，部分忽略。**不要依赖它**：全部路径写成绝对路径，`PYTHONPATH` 显式给出 |
| `timeout` | 若客户端提供，注意它通常是**连接/握手超时**，不是单次工具执行超时。训练与网格搜索类工具可能跑几分钟 |
| `disabled` / `enabled` | 各客户端命名不一，改完配置记得确认服务处于启用态 |
| `env` | 多数客户端会**替换**而非合并父进程环境。若需要代理或数据库凭证，全部显式写进去 |

### Windows

```json
{
  "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
  "env": { "PYTHONPATH": "C:\\path\\to\\credit_risk_agent\\src" }
}
```

JSON 里反斜杠要写成 `\\`。若平台支持多个 `PYTHONPATH` 条目，Windows 用 `;` 分隔，POSIX 用 `:`。

---

## 三、方式 B：平台原生工具注册

平台没有 MCP、但能注册 Python 函数时，把库函数包一层即可。门禁、envelope、账本逻辑全部复用，只是不走协议。

关键是**由宿主创建并持有 `Ledger`**，然后注入给每个工具函数。注意分层：`credit_agent.tools` 下的函数吃的是内存对象（DataFrame / run_id），文件读取与路径校验在 `mcp_server.py` 里——走原生注册时，读文件这段由你负责。

```python
from pathlib import Path

import pandas as pd

from credit_agent.tools import Ledger, inspect_and_bind_snapshot, verify_and_commit_report

LEDGER = Ledger(Path("/absolute/path/to/ledger"))
DATA_ROOT = Path("/absolute/path/to/datasets")

# 平台原生工具定义（伪代码，按目标平台 SDK 调整）
def bind_snapshot(dataset: str, grain: str, key_columns: list[str],
                  decision_time_column: str) -> dict:
    # 文件名限制在数据根内，拒绝绝对路径与 .. —— 这段校验 MCP 层做了，
    # 原生接入需要自己补上，否则模型给出的任意路径都会被读取。
    frame = pd.read_csv(DATA_ROOT / dataset)
    return inspect_and_bind_snapshot(
        frame,
        ledger=LEDGER,
        grain=grain,
        key_columns=key_columns,
        decision_time_column=decision_time_column,
    )

def commit_report(run_ids: list[str], report_markdown: str, citations: list[dict]) -> dict:
    return verify_and_commit_report(
        ledger=LEDGER,
        run_ids=run_ids,
        report_markdown=report_markdown,
        citations=citations,
    )
```

后续工具（`prepare_modeling_sample`、`screen_features`、`train_and_evaluate_candidate`、`run_portfolio_monitoring` 等）都以上一步返回的 `run_id` / `snapshot_id` 为输入，明细留在账本里，调用方只传 id，因此原生注册时不需要自己管理中间态。

两点必须遵守：

1. **`Ledger` 只能有一个实例、一个进程**。多进程并发写同一账本会破坏追加语义；需要并发就一租户一进程、各自独立账本根。
2. **不要把 `record_human_decision` 暴露成工具**。它的存在是为了让宿主写审批卡，暴露给模型就等于把门禁交还给模型。

工具函数的返回结构与 MCP 工具的返回完全一致，SKILL 里的判断逻辑可以原样复用：

```json
{
  "ok": true,
  "tool": "run_portfolio_monitoring",
  "status": "ok",
  "block_code": null,
  "snapshot_id": "snap_…",
  "run_id": "run_…",
  "metric_id": "bad_rate",
  "metric_version": "v1",
  "summary": {},
  "artifacts": [],
  "allowed_conclusions": ["mix_shift"],
  "human_gate": null,
  "details": {}
}
```

`status` 四态：`ok` / `degraded` / `needs_human` / `blocked`。**`blocked` 时 `summary` 只剩 `message`**，不携带任何未经核验的数字；诊断信息在 `details` 里。

### 报告核验失败时怎么判因

`verify_and_commit_report` 所有失败共用 `REPORT_UNTRACED` 一个码（宿主按码路由），具体原因在 `details.reason`：

| `details.reason` | 含义 | 关键字段 |
| --- | --- | --- |
| `no_runs_referenced` | `run_ids` 为空，数字没有追溯对象 | — |
| `empty_report` | 正文为空，没有可核验内容 | — |
| `run_not_found` | 引用的 run 不在账本里 | `missing_run_ids`、`known_run_ids` |
| `citation_malformed` | 引用缺 `path` / `rendered` | `citation_index` |
| `path_not_found` | 路径在该 run 的 summary 里不存在 | `path`、`available_paths` |
| `value_mismatch` | 引用值和产物不符 | `path`、`rendered`、`artifact_values`、`expected`（按同精度渲染的正确写法） |
| `uncited_number` | 正文里的数字没有对应引用 | `fragment`、`context` |
| `signoff_pending` | 写了上线/批准措辞但缺人工签收（码为 `SIGNOFF_REQUIRED`） | `gates` |

`details.failures` 会一次列出最多 10 条（`details.failure_count` 是总数，可能大于 10）。`details.checked` 固定为四个键：`runs`（传入的运行数）、`runs_resolved`（账本能读到的）、`citations`（传入的引用条数）、`numbers`（正文里需要核验的数字个数）。引用可选用 `run_id` 绑定到某一次运行，避免同一 `path` 在多次运行间「任一命中即通过」。

正文扫描会豁免日期、时长与阈值：`2026 年 3 月`、`9 月`、`12 个月`、`第 3 个月`、`MOB 3`、`≥10 万` 里的数字不要求引用；**其余数字必须来自某条引用，包括紧贴汉字的写法（`共400笔`、`坏账率3.45%`）**。`snap_abc123`、`v1.2.3` 这类标识符里的数字不参与扫描。

正文全是豁免标签时（如只有「9 月复核」），返回 `ok` 但 `summary.verification_scope = "no_figures"`，表示这次核验没有实际数字，不要和「核过 N 个数字」混为一谈。


---

## 四、方式 C：参考现成适配器

仓库里有两份适配器，结构相同：元数据、构建脚本、图标。业务代码和 SKILL 仍只在项目根维护一份。

`workbuddy/` 打成 WorkBuddy Connector：

- `connector-meta.json` — 连接器元数据（名称、描述、示例、最低版本）；
- `build_connector.py` — 打包脚本：读 `pyproject.toml` 版本、生成 `mcp.json`、复制 SKILL 与图标、打包 ZIP、做导入预检与产物断言；
- `icon.svg` — 图标。

`cursor/` 打成 Cursor 本地插件：

- `plugin.json` — 插件清单（名称、版本、说明、图标相对路径）；
- `build_connector.py` — 打包脚本：注入版本、生成 Cursor 的 `mcp.json`、复制六个 SKILL，可选安装到 `~/.cursor/plugins/local/credit-risk-analysis`；
- `assets/logo.svg` — 图标。

`codex/` 生成 Codex 本地插件市场，包含 `.codex-plugin/plugin.json`、`.mcp.json` 与项目 Skill。用 Codex CLI 添加市场并安装，参见 [Codex 接入与打包](../codex/README.md)。

构建细节见 [WorkBuddy 接入与打包](workbuddy-skill-mcp-integration-guide.md) 与 [Cursor 接入与打包](cursor-skill-mcp-integration-guide.md)。

注意：适配器生成物里固化的是**构建机**的绝对路径，所以必须在最终运行它的那台机器上构建，不能跨机器拷贝。

---

## 五、六步验证清单

按顺序做，任何一步失败都别往下走。

**1. 服务能起来**

```bash
/absolute/path/to/.venv/bin/python -m credit_agent.mcp_server \
  --data-root ... --registry-root ... --ledger-root ...
```

进程正常阻塞等待 stdin 即成功。报 `FileNotFoundError` 说明根目录不存在；报「根目录相交」说明三个路径有嵌套。

**2. 不经客户端，直接跑端到端**

```bash
python scripts/make_demo_workspace.py
python scripts/demo_end_to_end.py --direct
```

这一步验证的是本仓库自身，与任何平台无关。它会在本地拉起服务、走完「列数据集 → 绑定快照 → 成熟度 → 口径 → 监控 → 归因」并打印每个 envelope。

**3. 用你的客户端配置再跑一次**

```bash
python scripts/demo_end_to_end.py --config /path/to/your/mcp.json
```

这会读取你实际写进客户端的配置（`command` / `args` / `env`）并握手，等于提前验证客户端能不能起来。指定服务名用 `--server your-name`。

**4. 客户端里确认工具数量**

让客户端列出工具，应该是 **16 个**。少一个通常是 `args` 写错或版本旧；多出来则说明连到了别的服务。

**5. 跑一次真实链路**

在客户端里说：「用 demo 数据做一次存量监控，基期 2025-10/11，对比期 2025-12」。

预期：模型调用 `list_credit_datasets` → `inspect_and_bind_snapshot` → `check_label_maturity` → `resolve_metric_and_dimensions` → `run_portfolio_monitoring` → `decompose_and_locate_anomaly`，并报告未成熟批次。

**6. 验证门禁真的会拦人**

让它「直接训练一个模型，不要做准入体检」。正确行为是收到 `ADMISSION_REQUIRED` 并停下来解释，而不是硬跑。

最容易被忽略的是**第 6 步**——门禁失效时工具照样出数，看起来一切正常。

---

## 六、常见问题

**工具数不是 16**

- 客户端没有重新加载配置（多数需要重启或手动 Reload MCP）；
- `args` 里 `--data-root` 等参数写错，进程启动即退出；
- 连到了旧版本的服务进程。

**`list_credit_datasets` 返回空**

数据根下没有 `.csv` / `.parquet`，或文件超过 100 MiB，或是符号链接。相对路径是相对于 `--data-root` 解析的。

**`download_sample` 返回 `BACKEND_UNAVAILABLE`**

数仓凭证没配。这不是重试能解决的问题。让使用者在**他自己的终端**运行 `credit-risk-config`（或 `python scripts/config_setup.py`），在浏览器里填完保存，然后重启服务。凭证只有 `download_sample` 需要；`import_sample_file` 与其余工具完全离线可用。

**`needs_human` 之后卡住**

先看信封里有没有 `review_id`，这两类 `needs_human` 的出路完全不同：

- **有 `review_id`**（模型训练、策略模拟等）：宿主还没写审批卡。这是设计行为。用 `scripts/host_approval.py` 或自己的审批系统落账后，把 `review_id` 传回对应工具。
- **没有 `review_id`，只有 `human_gate`**（`describe_population` / `compare_population_distribution`）：这不是审批，是**口径没填完**。`human_gate.kind` 告诉你缺哪一项：
  - `field_semantics`：某个用到的列有缺失或零值，但 `spec.field_semantics` 没说它们算什么。补上 `{"列名": {"missing": ..., "zero": ...}}` 重跑。
  - `binary_column_role`：某个 0/1 列没人声明它是特征还是表现结果。在**绑定快照**时用 `column_roles` 声明（`feature` / `observation_outcome`），再重跑描述；声明已落地的 run 不能改，两期都要重新描述才能比较。

这两类都不落 run、不出数字，所以卡住的时候账本里没有半份结果，补完直接重跑即可。

**训练返回 `degraded`**

多半是 LightGBM 缺原生 OpenMP。此时候选对比不完整，不能当作双模型比较结论。装好 OpenMP 或只用逻辑回归评分卡路径。

**`REPORT_UNTRACED`**

报告正文里的数字与工具摘要对不上。改数字或改写法，重新提交；不要绕过核验。

**MCP 服务能起但调用超时**

部分客户端把 `timeout` 当连接超时。训练与网格搜索可能跑几分钟，确认客户端没有在握手阶段就判死。

---

## 七、多租户与生产部署

- **一租户一进程**：三个根目录各自独立，不要多个租户共享账本；
- **账本只追加**：不要手工编辑 `ledger.sqlite`，它是审批证据；
- **不要把数据或凭证打进镜像/仓库**：`.env` 已在 `.gitignore` 中，打包时确认没被带进去；
- **服务不开放网络端口**：只走 stdio。需要远程接入时，由宿主侧做进程隔离与转发，不要在服务里加 HTTP 监听。

---

## 八、平台适配的边界

本项目承诺的是**协议级兼容**：标准 stdio MCP、标准 envelope 返回、标准 SKILL Markdown。

仓库交付的完整适配器是 WorkBuddy 与 Cursor。其它 MCP 客户端按标准对接，本仓库提供验证脚本（第 2、3 步）而不是逐客户端的桌面端认证。如果你在某个平台上跑通了，欢迎在 issue 里补一条记录：客户端名 + 版本 + 使用的配置片段。
