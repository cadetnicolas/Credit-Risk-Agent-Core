# Cursor 接入信贷分析 SKILL 与 MCP

适用对象：要在 Cursor 里使用本仓库六个 SKILL 和 stdio MCP 的人。  
对应代码：`cursor/`、`skills/`、`src/credit_agent/mcp_server.py`。

共享规则（三个根目录、审批卡、六步验证）见 [跨平台接入指南](integration.md)。本文只写 Cursor 插件和 WorkBuddy 连接器的差别。

## 1. 插件里有什么

| 组件 | 位置 | 用途 |
| --- | --- | --- |
| 清单 | `.cursor-plugin/plugin.json` | 插件名 `credit-risk-analysis`、版本、图标 |
| SKILL | `skills/<name>/` | 六个规程，从仓库 `skills/` 原样复制 |
| MCP | 插件根目录 `mcp.json` | 启动 `python -m credit_agent.mcp_server` |

Cursor 按目录发现 `skills/` 和 `mcp.json`。清单里不写 `skills` / `mcpServers` 路径，避免把默认发现关掉。

服务在审批提示里会带插件前缀，显示为 `plugin-credit-risk-analysis-credit-risk-analysis`。这是 Cursor 的命名，不是第二个服务。

## 2. 和手写 `~/.cursor/mcp.json` 的关系

[接入指南](integration.md) 的方式 A 仍然有效：把 `command` / `args` / `env` 写进 `~/.cursor/mcp.json`，并把 SKILL 拷到 `~/.cursor/skills/`。那是不打包的接法。

本适配器是方式 C：构建一份 Cursor 插件，安装到 `~/.cursor/plugins/local/credit-risk-analysis`。

两套不要同时启用。用户级 `mcp.json` 里已有 `credit-risk-analysis` 时，再安装插件会多出一个同计算进程的入口。

## 3. 构建

在源码检出目录执行，且 `--output` 必须在仓库外面：

```bash
python cursor/build_connector.py \
  --python /absolute/path/to/venv/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-cursor \
  --install
```

`--install` 复制到 `~/.cursor/plugins/local/credit-risk-analysis`。目录已存在时加 `--replace`。然后执行 **Developer: Reload Window**。

生成的 `mcp.json` 只含 Cursor 使用的字段：

| 字段 | 作用 |
| --- | --- |
| `command` | 本机解释器绝对路径 |
| `args` | `-m credit_agent.mcp_server` 加三个 root |
| `cwd` | 源码检出目录 |
| `env.PYTHONPATH` | `<项目根>/src` |

不写 `type` 和 `timeout`。这两个字段属于 WorkBuddy 连接器。训练类工具可能跑几分钟，以 Cursor 自己的调用时限为准。

`command`、`cwd`、`PYTHONPATH` 和三个 root 都是构建机绝对路径。换机器要重新构建，不能拷贝插件目录过去。

企业版默认可能关闭本地插件导入。看不到插件时，让管理员打开 Allow Local Plugin Imports。市场里若已安装同名插件，市场版本优先于本地目录。

## 4. 自检

```bash
PYTHONPATH=src python scripts/verify_cursor_install.py
```

静态检查：

```bash
PYTHONPATH=src python scripts/verify_cursor_install.py --skip-handshake
```

通过时应当看到：清单名称正确、6 个 SKILL、三个根目录互不相交、stdio 握手列出 16 个工具。`record_human_decision` 不得出现。

审批仍由宿主写入账本，使用 `scripts/host_approval.py`。不要把这个函数暴露成 MCP 工具。

## 5. 卸下

删除 `~/.cursor/plugins/local/credit-risk-analysis` 这一个目录，然后 Reload Window。不要删整个 `~/.cursor`。如果当初走的是手写 `mcp.json`，只从 `mcpServers` 里去掉 `credit-risk-analysis` 这一段，并删掉 `~/.cursor/skills/` 下对应的六个目录。
