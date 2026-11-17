# WorkBuddy 信贷分析连接器（v0.7.0）

这是 `credit-risk-agent-core` 的 WorkBuddy 发布/本地导入包构建目录。Python 业务代码与 SKILL 源文件以项目根目录为唯一来源；生成脚本把它们组织成 WorkBuddy Connector 目录或 ZIP，不维护第二份代码副本。当前版本 **0.7.0**（`pyproject.toml`、`src/credit_agent/__init__.py`、`workbuddy/connector-meta.json` 三处一致）。

## 前置条件

- Python >= 3.11（构建脚本使用标准库 `tomllib`）。
- 目标解释器里可用 `credit_agent`、`mcp`、`yaml`：推荐 `pip install -e '.[dev]'` 一次装齐（含 mcp、PyYAML、matplotlib、duckdb、pytest）；最小运行面为 `pip install -e '.[mcp]'`，或用 `PYTHONPATH` 直接指向源码 `src/`（见下）。
- 已在**源码检出目录**（`cd credit_risk_agent`），而不是只装了 wheel。
- `--data-root` / `--registry-root` 必须是已存在的目录；`--ledger-root` 由宿主提供。

## 构建（必须在源码检出目录）

```bash
python workbuddy/build_connector.py \
  --python /absolute/path/to/venv/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-connector
```

脚本会：用 `tomllib` 从 `pyproject.toml` 读取版本并注入生成的 `connector-meta.json`（单一来源，避免漂移）；生成含 `cwd` / `env.PYTHONPATH` / 三个 root 参数的 `mcp.json`；复制图标、README 和六个 SKILL（含 references）；**生成前**自检 `connector-meta.json`（版本、`minWorkbuddyVersion`、`description_en` 长度、`source` kebab-case、examples 数量）、校验目标解释器依赖与 `src` 目录；**生成后**断言 ZIP 条目清单；最后打印一行版本清单便于发布核对。

产物是目录 `<output>/` 与同名 ZIP `<output>.zip`（若输出名含点，ZIP 也用完整输出名，不会被 `with_suffix` 截断）。`output` 与 ZIP 在写入前会同时检查是否已存在，任一已存在即报错且不留半成品；构建先在临时目录完成再原子改名，异常时清理中间产物。生成包只包含连接配置与说明，不含任何数据或账本。`--output` 必须位于项目目录之外。

## 安装或 PYTHONPATH 二选一

包代码在 `src/` 下；仅设 `cwd` 而未安装时，`python -m credit_agent.mcp_server` 会因找不到 `credit_agent` 失败。生成的 `mcp.json` 因此在 server 配置里加入绝对路径：

```json
"env": { "PYTHONPATH": "<项目根>/src" }
```

同时保留 `cwd` 与 `--data-root/--registry-root/--ledger-root` 三个 root 参数。于是**安装或 PYTHONPATH 二选一即可**：要么 `pip install -e .` 把包装进该解释器，要么依赖 mcp.json 的 `PYTHONPATH` 指向源码 `src/`，无需两者同时满足。构建前脚本会用 `--python` 指向的解释器执行 `import credit_agent, mcp, yaml`（注入 `PYTHONPATH=src`）做预检，依赖缺失时给出明确错误。

## 为什么 `minWorkbuddyVersion` = 4.24.0

`connector-meta.json` 使用了 `name_zh`/`name_en`/`examples_zh`/`examples_en`（4.24.0 引入的版本化字段），`mcp.json` 使用了 `cwd`（4.22.15 起支持）。按官方 Connector 规范，用到这些字段时必须声明 `minWorkbuddyVersion`，且取所用特性中的最高版本，因此设为 **4.24.0**；低于该版本的 WorkBuddy 可能无法正确解析这些字段。`description_en` 也按规范压到 20–100 字符以内。

## 构建与运行必须同一台机器

`mcp.json` 中的 `command`（解释器绝对路径）、`cwd`（项目根绝对路径）、`env.PYTHONPATH`（`src` 绝对路径）以及三个 root 都是构建机上的绝对路径；stdio server 导入本地包、读取本地数据/账本都依赖该机文件系统。因此 Connector 必须在**最终运行它的那台机器**上构建，不能把 A 机生成的包拷到 B 机导入。Windows 请传入虚拟环境的 `python.exe`。

## 发布前自检

- 版本三处一致且为 0.7.0；构建输出的一行版本清单（pyproject / `__init__` / 模板 / 生成物）无漂移。
- `connector-meta.json`：`description_en` 20–100 字符、`examples_zh`/`examples_en` 各 2–5 条、`source` 为 kebab-case、含 `minWorkbuddyVersion=4.24.0`。
- 预检通过：目标解释器 + `src` 下 `import credit_agent, mcp, yaml` 成功。
- ZIP 内含 `connector-meta.json` / `mcp.json` / `icon.svg` / `README.md` / 6 个 `skills/<name>/SKILL.md` 及其 references。
- `mcp.json` 的 command/cwd/env/三个 root 指向本机真实路径，且构建与运行同机。
- 提交 Connector 市场前确认 `source` 全局唯一性、图标与文案，并审查本地 stdio 执行权限。

## wheel 与源码检出的区别

`pip install`（wheel）会把 `skills/` 与 `workbuddy/` 通过 `[tool.setuptools.data-files]` 放到 `share/credit-risk-agent/...`（sdist 由 `MANIFEST.in` 收录），**不会**还原成项目根下的 `skills/`、`workbuddy/` 布局。而 `build_connector.py` 以自身所在目录的上一级作为项目根，直接按相对路径读取 `workbuddy/connector-meta.json`、`workbuddy/icon.svg`、`skills/<name>/`，并把 `src/` 以绝对路径写进 `mcp.json`。因此**构建 Connector 必须在源码检出目录执行**：wheel 只用于分发 Python 包本身，不能装完 wheel 就构建 Connector。

连接器元数据中的 `source` 是内部示例标识。当前包用于本地验证，不代表已接入用户身份认证或账本审批闭环。
