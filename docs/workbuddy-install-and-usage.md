# 在 WorkBuddy 中安装与使用信贷风控 SKILL + MCP

本文档对应 `credit_risk_agent` v0.7.0，记录本机已完成的两项安装，以及日常使用、审批、排障和卸载方法。

> 与[接入指南](workbuddy-skill-mcp-integration-guide.md)的分工：接入指南面向**构建与打包 Connector**（客户端版本兼容、`mcp.json` 字段、ZIP 结构、发布自检）；本文面向**已装好之后的使用者**（SKILL 何时触发、数据目录怎么摆、审批怎么批、出问题怎么查）。

- **6 个 SKILL** → `~/.workbuddy/skills/`（用户级，任意 WorkBuddy 会话可用）
- **1 个 MCP 服务** → `~/.workbuddy/mcp.json`，服务名 `credit-risk-analysis`（stdio）

两者是配套关系：SKILL 规定「怎么想、什么时候停下来问人」，MCP 提供「真正算数的工具」。只装 SKILL 不装 MCP，Agent 会照规程描述流程但拿不到数据；只装 MCP 不装 SKILL，Agent 会调用工具但不懂成熟度、拒绝推断、审批这些业务红线。

---

## 1. 安装结果一览

| 项目 | 位置 | 内容 |
| --- | --- | --- |
| SKILL | `~/.workbuddy/skills/credit-data-admission/` | `SKILL.md` + `references/data-contract-and-windows.md` |
| SKILL | `~/.workbuddy/skills/portfolio-anomaly-attribution/` | `SKILL.md` + `references/attribution-rules-and-formulas.md` |
| SKILL | `~/.workbuddy/skills/policy-cutoff-simulation/` | `SKILL.md` + `references/cutoff-and-swapset-methodology.md` |
| SKILL | `~/.workbuddy/skills/scorecard-modeling-pipeline/` | `SKILL.md` + `references/scorecard-math-and-thresholds.md` |
| SKILL | `~/.workbuddy/skills/population-distribution-analysis/` | `SKILL.md` + `references/population-spec-and-tests.md` + `references/analysis-discipline.md` |
| SKILL | `~/.workbuddy/skills/deliverable-html-report/` | `SKILL.md` + `references/layout-and-components.md` |
| MCP | `~/.workbuddy/mcp.json` | 服务 `credit-risk-analysis`，暴露 16 个工具（2 个取数 + 14 个分析） |
| 数据 | `~/credit-agent-workspace/datasets/` | 放授权数据集（csv / parquet / xlsx） |
| 口径 | `~/credit-agent-workspace/registries/` | 放指标 / 维度登记表（yaml / json） |
| 账本 | `~/credit-agent-workspace/ledger/` | 运行记录、快照、审批决定（权限 0700） |
| 演示数据 | `~/credit-agent-workspace/datasets/demo_loan_panel.csv` | 合成样本，用于验证链路是否通；接真实数据前请移走 |
| 数据库配置 | `~/.credit-agent/setup.html`（页面）或 `<仓库根>/.env`（模板 `.env.example`） | 取数凭证；只有 `download_sample` 需要，见 2.1。页面权限 `600` |
| 配置页 | `credit-risk-config` / `scripts/config_setup.py` | 生成浏览器填写页，保存后工具自动读取 |
| 自检脚本 | `scripts/verify_workbuddy_install.py` | 四层自检：SKILL 文件 / MCP 注册与三根不相交 / 客户端是否需重启 / 真实握手与 16 工具 |
| 连接自检 | `scripts/check_db.py` | 配置来自哪里、缺哪个变量或驱动、能否连通、可见表 |
| 演示脚本 | `scripts/make_demo_workspace.py`、`scripts/demo_end_to_end.py` | 造合成数据、走完整链路并打印每步状态（`--direct` 免客户端配置直接起服务；`--config <mcp.json>` 复用客户端配置） |

**三个目录必须两两互不相交**，且账本不能放进数据目录——否则 `list_credit_datasets` 会把账本内部的 parquet 当成数据集列出来，造成数据越权可见。

---

## 2. MCP 配置说明

`~/.workbuddy/mcp.json` 实际内容：

```json
{
  "mcpServers": {
    "credit-risk-analysis": {
      "type": "stdio",
      "command": "/Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/.venv/bin/python",
      "args": [
        "-m", "credit_agent.mcp_server",
        "--data-root", "/Users/zhongxing/credit-agent-workspace/datasets",
        "--registry-root", "/Users/zhongxing/credit-agent-workspace/registries",
        "--ledger-root", "/Users/zhongxing/credit-agent-workspace/ledger"
      ],
      "cwd": "/Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent",
      "env": {
        "PYTHONPATH": "/Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent/src"
      },
      "timeout": 30000
    }
  }
}
```

几个容易踩的字段：

- `env.PYTHONPATH` 指向仓库 `src/`：本机 venv 没有 `pip install -e`，没有这一行会报 `No module named credit_agent`。
- `env` 段**只放 `PYTHONPATH` 就够**。数据库凭证建议写进 `.env`（见 2.1）——写在 `env` 段里也能用，但那个文件是明文 JSON、且优先级最高会盖掉 `.env`，排查时容易看错地方。
- `cwd` 必须是仓库根：`mcp_server` 用它解析相对路径，**也是 `.env` 的查找起点**。
- `timeout: 30000` 是**连接超时（毫秒）**，不是单次工具执行超时；模型训练、网格搜索耗时长，客户端侧超时另配。
- 想换数据位置，只改 `args` 里三个 `--*-root`，同时确保新目录存在且互不相交。

**安装后必须手动启用一次**：WorkBuddy 不会自动信任新写入的 MCP。打开连接器管理页，在右上角「自定义连接器」入口找到 `credit-risk-analysis`，点「信任 / Trust」。之后新建会话生效。

### 2.1 数据库连接（取数用，可选）

`download_sample` 需要访问数仓。**只有取数工具需要凭证**——`inspect_and_bind_snapshot` 之后的全部分析工具都只读本地文件，完全离线可用。所以这一段可以最后再配，不配也不影响本地上传链路。

#### 方式 A：配置页（推荐，不用碰配置文件）

一条命令生成一个本地 HTML 页面，在浏览器里选数据库类型、填表、保存：

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
../.venv/bin/python scripts/config_setup.py
# 装过包的话等价于：credit-risk-config
```

它会：读出当前已生效的配置 → 预跑一次连接检测（8 秒超时，不卡）→ 生成 `~/.credit-agent/setup.html` → 自动在浏览器打开。

页面里是 5 个后端的分页表单，切换即填，右上角实时显示"还缺几项"，必填项没填会指名道姓列出来。填完点「保存配置」，重启客户端后生效。

表单是 Python 渲染出来的真实 HTML（不是脚本现场拼的），tab 切换用 CSS。所以在**禁止脚本的环境里也能看到并填写所有字段**——只是「保存」按不动，页面顶部会有一条橙色提示说明这件事，不会让人以为页面坏了。

**配置存在哪**：就在 `setup.html` 自己身上——页面内嵌一个惰性的 `<script id="credit-agent-config" type="application/json">` 数据块，保存时整页写回。所以**一个文件就是全部**，没有旁路文件。

**保存的三种结果**：

| 场景 | 点「保存配置」后 |
|---|---|
| Chrome / Edge 打开（推荐） | 弹出保存对话框。把页面底部那行完整路径粘进「文件名」栏回车，就直接覆盖本页；Chrome 会记住这个位置，之后一键保存 |
| Safari / Firefox | 文件落到「下载」文件夹，再运行 `credit-risk-config --adopt` 装到正确位置（旧版备份为 `.setup.html.bak`） |
| 禁脚本的预览面板 | 按钮没有反应，页面顶部橙色提示会说明原因 —— 这时改用 `--serve` |

#### 为什么不能"纯 HTML 自己存回去"

页面在 `file://` 下写盘走的是浏览器的 File System Access API。它的规则是：**必须由你的一次点击授权，任何页面都不能悄悄改硬盘上的文件**。这是浏览器沙箱，不是这个页面的实现问题 —— 纯 HTML + CSS 没有任何绕过办法。

所以"可编辑的 HTML"能做到，但需要那一下点击 + 一个保存对话框：这就是上表第一行的路径，**不需要任何服务**。区别只在浏览器：

- Chrome / Edge 实现了这个 API，能原地覆盖；
- Safari / Firefox 没实现，只能退化成下载 + `--adopt`。

`credit-risk-config` 会优先用装好的 Chrome / Edge 打开这个文件，就是为了别把你交给只能下载的浏览器 —— 填完才发现存不下去，是最难受的失败方式。

#### 什么时候才需要 `--serve`

只有一种场景：**页面跑在禁脚本的环境里**（典型是客户端的 HTML 预览面板，它连 JavaScript 都不执行）。那时浏览器再好的 API 也调不到，只能让一个本机进程替你写盘：

```bash
credit-risk-config --serve              # 默认 http://127.0.0.1:8765/
credit-risk-config --serve --port 9000  # 端口被占时换一个
```

它只监听 `127.0.0.1`（局域网和公网都访问不到），页面上会多一个「测试连接」按钮，保存走普通表单 POST，**完全不依赖脚本**。填完点保存，终端里按 `Ctrl+C` 退出即可。

> `--serve` 是直接改写 `setup.html` 而不是另存一份，所以退出服务后双击那个文件，看到的仍是刚填的值。

> 浏览器原生的 `Cmd+S` 保存的是**服务器原始响应**，不是页面上填好的内容——填完一定要点页面里的「保存配置」。

**几个附带的命令**：

```bash
credit-risk-config --print      # 打印当前生效配置（密码显示为"已设置"，不打印明文）
credit-risk-config --adopt      # 把「下载」里最新的 setup.html 装到配置目录
credit-risk-config --no-check   # 跳过连接检测（不访问网络）
credit-risk-config --json       # 机器可读
credit-risk-config --serve      # 仅在禁脚本的环境里需要
```

首次生成时，页面会**镜像当前生效的配置**（含密码）。这样"打开页面、什么都不改、直接保存"是一个空操作，不会把已经在用的 `.env` 配置清掉。

页面底部固定显示配置查找顺序，和"密码以明文存在这个文件里、别提交 git"的提示。文件权限是 `600`。

#### 方式 B：`.env` 文件（脚本化 / 批量部署）

适合写进部署脚本、CI、或一次配多套环境。

#### 三条注入路径的优先级

| 路径 | 适合 | 优先级 |
|---|---|---|
| `~/.workbuddy/mcp.json` 的 `env` 段 | 随连接器一起分发、或用不同目录切换多套环境 | **最高**（覆盖下面两者） |
| **配置页**（`config.json` / `setup.html`） | 日常使用。有表单、有校验、不用记变量名 | 中（覆盖 `.env`） |
| `.env` 文件 | 脚本化、CI、批量部署 | 低（只补空缺） |

进程环境永远赢——`mcp.json` 里写了什么就是什么，磁盘上的文件改不动它。所以**页面里某个字段下方如果出现橙色提示"当前生效值来自环境变量或 mcp.json"，说明这个值在 `mcp.json` 里写死了，页面里改了不生效**，要么删掉 `mcp.json` 里的那一项，要么就在那里改。

配置页覆盖 `.env` 是有意的：先有 `.env`、后来改用配置页的人，改完却发现没生效，比"两个地方都能改"更糟。自检脚本 `[1] 配置来源` 会把这两个来源分别列出、并标出被谁覆盖。

#### `.env` 放哪里

按顺序查找，**找到第一个就用**，不会合并多个文件：

1. `$CREDIT_AGENT_ENV_FILE`（显式指定。**指定了就不回退**——写错路径会明确报"文件不存在"，不会悄悄去读别的文件）
2. `<MCP 进程工作目录>/.env`
3. `~/.credit-agent/.env`

**空值等于未配置**：模板里 `CREDIT_AGENT_MAX_SCAN_ROWS=` 这种留空行会被当作"没设置"，走代码默认值，不会变成空字符串把数字字段打挂。所以 `cp .env.example .env` 之后只想填一项也能直接跑。

MCP 进程的 `cwd` 就是仓库根（见上一节 `mcp.json` 的 `cwd` 字段），所以**最省事的做法是在仓库根建立 `.env`**：

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
cp .env.example .env
chmod 600 .env
# 编辑 .env，填 CREDIT_AGENT_DATA_BACKEND 与对应后端的凭证
```

> 本机已经生成好 `<仓库根>/.env`（权限 `600`，值全为空）。现在只需把 `CREDIT_AGENT_DATA_BACKEND` 和对应后端的几行填上，其余空行保持空着即可。

`.env` 已在上级 `.gitignore` 里（`.env` / `.env.*` 被忽略，`.env.example` 例外），不会误提交；Connector 打包也只收 `skills/`、`mcp.json`、`icon.svg`、`README.md`，不会把 `.env` 打进去。

#### 四种后端的 `.env` 写法

切 `CREDIT_AGENT_DATA_BACKEND` 即可，**SQL 不用改**。用配置页的话不必写这些，页面上就是对应的输入框。

**Hive 类数仓（MaxCompute / ODPS / DataWorks）**

```dotenv
CREDIT_AGENT_DATA_BACKEND=odps
CREDIT_AGENT_ODPS_ACCESS_ID=LTAI5t...
CREDIT_AGENT_ODPS_ACCESS_KEY=...
CREDIT_AGENT_ODPS_PROJECT=opmgt_dev
# 可选；内网/金融云/海外 Region 必须显式指定，留空走公网默认
CREDIT_AGENT_ODPS_ENDPOINT=http://service.cn.maxcompute.aliyun.com/api
```

**Hologres**

```dotenv
CREDIT_AGENT_DATA_BACKEND=holo
CREDIT_AGENT_PG_HOST=hgpostcn-cn-xxx.hologres.aliyuncs.com
CREDIT_AGENT_PG_PORT=80
CREDIT_AGENT_PG_DATABASE=your_db
CREDIT_AGENT_PG_USER=your_user
CREDIT_AGENT_PG_PASSWORD=...
CREDIT_AGENT_PG_SCHEMA=public
```

**PostgreSQL**

```dotenv
CREDIT_AGENT_DATA_BACKEND=postgres
CREDIT_AGENT_PG_HOST=10.0.0.12
CREDIT_AGENT_PG_PORT=5432
CREDIT_AGENT_PG_DATABASE=credit_ops
CREDIT_AGENT_PG_USER=reader
CREDIT_AGENT_PG_PASSWORD=...
CREDIT_AGENT_PG_SSLMODE=require
CREDIT_AGENT_PG_SCHEMA=risk
```

**MySQL / MariaDB**

```dotenv
CREDIT_AGENT_DATA_BACKEND=mysql
CREDIT_AGENT_MYSQL_HOST=10.0.0.21
CREDIT_AGENT_MYSQL_PORT=3306
CREDIT_AGENT_MYSQL_DATABASE=credit_ops
CREDIT_AGENT_MYSQL_USER=reader
CREDIT_AGENT_MYSQL_PASSWORD=...
CREDIT_AGENT_MYSQL_CHARSET=utf8mb4
```

`PG_SCHEMA` 建议显式填：不填的话每条 SQL 都得写 `public.xxx`，填了就直接写表名。`PG_SSLMODE` 留空由驱动决定，云上实例通常要求 `require`。

#### 驱动（装一次）

只有 `duckdb` 是基础依赖，其余后端各需一个驱动。**当前本机已装齐全部四个**（`duckdb` / `pyodps` / `psycopg2-binary` / `pymysql`）：

| backend | 驱动 | 单独安装 |
|---|---|---|
| `duckdb` | `duckdb` | 基础依赖，已随包安装 |
| `odps` | `pyodps` | `pip install pyodps` |
| `postgres` / `holo` | `psycopg2-binary` | `pip install psycopg2-binary` |
| `mysql` | `pymysql` | `pip install pymysql` |

一次装全：`pip install -e ".[warehouse]"`。

驱动缺失和密码错误是两回事，但报错经常长得很像——所以自检脚本会先把驱动单独判掉。

#### 配完先自检

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
../.venv/bin/python scripts/check_db.py --list-tables
```

输出四段：**配置来自哪里（页面 / `.env`，以及被谁覆盖）、各后端配置与驱动状态、连接测试、可见表**。它不会打印任何密码值，只报变量名；接不通时直接告诉你缺哪个变量、缺哪个驱动。

```
[1] 配置来源
    配置页：/Users/.../.credit-agent/setup.html
      选中后端：holo
      生效变量 6 个：CREDIT_AGENT_DATA_BACKEND, CREDIT_AGENT_PG_DATABASE, …
    .env：/Users/.../credit_risk_agent/.env
      生效的凭证类变量（值不显示）：CREDIT_AGENT_PG_PASSWORD
[2] 后端可用性
    ● holo      已配置   服务端只读
    ○ odps      未配置   仅 SQL 文本审计
      缺少：CREDIT_AGENT_ODPS_ACCESS_ID/KEY/PROJECT
...
[3] 连接测试（holo）
    ✓ 连接成功  target=holo:xxx.hologres.aliyuncs.com:80/your_db
      服务端只读：是
[4] 可见表
    共 128 张，前 30 张：
      - public.dwd_apply
      ...
```

常用参数：`--backend mysql` 单独验另一个后端（临时试配不用改激活项）、`--json` 给脚本用、退出码非 0 表示当前后端不可用。

#### 常见故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `credentials incomplete`，缺项里全是空变量 | 配置没被读到，或变量名拼错 | 看自检 `[1]` 段：`配置页` 和 `.env` 两条路径分别列出来了；`.env` 需 `cd` 到仓库根再建 |
| 页面里改了、重启后仍是旧值 | `mcp.json` 的 `env` 段写死了同名变量 | 自检 `[1]` 段会显示「被进程环境覆盖」，删掉那一项或改在 `mcp.json` 里 |
| `driver not installed` | 少驱动 | 按提示 `pip install`，或装 `.[warehouse]` |
| `No module named credit_agent` | `mcp.json` 缺 `env.PYTHONPATH` | 补回该字段（见上一节） |
| 连接超时而非报密码错 | 网络不通 / 安全组 / 端口写错 | Hologres 默认 80 不是 5432；ODPS 内网实例必须显式配 `ODPS_ENDPOINT` |
| `permission denied` | 账号无该表 SELECT 权限 | 属源头权限体系，按字段权限申请，工具侧不做绕过 |
| 中文列名乱码 | MySQL charset 不是 utf8mb4 | 设 `CREDIT_AGENT_MYSQL_CHARSET=utf8mb4` |
| ODPS 报分区不存在 | 分区键写错 | 先 `download_sample(preview_only=true)` 看 0 行提示 |

#### 改完配置要重启 MCP 进程

`.env` 和 `mcp.json` 都是**进程启动时读取**的，改完必须让 MCP 子进程重新拉起才生效：重开一个会话；若仍不生效，`Cmd + Q` 完全退出客户端再启动（客户端启动时才构建工具清单）。

#### 只读强度不是五档一样

| backend | 只读由谁保证 |
|---|---|
| `duckdb` | 连接级 `read_only=True` |
| `postgres` / `holo` | **服务器**：`set_session(readonly=True)`，写操作由 PG 直接拒绝 |
| `mysql` | **服务器**：`SET SESSION TRANSACTION READ ONLY`（5.7 以下不支持时如实报告，不静默降级） |
| `odps` | **仅 SQL 文本审计**——MaxCompute 没有等价开关。返回值里会标 `server_side_read_only=false` |

前三档即使文本审计漏了也写不进去；`odps` 档请务必使用只读 RAM 子账号的 AccessKey，别用主账号。


---

## 3. 验证安装

### 3.0 先让客户端识别新配置（最容易漏的一步）

**WorkBuddy 的 MCP 清单是在进程启动时构建的，不会热加载后写入的 `mcp.json`。** 如果配置文件是在客户端已经运行之后才写入的，连接器页面的「自定义」列表里不会出现它——不是配置错，是没重新扫描。

本机已完成的操作（2026-11-12）：

1. `Cmd+Q` 完全退出 WorkBuddy 后重新打开；
2. 「连接器」→ 右上角「自定义连接器」→ 列表出现 `credit-risk-analysis`；
3. 点「**信任**」——信任记录已写入 `~/.workbuddy/mcp-approvals.json`；
4. 客户端日志确认握手成功：
   ```
   [MCP-AuthDiag] stdio-connected {"pid":37634,"id":"custom-mcp:credit-risk-analysis","elapsedMs":2414}
   ```
   同时 `~/.workbuddy/mcp-tool-list.json` 已缓存该服务的 16 个工具。

**还差最后一步：新建会话。** 工具清单属于会话上下文，MCP 连接成功之前建立的会话不会包含这些工具。判定方法——看日志里该会话启动参数中的 `mcp-config` 长度：

```bash
grep -hs "Session <session-id>" ~/.workbuddy/logs/daemon.log | tail -n 1 | sed 's/.*\(mcp-config=<len=[0-9]*>\).*/\1/'
```

`mcp-config=<len=101>` 表示空配置（该会话无 MCP 工具），只有新建会话后才会带上真实配置。

判定「是否需要重启」的方法（已内置在自检脚本里）：

```bash
PYTHONPATH=src ../.venv/bin/python scripts/verify_workbuddy_install.py --skip-handshake
```

若输出 `WARN: 配置写入时间晚于当前客户端启动时间……`，就必须按上面步骤 1–4 重来。

> 说明：MCP 配置的注册表是 `~/.workbuddy/mcp.json`（用户级）；项目级配置存放在 `~/.workbuddy/project-resources/<项目ID>/mcp.json`，两者按服务名合并。本项目目录下的 `.workbuddy/` 只放记忆与临时数据，不放 MCP 配置。

### 3.1 命令行验证（不依赖客户端）

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
../.venv/bin/python -m credit_agent.mcp_server --help
```

能打印出 `--data-root / --registry-root / --ledger-root` 三个参数即说明入口正常。

更完整的四层自检（SKILL 文件 → MCP 注册与三根不相交 → 客户端是否需要重启 → 真实 stdio 握手与 16 工具）：

```bash
PYTHONPATH=src ../.venv/bin/python scripts/verify_workbuddy_install.py
```

预期全部为 `OK`（客户端若早于配置启动会出现一行 `WARN`，见 3.0），最后一行 `OK: stdio 握手成功，发现 16 个工具`。任一静态检查失败会以非 0 退出并打印 `FAIL: 具体原因`。

如果只想做静态检查（不起服务）：

```bash
PYTHONPATH=src ../.venv/bin/python scripts/verify_workbuddy_install.py --skip-handshake
```

### 3.2 客户端验证

新建一个 WorkBuddy 会话，直接问：

> 你现在能调用哪些信贷相关的工具？

预期能看到 `inspect_and_bind_snapshot`、`check_label_maturity`、`run_portfolio_monitoring`、`simulate_policy_rules`、`prepare_modeling_sample` 等 16 个工具，且**没有** `record_human_decision`（它刻意不暴露给 Agent）。

再问一句「你有哪些信贷风控的 skill」，应能列出上述 4 个。

### 3.3 用一份演示数据验证整条链路

数据目录初始是空的，此时 `list_credit_datasets` 只会返回 `count=0`，看不出任何东西。先造一份**合成演示数据**（仅写入 `~/credit-agent-workspace/`，文件名带 `demo_` 前缀，不会被误当生产数据）：

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent
PYTHONPATH=src ../.venv/bin/python scripts/make_demo_workspace.py
```

产出：

- `datasets/demo_loan_panel.csv`：1200 行 / 6 个放款批次（2025-10 ~ 2026-03），不良率从 4.5% 逐步恶化到 16.5%，含 `channel`、`region`、`score`、`mob` 等列；
- `registries/registry_demo.json`：`bad_rate/v1` 指标 + 2 个决策时点维度（`channel`、`region`）；
- `ledger/`：留给服务端初始化。

然后用安装好的 `mcp.json` 真实起服务、走完整链路（`--config` 指向客户端配置，脚本会按里面声明的 `command`/`args`/`env` 拉起服务）：

```bash
PYTHONPATH=src ../.venv/bin/python scripts/demo_end_to_end.py \
  --config ~/.workbuddy/mcp.json
```

本机实测输出：

```
mode: config /Users/<you>/.workbuddy/mcp.json (server=credit-risk-analysis)
handshake ok, 16 tools
[         ok] list_credit_datasets  count=1
[         ok] inspect_and_bind_snapshot  snapshot=snap_c285ea044c1b
[   degraded] check_label_maturity  mature_cohorts=5/6 rule=按放款批次月末 + 3 个完整账龄月 <= cutoff 判定（Vintage/MOB 行业惯例）
[         ok] resolve_metric_and_dimensions  dimensions=['channel', 'region']
[         ok] run_portfolio_monitoring  0.0675 -> 0.1100 (alarm=high)
[         ok] decompose_and_locate_anomaly  run=run_e527a83ab935 [channel:风险效应, region:风险效应]
               channel: mix=0.000291 rate=0.042239 closure=0.0
               region: mix=0.001 rate=0.042038 closure=0.0

allowed conclusions: ['within_group_deterioration', 'unexplained_residual']
```

这条输出里每个状态都值得对照理解：

| 观察到的现象 | 业务含义 |
| --- | --- |
| `check_label_maturity` 是 `degraded`、5/6 成熟 | 最后一个批次（2026-03）表现期不足，**被排除在分母之外**，不是报错 |
| `dimensions` 只有 `channel`、`region` | 登记表里 `cohort`、`obs_time` 声明为 `observation` 时点，被正确拒绝，不会进切片 |
| `mix` 接近 0、`rate` 主导 | 客群结构几乎没变，不良上升来自**同一客群内部恶化**，结论落在 `within_group_deterioration` |
| `closure=0.0` | 结构效应 + 组内效应 + 交互项完全闭合，归因可信 |
| `alarm=high` | 对比期不良从 6.75% 升到 11.00%，需人工复核 |

跑完后数据根里会留下一个 `demo_loan_panel.csv`，接入真实数据时删除或移走即可；账本里会留下本次运行的审计记录，属正常。

---

## 4. 六个 SKILL 的触发时机

| SKILL | 什么时候用 | 示例提问 |
| --- | --- | --- |
| `credit-data-admission` | 任何监控 / 归因 / 策略 / 建模任务的**第一步** | 「这份放款明细能不能做 3 期 MOB 的坏账分析？」 |
| `portfolio-anomaly-attribution` | 已有合法快照，要看存量异动、拆解是结构变化还是区内变化 | 「本月逾期率涨了 0.8 个点，是客群结构变了还是某渠道恶化？」 |
| `policy-cutoff-simulation` | 调审批分数线、挖掘单条件规则、做置换集与盈亏平衡 | 「把 cutoff 从 70 放宽到 60，坏账和通过率会怎么变？」 |
| `scorecard-modeling-pipeline` | 做评分卡：样本准备、特征筛选、训练验证、模型签收 | 「用这批数据训一版评分卡，先看 IV 和 PSI」 |
| `population-distribution-analysis` | 没有标签或标签未成熟，但要看客群结构、字段分布、缺失口径 | 「10 月申请客群结构变了没有？某字段的分布是不是整体上移？」 |
| `deliverable-html-report` | 结论已经算完，要把它交付成一份能看的 HTML 报告 | 「把这些结论做成一份可交付的报告」 |

**不该用的时候**：只是要读一个 Excel、画个图、做纯 SQL 取数——这些不需要走信贷规程，直接用普通工具即可。SKILL 的价值在于守住成熟度、时点、拒绝推断这几条业务红线，不是给所有取数都套一层流程。

---

## 5. 典型工作流

### 5.0 取数：两个入口，一个 manifest

数据从哪来只有两种可能，走哪个入口都产出同一种 `sample_manifest`，后续工具不区分来源。

入口 A 需要先配好数据库连接（见 2.1）；入口 B 不依赖任何外部系统，当天就能验证链路。

**入口 A：数仓下载（自己写 SQL）**

```
1. 先预览（不落盘）：download_sample(sql="select ... limit 200", user_sql="<需求方原句>", preview_only=true)
   → 确认列名、类型、分区条件对不对
2. 正式拉取：
   download_sample(sql=..., user_sql="<需求方原句>", as_of_date="2026-10-31",
                   dataset_name="apply_2025q2", grain_key="apply_id",
                   purpose="需求方给定 SQL，原样执行")
   → 返回 sample_id / row_count / sensitive_columns / path / checks
3. 改用 list_credit_datasets + inspect_and_bind_snapshot 绑定
```

要点：

- **`as_of_date` 必填**。标签成熟度 = 批次月末 + 表现期，而"数据到哪天为止"只有取数那一刻知道，数据本身推不出来。漏填会直接报错，不会猜。
- **`user_sql` 必填，且 `sql` 必须与之逐字一致**。不一致时工具**直接拒绝执行**（`SQL_SCOPE_NOT_AUTHORIZED`），不读数据、不落盘——不是"取完再标注"。需要扩列时先问需求方，拿到同意后把他的原话填进 `authorization` 再调用。
- **`sql` 由分析师自己写**。工具不代写、不改写、不生成；它只保证一条语句、只读、行数有上限，以及**执行的就是需求方给的那条**。
- **输出路径默认 `<data-root>/<dataset_name>.parquet`**，可以改：传目录就放该目录，传文件名就用该名字（后缀决定格式，`.csv` 写 CSV）。
- **同名默认不覆盖**。要替换必须显式 `overwrite=true`——旧快照可能是某份已签字报告的输入。
- `row_cap` 默认 200 万行。超了直接拒绝落盘并提示按月拆分，不会静默截断。
- 没有分区条件不会拦，但会在 `checks.partition_warning` 里提示（MaxCompute 按扫描字节计费）。

**入口 B：本地文件（Excel / CSV / Parquet）**

```
import_sample_file(file_path="~/Downloads/合作方样本.xlsx",
                   dataset_name="partner_2026q1", grain_key="客户号",
                   column_map={"放款金额(元)": "amount", "MOB1逾期天数": "dpd1"})
```

自动探测编码（UTF-8 / GB18030 / GBK）、分隔符、sheet，并返回**数据体检报告**：行数、列数、标签候选列、时间列、高缺失列、敏感列名提示。发现「没有标签列」「没有时间列」「主键重复」会直接列在 findings 里。

清洗**不在工具里做**：本地清洗完再 `import_sample_file` 一次，链条上就同时有「原始」和「清洗后」两个快照，谁都看得出中间做了什么。

### 5.1 从原始数据到一次合规的策略测算

```
1. 把文件放进 ~/credit-agent-workspace/datasets/
2. 让 Agent 列出数据集 → 绑定快照（得到 snapshot_id）
3. 检查表现期成熟度（monthly_cohort，默认）
4. 指标与维度登记（metrics + dimensions）
5. 组合监控（必填 base_periods / comp_periods）
6. 如需归因 → 异常定位（阈值可覆盖）
7. 策略模拟（cutoff / rule_mining）→ 报告提交
```

每一步的返回都是统一 envelope：`status` 取 `ok / degraded / needs_human / blocked` 之一，`blocked` 必带 `block_code`。**看到 `blocked` 不要重试，按码表处置**；看到 `degraded` 说明结论被收窄，不能当成完整结论对外引用。

### 5.2 成熟度按场景分化（重要业务口径）

- **建模 / 存量监控**：默认 `observation_mode="monthly_cohort"`，按「放款批次月末 + W 个完整账龄月 ≤ 数据截止日」判定，符合 Vintage / MOB 行业惯例。未成熟批次不混入分母、不当作好样本。FPD 类窗口按自然日 `T0 + days ≤ cutoff`。
- **策略分析**：用 `observation_mode="latest_observed"` 生成含未成熟批次的最新可观察样本，**只做描述性测算**，目标表现期坏账率、Lift、正式推荐恒为空并返回 `degraded`。这类结果不能用于建模，也不能拿去测算放宽审批后的新增收益。

### 5.3 登记表格式

`~/credit-agent-workspace/registries/` 下的口径文件（YAML 或 JSON）需包含 `metrics` 与 `dimensions`：

```yaml
metrics:
  - metric_id: bad_rate
    version: v1
    flag_column: bad
    period_column: period
    # 列角色必须显式声明，工具不会去猜、也不会扫全表
    loan_id: loan_id
    cohort: cohort
    mob: mob
    bucket: dpd_bucket      # migration 视图需要 bucket 或 dpd
    signed_by: risk_owner
dimensions:
  - column: channel
    point_in_time: decision
    max_cardinality: 12
    min_cell_n: 50
    min_expected_bad: 5
  - column: region
    point_in_time: decision
  - column: obs_time
    point_in_time: observation
```

规则：
- **列角色必须显式声明**。`loan_id`、`cohort`、`mob`、`bucket`/`dpd` 不写，对应视图会被跳过并在 `notes` 里写明「vintage 未计算，缺少列：…」，而不是报错。这是刻意设计：工具不猜列名。
- `point_in_time` 只有 `decision` 的维度才能用于规则挖掘和切片；`observation` 时点会被拒绝为 `TIME_POINT_VIOLATION`（或列入 `rejected_columns`）。
- **不要把期次字段（如 `cohort`）登记为归因维度**。期次本身就是分层定义，用它归因只会得到纯结构效应（实测 `mix` 吃掉全部变化、`rate≈0`），没有业务含义。
- 维度上的 `max_cardinality` / `min_cell_n` / `min_expected_bad` / `bins` / `cut_points` 逐维生效，优先于工具默认值；连续变量用事先登记的固定刀口，不在运行期重分箱。
- YAML **禁止锚点 `&` 与别名 `*`**（会被直接拒绝），禁止超深嵌套。
- 口径文件 ≤ 1 MiB，数据文件 ≤ 100 MiB。
- 规则挖掘的 `features` 必须是已签收登记表中 `point_in_time=decision` 的维度子集，不会默认扫描全表列。

可直接参考 `scripts/make_demo_workspace.py` 生成的 `registry_demo.json`，它把上述要点都写成了可运行示例。

---

## 6. 人工审批怎么走

`record_human_decision` **不注册为 MCP 工具**，Agent 无法自己给自己批。审批必须由受信任宿主（你）用脚本写入，服务端才能消费。

6 个审批闸门：

| gate | 触发场景 |
| --- | --- |
| `feature_list` | 特征名单确认，之后才能训练 |
| `validation_fallback` | 没有 OOT，只能选更弱的验证方式 |
| `maturity_choice` | 表现期不足，选「更短已签收窗口 / 无标签描述 / 等成熟」 |
| `anomaly_disposition` | 异动归因后的处置结论 |
| `policy_trial` | 策略调整的冠军-挑战者试验 |
| `model_signoff` | 模型签收，未签收不得写「已批准 / 建议上线」 |

用法（`--ledger-root` 必须与 MCP 配置里的完全一致）：

```bash
cd /Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent

# 策略试验：选择停止（不放宽）
../.venv/bin/python scripts/host_approval.py \
  --ledger-root ~/credit-agent-workspace/ledger \
  --gate policy_trial \
  --review-id run_policy_1 \
  --actor risk_owner \
  --method stop

# 特征名单确认
../.venv/bin/python scripts/host_approval.py \
  --ledger-root ~/credit-agent-workspace/ledger \
  --gate feature_list \
  --review-id run_screen_1 \
  --actor risk_owner \
  --features feat_a,feat_b
```

要点：
- `--review-id` 必须等于对应工具输出里的 `run_id` / `review_id`，否则审批无效。
- `--actor` 必须是真人标识，写 `agent` 会被拒绝。
- 每个审批决定**只能消费一次**（`consume_decision`），重复使用会被拒；`get_decision` 仍可读，用于审计。
- 报告正文出现「已批准 / 建议上线 / 已立项 / 已签收」但对应 gate 未签收 → 返回 `blocked / SIGNOFF_REQUIRED`。

---

## 7. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| **自定义连接器列表里根本没有 `credit-risk-analysis`** | 配置写入晚于客户端启动，MCP 清单只在启动时构建 | `Cmd+Q` 完全退出再打开；自检脚本会直接报这条 `WARN` |
| 客户端看不到 16 个工具 | 新 MCP 未信任 | 连接器管理页右上角「自定义连接器」→ 点「信任」→ 新建会话 |
| 启动报 `No module named credit_agent` | 缺 `env.PYTHONPATH` | 检查 `mcp.json` 是否指向仓库 `src/` |
| 报 `data-root 与 registry-root 必须是互不相交的独立目录` | 三个根重叠 | 改成三个平级独立目录 |
| `list_credit_datasets` 列出了不该有的 parquet | 账本根放进了数据根 | 把 `--ledger-root` 移出数据目录 |
| YAML 口径文件加载失败 | 用了 `&` / `*` 锚点别名 | 改写成显式重复结构 |
| `download_sample` 报凭证不全 / 缺驱动 / 连不上 | `.env` 没被读到、驱动没装、网络或端口 | 先跑 `python scripts/check_db.py`，对照 2.1「常见故障对照」 |
| 改了 `.env` 但行为没变 | `.env` 是进程启动时读的 | 重开会话；仍无效则 `Cmd+Q` 重启客户端 |
| 工具返回 `blocked` 且带 `block_code` | 业务前置条件不满足 | 按码表处置，**不要重试**；`LABEL_IMMATURE` 表示表现期不足 |
| 返回 `degraded` | 结论被收窄 | 只能作为描述性参考，不得当完整结论引用 |
| 训练/网格搜索超时 | `timeout` 只是连接超时 | 调客户端单次调用超时，或减少候选规模 |
| 候选对比总是「单模型」 | 环境缺 OpenMP，LightGBM 不可用 | 装 OpenMP 或接受 `degraded` 结论 |

---

## 8. 更新与卸载

### 更新 SKILL（改了仓库里的 `skills/` 之后同步）

```bash
SRC=/Users/zhongxing/Desktop/code_project/credit_data_analysis_and_auto_model_agent/credit_risk_agent/skills
for d in credit-data-admission portfolio-anomaly-attribution policy-cutoff-simulation scorecard-modeling-pipeline population-distribution-analysis deliverable-html-report; do
  rm -rf ~/.workbuddy/skills/$d && mkdir -p ~/.workbuddy/skills/$d && cp -R "$SRC/$d/." ~/.workbuddy/skills/$d/
done
```

改完新建会话生效。

### 卸载

1. 删除 6 个 skill 目录：`rm -rf ~/.workbuddy/skills/credit-data-admission ~/.workbuddy/skills/portfolio-anomaly-attribution ~/.workbuddy/skills/policy-cutoff-simulation ~/.workbuddy/skills/scorecard-modeling-pipeline ~/.workbuddy/skills/population-distribution-analysis ~/.workbuddy/skills/deliverable-html-report`
2. 从 `~/.workbuddy/mcp.json` 的 `mcpServers` 中删掉 `credit-risk-analysis` 这一段（**只删这一段，不要删整个文件**）。
3. 数据工作区 `~/credit-agent-workspace/` 建议保留，里面有账本审计记录。

---

## 9. 安全与合规要点

- 数据目录只放**已获授权**的样本；账本目录权限保持 `0700`，不要共享给其他用户。
- 生产环境建议一租户一进程，不要把多个业务线的数据塞进同一组根目录。
- 账本是 append-only：审批决定带时间戳、操作人、渠道，用于事后审计，不要手工改库。
- **凭证只落在 `.env` 或 `mcp.json` 的 `env` 段**，`chmod 600`；不要写进代码、不要贴进对话、不要放进报告。自检脚本与错误信息只报变量名、不打印值——这一点有测试守着。
- **数据库账号用只读子账号**。`odps` 后端没有服务端只读开关，只靠 SQL 文本审计，用最弱的一档就得配最严的账号权限。
- `swap_in_bad_rate` 恒为「未知」：现行拒绝客群不在已放款样本内，任何声称能算出调入集真实坏账率的结论都是错的。
- 公开数据集（欺诈 / HELOC）只能验证代码路径，不能当作信贷表现期结论；真实 OOT 复核必须在授权样本上完成。
