# il2cpp-forge

[English](README.md) | **简体中文**

**面向 Unity IL2CPP 工程的证据驱动 C# 代码还原工具链。**

`il2cpp-forge` 将 IL2CPP 元数据、原生反编译代码和待恢复的 C# 源码树关联起来。它可以识别尚未完成的方法、生成有边界的恢复证据卡、应用候选方法体，并输出结构检查和编译诊断。

这是一个**本地命令行工具**，不是一键 IL2CPP 转 C# 的反编译器。工具本身不调用大模型、不直接操作 IDA/Ghidra，也不负责恢复 Unity 场景和资源。生成的 C# 是基于证据的行为假设，**不是原始源码**。

## 核心能力

- **先建立证据，再恢复代码。** 利用 Il2CppDumper、`script.json`、Native 符号名和地址信息关联托管方法与原生实现。
- **控制模型输入范围。** 为每个尚未完成的成员生成一张 JSON 证据卡，包含方法签名、类型字段、精简 Native 行为、已解析字符串及校验锚点。
- **防止假完成。** 区分部分恢复、占位方法、残留标记和可疑空方法，不通过简单删除注释清空待办。
- **安全应用补丁。** 支持 Dry Run、备份原文件、保存原始证据，默认禁止覆盖已经完成的成员。
- **可追踪验证。** 索引和报告使用 JSON/JSONL，并可通过 .NET 编译获取语法及类型诊断。
- **无第三方 Python 运行时依赖。** CLI 仅依赖 Python 标准库；反编译器和 .NET SDK 按阶段使用。

## 环境要求

- Python **3.10 或以上**。
- [Il2CppDumper](https://github.com/Perfare/Il2CppDumper)，用于主要的元数据导出流程。[Cpp2IL](https://github.com/SamboyCoding/Cpp2IL) 也可作为输入工具，但不同版本的输出格式和参数可能不同。
- [Ghidra](https://github.com/NationalSecurityAgency/ghidra) 或 IDA/Hex-Rays，用于**单独导出**原生伪 C。当前精简规则主要针对 Hex-Rays 输出格式。
- `.NET SDK`，仅在执行 `build` 时需要，并要求目标 Framework 可用。
- 已有的 C# 骨架或源码树。CLI **不会自动从 DummyDll 生成完整的 C# 源文件树**。

仅对拥有分析授权的程序和资源使用本工具。

## 安装

在源码仓库或解压目录执行：

```bash
python -m pip install -e .
il2cpp-forge --version
```

也可以不安装，直接在项目根目录运行：

```bash
python -m forge.cli --help
```

安装后，`forge` 和 `il2cpp-forge` 是同一个 CLI 的两个命令名。

## 快速开始

以下流程假定已准备好外部反编译工具和 C# 骨架。初始化后请先检查 `forge.json`，尤其是 `native.target_assemblies` 与 `source.src_dir`。

```bash
# 1. 初始化本地配置
il2cpp-forge init

# 2. 从 APK/XAPK/拆分包中定位 IL2CPP 文件
il2cpp-forge detect --input game.apk

# 3. 使用 Il2CppDumper 导出元数据和托管方法签名
il2cpp-forge dump --tool il2cppdumper --exe /path/to/Il2CppDumper

# 4. 先用 IDA/Ghidra 独立导出伪 C，再建立索引
il2cpp-forge index --native-dir native/split-c

# 5. 扫描现有 C# 源码树，统计待恢复方法
il2cpp-forge queue --src work/src --csv work/open.csv

# 6. 导出证据卡，交给人工或外部大模型恢复
il2cpp-forge cards --src work/src --limit 20 --out work/cards --markdown

# 7. 将恢复结果组织成 JSON 补丁，先预览
il2cpp-forge apply --patch work/patch.json --src work/src --dry-run

# 8. 正式写入，保留原文件备份和证据快照
il2cpp-forge apply --patch work/patch.json --src work/src --backup work/backup

# 9. 利用证据快照进行验证，再做 C# 编译诊断
il2cpp-forge verify --patch work/patch.json --src work/src \
  --snapshot work/patch.evidence.json --json
il2cpp-forge build --src work/src --refs dump/DummyDll --json

# 10. 生成当前恢复状态报告
il2cpp-forge tally --src work/src --out work/STATUS.md
```

`detect`、`dump` 在已有 `forge.json` 的情况下可自动写回发现的路径。`index` 将索引产物保存到配置的目录。`apply` **成功写入方法体时**，会生成 `*.evidence.json` 快照。正式处理大工程前建议运行 `doctor` 检查配置。

## 命令与流程

| 命令 | 作用 | 主要结果 |
| --- | --- | --- |
| `init` | 创建 `forge.json` | 本地项目配置 |
| `detect` | 在 APK/XAPK/拆分包中查找二进制及元数据 | 载荷路径、元数据信息 |
| `metadata` | 查看元数据头、抽样字符串字面量 | 终端或 JSON |
| `dump` | 调用 Il2CppDumper 或 Cpp2IL | 取决于工具版本的导出文件 |
| `index` | 建立托管方法、Native 符号和地址映射 | `methods.jsonl`、`types.jsonl`、`symbols.json` 等 |
| `helpers` | 基于符号或语料特征识别 IL2CPP 运行时辅助函数 | `helpers.json` |
| `queue` | 扫描、分类未恢复的 C# 成员 | 数量、CSV 或 JSON |
| `cards` | 生成逐成员证据卡 | JSON，可选 Markdown |
| `apply` | 预览或写入方法体 | 更新后的 C#、证据快照 |
| `verify` | 检查未完成标记、占位代码和证据覆盖率 | 逐成员审查结果 |
| `build` | 生成诊断 C# 工程并执行 `dotnet build` | 编译诊断及可配置门禁 |
| `tally` | 汇总恢复队列和验证统计 | Markdown 报告 |
| `doctor` | 检查工具环境及路径 | 环境检查结果 |

完整参数见 `il2cpp-forge <command> --help`。

## 配置说明

`forge.json` 存储具体项目的路径。**相对路径以配置文件所在目录为基准**，不一定是当前终端工作目录。

```json
{
  "project": { "name": "MyGame", "evidence_banner": "UNRESTORED EVIDENCE" },
  "input": { "apk": "", "libil2cpp": "", "metadata": "" },
  "dump": {
    "dump_cs": "dump/dump.cs",
    "script_json": "dump/script.json",
    "dummy_dll_dir": "dump/DummyDll",
    "skeleton_src": ""
  },
  "native": {
    "c_dir": "native/split-c",
    "helper_file": "index/helpers.json",
    "target_assemblies": ["Assembly-CSharp.dll", "MyGame.dll"]
  },
  "source": { "src_dir": "work/src" },
  "index": { "dir": "index" },
  "build": { "dotnet": "dotnet", "target_framework": "netstandard2.1" },
  "gates": { "min_coverage": 0.6, "max_syntax_errors": 0, "check_residue": true, "max_residue": 0 }
}
```

更完整的配置见 [`examples/forge.example.json`](examples/forge.example.json)。默认只处理 `Assembly-CSharp.dll`，其他业务程序集需要手工加入 `target_assemblies`。

## 证据卡与恢复补丁

每张卡片记录成员身份和可用的 Native 证据，包括方法签名、字段/属性、精简反编译语句、有限长度的原始片段、解析后的字符串字面量。对于能够识别的 async 状态机，还会关联 `MoveNext`。**证据卡并不证明生成代码与原生行为等价。**

补丁是 JSON 数组，一个成员一条记录。`body` 只包含**方法内部的代码**，不要带外层花括号：

```json
[
  {
    "file": "Game/Counter.cs",
    "member": "GetValue",
    "params": 0,
    "body": "return _value;",
    "note": "读取索引中确认的实例字段。"
  }
]
```

有重载时建议附上卡片提供的完整 `signature`，提高定位准确性。写入前执行 `apply --dry-run`。默认情况下，目标成员没有证据块时拒绝覆盖，除非显式添加 `--allow-restored`。

**重要：** 保留 `apply` 生成的 `<patch>.evidence.json`，并通过 `verify --snapshot` 传入。否则方法体写入后，原始证据可能已经不在源码中，审核无法完整取回。

## 验证能力与边界

`queue` 将方法分为 `partial`、`stub`、`marker_only`、`empty_silent`、`legit_empty`、`restored`。`verify` 可以发现证据残留、未完成标记、占位实现、Native 机械翻译痕迹、空方法和锚点覆盖率不足的问题。

`build` 会生成一个诊断工程，引用 DummyDll，同时排除已经以源码形式恢复的目标程序集，防止重复类型定义。**当前门禁主要检查语法类错误及残留标记**，并不等同于完整编译成功，也不证明行为一致。必须单独查看 `dotnet build` 退出码和所有编译错误。启发式检查通过，不等于原生语义已被正确还原。

## 已知限制

- **不内置模型推理或自动恢复调度。** 本工具输出证据卡，外部模型或人工产生 JSON 补丁；没有内置按类批量恢复执行器。
- **不内置 IDA/Ghidra 控制器。** 需要先导出 Native 伪 C。精简规则和虚调用识别主要面向 Hex-Rays 风格代码。
- **不能直接还原完整 Unity 工程。** 场景、Prefab、资源、Shader 和 Package 需要其他工具处理。
- **泛型共享处理存在限制。** 重载、共享泛型 Native 函数、Lambda 和状态机等都需要额外核对，不能把不确定的映射当作真实绑定。
- **托管与 Native 映射依赖证据。** 地址可能无法对齐或存在歧义；无法确认 Image Base 时应保留未知状态。
- **验证是启发式的。** 锚点覆盖率、残留检查与 Roslyn 诊断都不能证明完整语义等价。
- 除当前文档所述流程外，不同平台和 Cpp2IL 分支的兼容性仍需结合目标文件验证。

## 项目结构

```text
forge/                 CLI 与核心库
  unity/               APK/XAPK 载荷和元数据
  dump/                Dump 解析
  index/               托管与 Native 符号索引
  native/              伪 C 索引、精简及运行时辅助函数识别
  csharp/              成员解析及方法体操作
  restore/             队列、证据卡、补丁应用与验证
  build/               C# 诊断工程与编译结果
  report/              进度报告
  prompts/             面向模型的成员恢复协议
skills/il2cpp-forge/   Agent Skill 使用规范
examples/              配置示例
docs/                  架构和迁移文档
tests/                 Python unittest 用例
AGENTS.md              代码恢复 Agent 的操作约束
LICENSE                MIT 协议
```

技术设计见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，从旧 Toolkit 迁移见 [`docs/MIGRATION_FROM_V5.md`](docs/MIGRATION_FROM_V5.md)。Agent 处理代码前应阅读 [`AGENTS.md`](AGENTS.md)。

## 许可证

`il2cpp-forge` 采用 **MIT License**，详见 [`LICENSE`](LICENSE)。外部工具、游戏二进制和第三方资源分别受其自身许可证与权利约束，**不因本工具使用 MIT 协议而自动获得授权**。
