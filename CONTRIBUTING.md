# 贡献指南

这个项目的核心资产是**约束**——门禁、码表、规程。改计算逻辑不难，难的是别让约束悄悄失效。所以下面对「改什么、要同步改哪里」写得比较具体。

## 环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[mcp,dev]'
pytest -q
```

`dev` 组已包含 `mcp`、`PyYAML`、`matplotlib`、`duckdb`、`pytest`。`lightgbm` 是可选的，不装也能跑全量测试（相关用例会跳过或走降级路径）。

## 提交前

```bash
pytest -q                       # 全绿
ruff check src tests scripts    # 若无 ruff 可跳过，CI 只跑 pytest
```

另外自查三件容易被忽略的事：

- 没有把 `.env`、数据文件、账本目录带进提交（`.gitignore` 已覆盖，但 `git status` 仍值得看一眼）；
- 新增的公开函数有 docstring，说明它返回的 envelope 在什么情况下是 `blocked`；
- 改动了 SKILL 里的阈值或步骤时，README / references 里对应的数字也一起改了——同一份方法论散落在多处是常见回归源。

## 改动分类与必须同步的位置

### 改业务口径、门禁语义、状态码

必须同时改三处，否则 `pytest` 会红：

| 位置 | 作用 |
| --- | --- |
| `src/credit_agent/tools/codes.py` | 码表与工具名的单一来源 |
| `skills/<相关>/SKILL.md` | 停机表与步骤里的判断依据 |
| `tests/test_release_contract.py` | 校验上述两者与 MCP 注册集合一致 |

`mcp_server` 在启动时还会自检「注册的工具 == `MCP_TOOL_NAMES`」，漏登记会直接启动失败——这是故意的，比运行到一半才发现好。

### 新增一个 MCP 工具

1. 在 `tools/` 下实现纯函数，返回 `envelope(...)`，不要抛裸异常；
2. 在 `tools/__init__.py` 的 `__all__` 里导出；
3. 在 `codes.py` 的 `MCP_TOOL_NAMES` 登记；
4. 在 `mcp_server.py` 用 `@server.tool(name=...)` + `@_guarded(...)` 注册，参数做类型与范围校验；
5. 更新相关 SKILL 的 `allowed-tools`；
6. 补测试：正常路径、`blocked` 路径、以及「未准入就调用」的路径。

如果新工具会产出可被报告引用的数字，它必须把摘要写进 `runs` 表，否则 `verify_and_commit_report` 无法核验引用。

### 新增一个 SKILL

- 目录 `skills/<name>/`，`SKILL.md` frontmatter 需含 `name`、`description`、`license`、`allowed-tools`、`metadata.version`；
- 方法论细节放 `references/`，`SKILL.md` 保持可执行步骤；
- 在 `pyproject.toml` 的 `[tool.setuptools.data-files]` 加两行（`SKILL.md` 与 `references/*.md`），否则 wheel 装完没有规程文件；
- 若新增 SKILL 引入新的工具依赖，同步改 `allowed-tools` 与 `MCP_TOOL_NAMES`。

### 新增数据后端

见 [docs/architecture.md](docs/architecture.md#六扩展点)。要点：继承 `DataSource`、注册到 `build_data_source` 与 `DRIVER_PACKAGES`、声明环境变量、加一个 optional-dependencies 组。

## 代码约定

- Python ≥ 3.11，行宽 120（`ruff` 配置在 `pyproject.toml`）；
- 计算层（`analytics/`、`modeling/`）**不得** import `mcp`、`yaml` 或任何协议相关模块；
- 路径参数一律经 `Path.is_relative_to` 校验，不接受绝对路径与 `..`；
- 日志与错误信息用中文写清楚「缺什么、怎么补」，不要只说「失败」；
- 浮点比较用相对容差，不要把 `==` 用在比率上；报告核验那边已经是 6 位小数精度对齐。

## 不要做的事

- 不要把审批能力暴露成工具（`record_human_decision` 必须留在宿主侧）；
- 不要为了「让测试过」放宽门禁——先确认门禁本身是否设计错了，再改，并在 PR 里说明理由；
- 不要提交任何真实业务数据、凭证、账本快照；
- 不要在计算层里写死平台名（WorkBuddy 及任何客户端都不该出现在 `src/` 的计算逻辑里，`workbuddy/` 目录是例外，它就是适配层）。

## 提交与 PR

- 一个 PR 做一件事。门禁语义变更与功能新增不要混在一起，否则 review 时无法判断红线有没有被顺手改掉；
- PR 描述里写清：改了什么、为什么、怎么验证的（贴 `pytest` 结果或 `demo_end_to_end.py` 输出）；
- 涉及业务口径的改动，附上参考依据（方法论出处、监管要求或内部签收文档的公开名称，不要贴内部文档原文）。

## 报告问题

提 issue 时请附：

- 操作系统与 Python 版本；
- 目标平台与客户端版本（如果不是用 `demo_end_to_end.py` 复现的）；
- `python scripts/demo_end_to_end.py --direct` 的输出（截图或文本都可以）；
- 如果涉及数据，说明用的是公开数据还是自造数据，**不要**附真实业务数据。
