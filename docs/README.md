# 文档索引

按「你现在想干什么」来找，不按目录树。

## 想跑起来

| 文档 | 内容 |
| --- | --- |
| [../README.md](../README.md) | 项目定位、设计原则、快速开始、工具与 SKILL 清单 |
| [integration.md](integration.md) | 跨平台接入：MCP 客户端配置、原生工具注册、六步验证清单、踩坑 |

## 想搞懂它为什么这样设计

| 文档 | 内容 |
| --- | --- |
| [architecture.md](architecture.md) | 分层与依赖方向、数据流、账本结构、门禁生效位置、扩展点、设计取舍 |
| [sample-acquisition-design.md](sample-acquisition-design.md) | 取数链路：原样执行 SQL 的授权契约、单语句/只读/行数上限、manifest 与落盘 |
| [../skills/](../skills/) | 五个 SKILL 的业务方法论与 references（准入、监控归因、策略模拟、评分卡、报告版式） |

## 平台专用

| 文档 | 内容 |
| --- | --- |
| [workbuddy-skill-mcp-integration-guide.md](workbuddy-skill-mcp-integration-guide.md) | WorkBuddy 连接器的构建与打包：版本兼容、`mcp.json` 字段、ZIP 结构、发布自检。可作其它平台的接入模板 |
| [workbuddy-install-and-usage.md](workbuddy-install-and-usage.md) | WorkBuddy 装好之后怎么用：SKILL 触发时机、数据目录约定、审批 CLI、排障 |

这两份是**参考实现记录**，不是使用前提。其它平台走 [integration.md](integration.md) 即可。

## 验证记录

| 文档 | 内容 |
| --- | --- |
| [skill-mcp-validation-2026-11-11.md](skill-mcp-validation-2026-11-11.md) | 公开数据（Kaggle 反欺诈 / HELOC）上的功能复核结果与能力边界 |
| [credit-skill-mcp-audit-2026-11-11.html](credit-skill-mcp-audit-2026-11-11.html) | 修复前的只读完备性复核快照，记录问题清单与修复范围 |

复跑方式：

```bash
PYTHONPATH=src python scripts/verify_public_datasets.py \
  --fraud /path/to/creditcard.csv \
  --heloc /path/to/heloc_dataset_v1.csv
```

公开数据只能证明代码路径通，不能证明业务口径对——真实结论必须用带申请日与表现标签的授权样本。

## 历史资料

[`archive/`](archive/) 里是产品设计稿、开发计划与旧验证报告。**不是实现依据**，只用于追溯决策过程。每份文件顶部都有 Status 抬头说明它被什么取代。

## 阅读顺序建议

- **第一次接触**：README → `make_demo_workspace.py` + `demo_end_to_end.py --direct` → architecture
- **要接平台**：integration 第一节（三个决策）→ 对应方式 → 六步验证
- **要改代码**：architecture → skills 的 references → `tools/codes.py` → `tests/test_release_contract.py`
- **要评估能否用于生产**：README 的「已知限制」→ validation 记录 → architecture 的门禁表
