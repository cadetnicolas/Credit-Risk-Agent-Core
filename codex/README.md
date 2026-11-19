# Codex 信贷分析插件

适配器从当前源码检出读取版本和全部 Skill，生成 Codex 本地插件市场。插件里的 stdio MCP 服务用指定的 Python 运行同一份项目源码。

## 构建

需要 Python 3.11+ 和支持本地插件市场的 Codex CLI。在源码根目录执行，已有 Conda 环境可直接指定其 Python。

```bash
/absolute/path/to/conda/envs/dm/bin/python -m pip install -e '.[mcp]'
/absolute/path/to/conda/envs/dm/bin/python codex/build_connector.py \
  --python /absolute/path/to/conda/envs/dm/bin/python \
  --data-root /absolute/path/to/authorized-data \
  --registry-root /absolute/path/to/signed-registries \
  --ledger-root /absolute/path/to/credit-ledger \
  --output /absolute/path/to/credit-risk-codex
```

数据与口径目录须已存在，账本目录的父目录须已存在。三个根目录不能互相包含。输出须为项目与三个运行目录之外的新目录，其父目录须已存在。构建时会检查目标 Python 能否导入 MCP 服务。已有合适依赖时可直接构建，插件会通过 `PYTHONPATH` 读取本项目 `src/`。

产物包含 `.agents/plugins/marketplace.json` 和 `plugins/credit-risk-analysis/`。后者包含 `.codex-plugin/plugin.json`、`.mcp.json` 和项目 Skill。生成的 MCP 配置仅包含本机路径和启动参数。使用原有的 `credit-risk-config` 配置数据源。需要连接数仓时，在目标环境按后端安装相应的可选驱动。产物绑定本机的解释器、源码与数据目录，应在目标机器上构建。

## 在 Codex 中安装

```bash
codex plugin marketplace add /absolute/path/to/credit-risk-codex
codex plugin add credit-risk-analysis@credit-risk-local
```

重新打开 Codex 会话以加载插件。此前若通过用户配置手动注册了 `credit-risk-agent`，或手工安装过同名 Skill，切换时可禁用旧 MCP 条目并将旧 Skill 移出发现目录，避免重复。保持数据、口径和账本路径一致即可继续使用已有本地快照。

更新时先成功构建到新目录，然后按以下顺序切换插件和市场。完成后重新打开 Codex 会话。

```bash
codex plugin remove credit-risk-analysis@credit-risk-local
codex plugin marketplace remove credit-risk-local
codex plugin marketplace add /absolute/path/to/new-credit-risk-codex
codex plugin add credit-risk-analysis@credit-risk-local
```

可使用 `codex plugin list --marketplace credit-risk-local` 检查插件发现，在会话内调用 `list_credit_datasets` 检查 MCP 是否可调用。卸载插件使用上面的 `codex plugin remove` 命令，卸载市场使用 `codex plugin marketplace remove credit-risk-local`。
