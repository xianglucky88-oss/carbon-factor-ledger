# Carbon Factor Ledger

带证据链的碳因子账本与可追溯重算：统一物料、活动、单位、地理范围与因子
版本，判断每条消耗记录的因子适用性，对缺证据、过期、撤回、边界不匹配等
情况给出隔离原因，并输出产品/期间足迹、贡献明细与受影响对象。

## 能力概览

- **事件溯源账本**：证据登记、因子版本登记、撤回、消耗记录、报告冻结五类
  事件；事件内容寻址（SHA-256），重复导入幂等，乱序输入经确定性拓扑排序
  后得到相同状态哈希。
- **因子版本与有效期**：替换 = 登记新版本（带 `supersedes` 与有效期窗口），
  撤回 = 独立状态事件；二者只在各自有效范围内生效。
- **适用性规则引擎**：证据缺失/哈希漂移、撤回、过期/未生效、地理范围、
  生命周期边界与活动/物料流身份、单位维度——规则不短路，可叠加多个隔离
  原因，无效因子不会被静默当成零。
- **单位换算**：质量、能量、距离、货运质量距离、体积、面积统一注册表，
  先换算到因子基准单位再相乘，排放统一折算 kgCO2e；跨维度换算隔离而非归零。
- **证据链守恒**：每笔贡献携带证据 id 与登记哈希；哈希漂移保留原哈希并
  隔离整条依赖链。
- **冻结报告不可覆盖**：冻结即快照复制；后续事件只能将报告标记为
  `superseded` 并记录触发事件。
- **三种重算策略**（同一份计算代码，数值一致，仅重算范围不同）：
  - `materialized`：按产品物化，脏单元刷新；
  - `graph`：因子依赖图，沿边增量传播 + 逐记录排放增量；
  - `report_time`：报告时全量重算（无派生状态，作为正确性基线）。
- **事务与恢复**：批次失败整体回滚，无半应用临时状态；重放事件日志即可
  恢复出相同状态哈希。

## 模块

| 文件 | 职责 |
| --- | --- |
| `src/carbon_factor_ledger/models.py` | 不可变领域契约（事件、因子版本、记录、贡献、隔离、冻结报告、统计） |
| `src/carbon_factor_ledger/units.py` | 统一单位注册表与确定性换算 |
| `src/carbon_factor_ledger/parser.py` | 事件校验、内容寻址 id、确定性拓扑排序 |
| `src/carbon_factor_ledger/ledger.py` | 事件溯源状态、哈希链、事务批次、因子解析 |
| `src/carbon_factor_ledger/applicability.py` | 适用性规则与逐记录分类（贡献/隔离） |
| `src/carbon_factor_ledger/engine.py` | 单元评估、全量重算、冻结报告影响标记 |
| `src/carbon_factor_ledger/materialized.py` | 策略 A：按产品物化 + 脏单元刷新 |
| `src/carbon_factor_ledger/graph.py` | 策略 B：因子依赖图增量传播 |
| `src/carbon_factor_ledger/report.py` | 稳定确定性的输出信封 |
| `src/carbon_factor_ledger/pipeline.py` | 编排边界与 `LedgerSession` |
| `src/carbon_factor_ledger/cli.py` | 确定性 JSON CLI |

设计细节与三策略比较见 [`docs/design.md`](docs/design.md)。

## 运行

```bash
# CLI（默认 materialized，可切换 graph / report_time）
PYTHONPATH=src python3 -m carbon_factor_ledger.cli fixtures/input.json --strategy graph

# 测试（无第三方依赖）
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

fixture 为本地确定性数据，不需要网络或凭据。
