# 从旧脚本迁移

旧工具链是 200+ 个单用途脚本，按需增长，彼此靠目录约定耦合。`il2cpp-forge` 把它们收敛成 11 个子命令 + 4 个库模块。

## 命令对照

| 旧脚本 | 新命令 | 说明 |
| --- | --- | --- |
| `reconstruct_cli.py` / `recovery_v2.py` / `pipeline.py` | `il2cpp-forge <stage>` | 一个 CLI，阶段即子命令 |
| `build_il2cpp_recovery_index.py` | `index` | 增加程序集归属、native 对齐、image base 投票 |
| `restore_tally.py` | `queue` + `tally` | 拆成"分类"和"出报告"两件事 |
| `audit_partial_bodies.py` | `queue`（分类）+ `verify`（覆盖率） | 审计不再单独跑一遍树 |
| `rank_unrestored_by_size.py` / `rank_unrestored_worklist.py` | `queue --limit` / `queue --csv` | 排序内置 |
| `method_card.py` / `gap_cards.py` / `review_cards.py` | `cards` | 合并成一种卡：签名 + 字段 + 精简 native + 字面量 + 锚点 + 契约 |
| `apply_bodies.py` | `apply` | 保留 dry-run、备份、证据快照、无证据拒写 |
| `verify_bodies.py` | `verify` | 判定标签一致（EVIDENCE/MARKER/PLACEHOLDER/MECHANICAL/SCAFFOLDING/SILENT_CLOSURE/LOW_COVERAGE） |
| `reopen_partial_bodies.py` / `requeue_partial_bodies.py` | `queue --kind marker_only` | 不再改文件，只报告；要不要重开是人的决定 |
| `native_reduce.py` | `forge.native.decompiled`（`Reducer`） | 运行时辅助集合改成按项目学习 |
| `body_utils.py` | `forge.csharp.body` | 横幅正则化，`INFER:` 保留 |
| `csharp_members.py` | `forge.csharp.members` | 原样保留能力，补 `enclosing_type` / `shape_of` |
| `literal_matcher.py` / `lookup_strings.py` | `index`（`literals.json`）+ 卡片自动解析 | 卡片直接给解析后的字面量 |
| `project_compile.py` / `build_error_report.py` / `classify_syntax_errors.py` | `build` | 去重 + 语法类分类 + 残留扫描 + 门禁 |
| `native_disassembly_cache.py` | `FunctionIndex` 的 TSV 缓存 | 按"文件数 + mtime"失效 |
| `package_toolkit.py` / `export_toolkit_handoff.py` | 不再需要 | 仓库本身就是交付物 |

## 未迁移（有意保留在原仓库）

| 能力 | 原因 |
| --- | --- |
| 模型传输（`run_llm.py`、`model_pipeline.py`、`codex_*.py`） | 端点/成本/重试属于另一层，工具的边界在"卡片出、补丁回" |
| Cpp2IL IL 恢复（`cpp2il_candidates.py`、`cpp2il_recovery.py`） | 依赖 Cpp2IL 具体分支行为，尚未抽象稳定 |
| 依赖/Unity 包装配（`dependency_manager.py`、`stage_*_packages.py`、`prepare_local_unity_project.py`） | 需要具体 Unity 版本与包缓存，属于项目适配 |
| 语义批处理规划（`semantic_batch_planner.py`、`semantic_core.py`） | 与模型侧批策略强耦合 |
| 目标适配脚本（`complete_*.py`、`finish_*.py`、`run-*.cmd`） | 就是"要通用化掉"的那部分，不再需要 |

## 行为差异（迁移时会注意到的）

1. **不再改文件来"清队列"**。旧流程里 `reopen_*` 会重写证据块；新工具只分类，改不改由人决定。
2. **不再有模块白名单**。`target_assemblies` 从 `dump.cs` 的程序集表里来，配置里写清楚即可。
3. **不再硬编码运行时辅助地址**。`sub_52A76A0` 这类常量全部移除，改为 `helpers` 阶段按符号表或调用形状识别。
4. **不再有默认游戏路径**。所有路径来自 `forge.json`，相对路径按配置文件目录解析。
5. **`--no-incremental` 成为默认**。增量构建会把上一次的诊断混进来，让 delta 不可信。
6. **证据块横幅可配置**。旧批次写过不同横幅的项目，改 `project.evidence_banner` 即可，不需要改代码。

## 迁移步骤建议

```bash
# 1. 用旧项目的数据建一个 forge.json，先只填 dump / native / source
il2cpp-forge init
il2cpp-forge doctor

# 2. 跑 index，对比 methods_with_native 是否与旧索引量级一致
il2cpp-forge index --native-dir <旧 split-c>

# 3. 跑 queue，对比旧的剩余统计
il2cpp-forge queue --src <旧 src> --csv open.csv
#   旧报告里"未还原 N"应该 ≈ partial + stub + marker_only + empty_silent

# 4. 先对 5 个成员出卡片，人工核对卡片里的 native 是否就是该方法的
il2cpp-forge cards --src <旧 src> --limit 5 --out cards --markdown

# 5. 再接入模型批量跑
```
