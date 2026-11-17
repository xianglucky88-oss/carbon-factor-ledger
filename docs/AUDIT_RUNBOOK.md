# 离线可复现发布与审计运行手册（Audit Runbook）

本手册面向审计服务器上的操作员。整个流程**不访问网络、不依赖第三方包**，
仅使用 Python 3.9+ 标准库与系统自带的 `sha256sum`。所有 Python 子进程均以
`-I -B`（隔离模式 + 不写字节码）运行，忽略 `PYTHONPATH`、用户 site-packages
与环境钩子，避免同目录下被植入的 `json.py` 等模块劫持解释器。

## 1. 目录与发布身份

```
releases/
├── _work/                       # 临时暂存区（成功后为空，失败后自动清除）
├── current -> <release-id>      # 原子替换的符号链接，永远指向已验证版本
└── <version>-<source12>/        # 不可变发布目录，例如 0.1.0-3aa5debd6e3f
    ├── RELEASE_OK               # 完成标记（先于校验和写入，自身也被哈希覆盖）
    ├── manifest.json            # 发布清单（无时间戳、无主机名，完全确定性）
    ├── manifest.sha256          # manifest.json 的独立哈希侧车
    ├── SHA256SUMS               # 除自身/侧车外全部产物的 sha256，可 sha256sum -c
    ├── footprint.json           # 确定性足迹结果（含因子/单位/证据版本绑定）
    ├── test_output.log          # 测试运行日志（计时行已规范化，跨机构字节一致）
    ├── pyproject.toml
    ├── src/                     # 打包源码
    ├── fixtures/                # 确定性输入、规则、证据
    └── tests/                   # 打包的领域测试（verify 阶段会重跑）
```

发布 ID = `项目版本-源码树哈希前12位`。源码、fixture 或 pyproject 的任何字节
变化都会产生新的发布 ID；内容不变则发布 ID 不变，重复构建永远命中同一个目录。

## 2. 标准操作

```bash
# 构建（在仓库根目录执行；--release-root 可指向审计盘上的任意位置）
python3 -I scripts/build_release.py build --release-root /var/audit/releases

# 独立验证一个已发布版本（重算全部哈希、重跑打包 CLI 与打包测试）
python3 -I scripts/build_release.py verify /var/audit/releases/<release-id>

# 不依赖 Python 的纯工具校验（离线、可交叉复核）
cd /var/audit/releases/<release-id> && sha256sum -c SHA256SUMS
cat manifest.sha256 | sha256sum -c

# 回滚到上一版本（回滚前强制 verify，未通过或 --skip-tests 的版本会被拒绝）
python3 -I scripts/build_release.py rollback \
    --release-root /var/audit/releases <previous-release-id>

# 审查两个版本的差异（足迹变化时退出码为 1）
python3 -I scripts/build_release.py diff \
    /var/audit/releases/<old> /var/audit/releases/<new>
```

## 3. 构建阶段与“不会出现假成功”的保证

构建全部发生在 `releases/_work/.stage-<id>-XXXX/` 临时目录中，依次执行：

1. **离线策略 + 模块导入检查**：静态扫描禁止网络模块（`urllib`/`socket`/
   `requests` 等）；对每个 `.py` 做内建 `compile()` 语法校验（不落盘 pyc），
   再在隔离解释器中导入全部公开模块。
2. **单元测试**：隔离解释器运行打包的领域测试 `test_core.py`，摘要写入
   manifest（测试数、失败/错误/跳过数、排序后的测试 ID）。
3. **足迹 CLI**：用打包源码运行 CLI 生成 `footprint.json`，非零退出即失败。
4. **重复运行确定性检查**：在另一个时区（默认 `America/Los_Angeles`）下重跑，
   输出必须与第一次**逐字节相同**，临时文件 `footprint.rerun.json` 随即删除。
5. **manifest 与校验和装配**：写入 `RELEASE_OK` → `manifest.json` + 侧车 →
   `SHA256SUMS`。
6. **独立验证**：对暂存目录执行完整 `verify`（哈希重算、CLI 复现、测试重跑）。
   只有全部通过，才用一次 `os.replace` 原子 rename 发布，并原子切换 `current`。

任一阶段失败：立即返回非零退出码、错误信息进 stderr、**暂存目录整体删除**、
目标发布目录绝不创建或修改、`current` 绝不移动。因此磁盘上要么不存在该发布，
要么是一个带 `RELEASE_OK` 且通过全部校验的完整发布，没有第三种状态。

已存在的发布目录**永远不会被覆盖**：重复构建自动转为只读 verify；并发出现的
同名目录会触发显式报错而不是替换。

## 4. 必须清除的临时文件及处置方式

| 临时对象 | 产生位置 | 处置 |
| --- | --- | --- |
| `_work/.stage-<id>-*` | 每次构建的暂存目录 | 成功时被原子 rename 为正式发布；失败时由上下文管理器整体 `rmtree` |
| `footprint.rerun.json` | 确定性重跑 | 比对后立即 `unlink`，不进入发布 |
| `verify.footprint.json` | verify 阶段重跑 CLI | 比对后立即 `unlink`；测试断言它不得残留 |
| `__pycache__/`、`*.pyc`、`*.pyo` | 解释器/编辑器 | 子进程用 `-B` + `PYTHONDONTWRITEBYTECODE=1` 禁止生成；构建前守卫发现即拒绝；构建结束 `prune_caches` 再扫一遍 |
| `.pytest_cache/.mypy_cache/.ruff_cache` | 本地工具 | 打包时 ignore，结束时清理 |
| `current` 的临时符号链接 `.<name>.<id>.tmp` | 切换 current | `os.symlink` 后立即 `os.replace`，失败不会留下半链接 |

审计检查点：`releases/_work/` 在任意一次构建结束后必须为空；正式发布目录中
`find . -name '*.pyc' -o -name '__pycache__'` 必须无输出。

## 5. 两次足迹结果的差异审查

1. `diff <old> <new>` 输出结构化报告：
   - `file_changes`：按产物路径列出 added/removed/modified/unchanged（基于哈希）；
   - `changed_sections`：manifest 中 runtime/hashes/test_summary/module_imports
     的变化；
   - `rule_binding_changed`：因子、单位、证据规则的**版本或哈希**是否变化；
   - `footprint_field_diff`：足迹信封中每个发生变化的字段的左右值对照，例如
     `footprints: {left: {P1: 20.0 ...}, right: {P1: 25.0 ...}}`。
2. 退出码 `0` 表示足迹信封逐字段一致；`1` 表示足迹发生变化，需要人工签字。
3. 建议的审查顺序：先看 `rule_binding_changed` 判断是否规则版本漂移，再看
   `footprint_field_diff` 定位受影响产品与数值，最后用 `file_changes.modified`
   回到具体源文件/规则文件的哈希变化。
4. 纯命令行复核：`diff <old>/footprint.json <new>/footprint.json` 与
   `sha256sum -c SHA256SUMS`（两个目录分别执行）。

## 6. 覆盖的场景与对应测试

| 场景 | 测试 |
| --- | --- |
| 干净目录首次构建、产物完整性、manifest 绑定 | `test_release.CleanBuildTests` |
| 不同时区（UTC / Asia/Tokyo）逐字节一致 | `test_timezone_variation_does_not_change_release_id`、CLI 时区测试 |
| 输入遍历顺序（活动数组打乱） | `test_shuffled_inputs_produce_identical_business_envelope`、`test_input_hash_three_level_identity` |
| 重复构建不覆盖、同发布 ID、inode 不变 | `test_repeat_builds_are_byte_identical_and_never_overwrite` |
| 上一版本回滚（含拒绝未验证/跳过测试版本） | `VerifyAndRollbackTests` 全部用例 |
| 无效/过期/未生效因子 | `test_factor_state_matrix`、`test_not_yet_effective_factor_excluded` |
| 单位换算（kg/g/t/tonne）与未知单位 | `test_unit_conversion_kg_g_tonne`、`test_unknown_factor_and_unknown_unit_fail` |
| 空证据路径（输入层与 manifest 层） | `test_empty_evidence_id_is_rejected`、`test_evidence_manifest_rejects_empty_path` |
| 证据缺失/篡改哈希 | `test_evidence_manifest_detects_tampering_and_missing_files`、`test_tampered_evidence_blocks_publication` |
| 失败注入（语法错误、测试失败、规则损坏、离线策略、缓存污染） | `FailureInjectionTests` |
| 临时文件清理 | `test_build_in_clean_directory_produces_complete_release`、`test_failed_build_leaves_no_release_and_cleans_staging` |

## 7. `--skip-tests` 的边界

该开关仅供失败注入/规则变异的工程化重建使用。使用它构建的发布在 manifest 中
记录 `test_summary.skipped = -1`，verify 会打印 WARNING，**rollback 硬性拒绝**
将其设为 current。它永远无法伪装成一个完整验证过的足迹包。
