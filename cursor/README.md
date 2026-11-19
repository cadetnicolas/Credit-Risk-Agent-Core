# Cursor 信贷分析插件（v0.7.0）

这是 `credit-risk-agent-core` 的 Cursor 适配器。Python 业务代码与 SKILL 源文件以项目根目录为唯一来源；生成脚本把它们组织成 Cursor 本地插件目录或 ZIP，不维护第二份代码副本。当前版本 **0.7.0**（`pyproject.toml`、`src/credit_agent/__init__.py`、`cursor/plugin.json` 三处一致）。

Cursor 认的是插件目录，不是 WorkBuddy 的连接器 ZIP。产物布局：

```text
<output>/
├── .cursor-plugin/plugin.json
├── mcp.json
├── assets/logo.svg
├── README.md
└── skills/<name>/SKILL.md
```

`skills/` 与根目录 `mcp.json` 按 Cursor 的默认发现规则加载，清单里不再另写组件路径。

## 前置条件

- Python >= 3.11（构建脚本使用标准库 `tomllib`）。
- 目标解释器里可用 `credit_agent`、`mcp`、`yaml`：推荐 `pip install -e '.[dev]'`；最小运行面为 `pip install -e '.[mcp]'`，或依赖生成的 `PYTHONPATH`。
- 已在**源码检出目录**（`cd credit_risk_agent`）。
- `--data-root` / `--registry-root` 必须是已存在的目录；`--ledger-root` 由宿主提供。三个目录两两不相交。

## 构建（必须在源码检出目录）

```bash
python cursor/build_connector.py \
  --python /absolute/path/to/venv/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-cursor
```

脚本会：用 `tomllib` 从 `pyproject.toml` 读取版本并注入生成的 `plugin.json`；生成只含 `command` / `args` / `cwd` / `env.PYTHONPATH` 的 `mcp.json`（不写 WorkBuddy 的 `type` 与 `timeout`）；复制图标、README 和六个 SKILL。`--output` 必须位于项目目录之外。目录与 ZIP 任一已存在即报错，不留半成品。

装进本机 Cursor：

```bash
python cursor/build_connector.py \
  --python /absolute/path/to/venv/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-cursor \
  --install
```

`--install` 把构建结果**复制**到 `~/.cursor/plugins/local/credit-risk-analysis`。Cursor 不加载指向该目录之外的符号链接，所以这里不做链接。目标已存在时加 `--replace`。复制完成后执行 **Developer: Reload Window**。企业版若关闭了本地插件导入，这个目录不会被加载，需要管理员打开 Allow Local Plugin Imports。

若 `~/.cursor/mcp.json` 里已经有同名 `credit-risk-analysis`，不要再安装这份插件，否则会同时出现用户级服务和 `plugin-credit-risk-analysis-credit-risk-analysis`。两套只留一套。

## 安装或 PYTHONPATH 二选一

生成的 `mcp.json` 写入：

```json
"env": { "PYTHONPATH": "<项目根>/src" }
```

同时保留 `cwd` 与三个 root 参数。`pip install -e .` 与这份 `PYTHONPATH` 满足其一即可。构建前脚本会用 `--python` 指向的解释器执行 `import credit_agent, mcp, yaml`。

## 构建与运行必须同一台机器

`command`、`cwd`、`PYTHONPATH` 和三个 root 都是构建机上的绝对路径。插件必须在最终运行它的那台机器上构建，不能把 A 机的包拷到 B 机。Windows 请传入虚拟环境的 `python.exe`。

## 自检

```bash
PYTHONPATH=src python scripts/verify_cursor_install.py --skip-handshake
```

默认检查 `~/.cursor/plugins/local/credit-risk-analysis`：清单、六个 SKILL、三个根目录互不相交，以及一次 stdio 握手（16 个工具，且不暴露 `record_human_decision`）。

## wheel 与源码检出的区别

`pip install`（wheel）会把 `cursor/` 通过 `[tool.setuptools.data-files]` 放到 `share/credit-risk-agent/cursor`，**不会**还原成项目根下可直接构建的布局。`build_connector.py` 以自身所在目录的上一级作为项目根。因此**构建插件必须在源码检出目录执行**。
