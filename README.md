# il2cpp-forge

**面向 AI 的 Unity IL2CPP 逆向工具链**：把「反编译 → 逐方法还原 → 验证 → 编译门禁」做成一条可重复、可审计、可交给模型批量执行的流水线。

零第三方依赖（只用 Python 标准库），所有中间产物都是明文 JSON/JSONL，任何阶段都能单独重跑，任何结论都能追到具体证据。

---

## 1. 它解决什么问题

Unity IL2CPP 逆向真正的难点不是「拿到一份 C# 骨架」，而是**判断这份 C# 对不对**。常见的失败方式有三类，这个工具针对性地处理：

| 失败方式 | 表现 | 工具的做法 |
| --- | --- | --- |
| **静默关闭** | 把 `UNRESTORED EVIDENCE` 块删掉，队列就"清空"了，bug 留着 | 队列按证据块和未完成标记两条线统计；`marker_only` / `empty_silent` 单独列出 |
| **假还原** | `throw new NotImplementedException()`、`return default`、反射伪造分发 | `verify` 逐条判定 `PLACEHOLDER` / `SCAFFOLDING` / `MECHANICAL` |
| **计数幻觉** | 总错误数上升就以为退步，其实是语义检查变深了 | 门禁只看 **语法类错误 = 0** + **残留 = 0**，总错误数只作参考 |

再加上一个正向能力：**为每个方法生成一张自包含的"证据卡"**，模型只看到它需要的东西（签名、字段布局、精简后的 native 行为、已解析的字符串字面量、锚点），看不到整份文件，因此不会越界发明 API。

---

## 2. 安装

```bash
git clone https://github.com/YOUR-NAME/il2cpp-forge && cd il2cpp-forge
python -m pip install -e .        # 可选：装完可以直接用 il2cpp-forge 命令
python -m unittest discover -s tests -v
```

要求 Python ≥ 3.10。运行时不需要任何 pip 包（CI 在 3.10 / 3.11 / 3.12 / 3.13 × Linux / Windows 上跑同一套测试）。

不想污染环境的话用 pipx：

```bash
pipx install git+https://github.com/YOUR-NAME/il2cpp-forge
il2cpp-forge --version
```

`il2cpp-forge` 和 `forge` 是同一个入口，后者只是短名字。

外部工具（按需，工具本身不捆绑）：

| 工具 | 用途 | 说明 |
| --- | --- | --- |
| [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) | 生成 `dump.cs` / `script.json` / `DummyDll` | 主力路径 |
| [Cpp2IL](https://github.com/SamboyCoding/Cpp2IL) | 生成可编译骨架、DummyDll | 备用路径，参数因版本而异 |
| IDA / Ghidra | 反编译 `libil2cpp.so` 为 C | 需要按类/按函数导出成 `.c`（见下方限制） |
| .NET SDK | `build` 阶段做编译诊断 | `dotnet build` 能跑即可 |

---

## 3. 五分钟跑通

```bash
# 0. 建项目
il2cpp-forge init                       # 写出 forge.json

# 1. 从 APK / XAPK / 拆分包目录里定位 IL2CPP 载荷
il2cpp-forge detect --input game.apk
#   libil2cpp   : work/input/arm64-v8a/libil2cpp.so
#   metadata    : work/input/metadata/global-metadata.dat
#   metadata ver: 31 (Unity 2022.2 - 2023.1)

# 2. 跑 dumper（也可以手动跑，然后只填 forge.json）
il2cpp-forge dump --exe tools/Il2CppDumper/Il2CppDumper.exe

# 3. 建索引：类型/方法/RVA/程序集归属 + 与 native 函数对齐
il2cpp-forge index --native-dir native/split-c
#   types          : 34614
#   methods        : 219183
#   target methods : 57790
#   native join    : 54321 (script 41200, name 13121, address 0)
#   image base     : 0x0

# 4. 看看还剩多少、是什么形状
il2cpp-forge queue --src work/src --csv work/open.csv

# 5. 生成证据卡（交给模型）
il2cpp-forge cards --src work/src --limit 20 --out work/cards --markdown

# 6. 模型返回 JSON → 落地（先 dry-run）
il2cpp-forge apply --patch work/patch.json --src work/src --dry-run
il2cpp-forge apply --patch work/patch.json --src work/src --backup work/backup

# 7. 判定 + 编译门禁
il2cpp-forge verify --patch work/patch.json --src work/src --json
il2cpp-forge build  --src work/src --refs dump/DummyDll --json

# 8. 出一页状态报告
il2cpp-forge tally --src work/src --out work/STATUS.md
```

---

## 4. 阶段详解

### `detect` — 定位载荷

支持 `.apk` / `.xapk` / `.apks` / 拆分包目录 / 裸目录：

- 在 zip 内按 `lib/<abi>/libil2cpp.so` 找库，ABI 优先级 `arm64-v8a > armeabi-v7a > x86_64 > x86`；
- 在 `assets/bin/Data/Managed/Metadata/global-metadata.dat` 找元数据；
- XAPK 会向里递归一层（base APK 带元数据、ABI 分包带 `.so` 是常见组合）；
- 从 `globalgamemanagers` 里读 Unity 版本；
- 自己解析 `global-metadata.dat` 头部（magic `0xFAB11BAF` + 版本 + 字面量表偏移），不依赖 dumper。

```bash
il2cpp-forge metadata --literals 20     # 顺便看一眼字面量表
```

### `dump` — 跑反编译器

`--tool il2cppdumper|cpp2il|auto`。跑完会自动在输出目录里找 `dump.cs`、`script.json`、`DummyDll/`、`cs/`，并把相对路径写回 `forge.json`。`--dry-run` 只打印命令行。

### `index` — 建索引（最关键的一步）

产出在 `index/`：

| 文件 | 内容 |
| --- | --- |
| `methods.jsonl` | 目标程序集的每个方法：签名、参数、RVA/VA、是否静态、属性、native 函数名与所在 `.c` |
| `types.jsonl` | 目标类型的字段（含偏移）、属性、基类 |
| `symbols.json` | `mangled type → {类型全名, 程序集, 是否目标}`，用于判断"这个调用是框架还是业务" |
| `assemblies.json` | 每个程序集的类型/方法数量与是否目标 |
| `literals.json` | 字符串字面量表（`StringLiteral_N` 的第 N 项） |
| `join.json` / `summary.json` | 对齐统计、image base |

**托管 ↔ native 对齐按可信度依次尝试**：

1. `script.json` 的 `ScriptMetadataMethod`（`Method$Ns.Type.Name(Args)` → 函数 VA）——最可靠，能扛住重载与改名；
2. mangled 名匹配（`Sirius_AD_ADFactory__Create`）；
3. 地址匹配（`image_base + rva`），其中 image base 用前两种方式**投票**得出，票数不过半就报 0 而不是猜。

> 注意 `target_assemblies`：默认只有 `Assembly-CSharp.dll`。真实项目里业务代码常常在自研程序集（`Foo.dll`）里，务必在 `forge.json` 里改。

### `queue` — 还剩什么

把树里每个成员分类，而不是只数证据块：

| kind | 含义 |
| --- | --- |
| `partial` | 有证据块，也有一点点代码（半成品） |
| `stub` | 有证据块，但只有 `throw NotImplementedException` / `return default` |
| `marker_only` | 证据块被删了，但 `// RECOVERY_PARTIAL` / `// ASSUMED` 还在 —— **假装完成的那种** |
| `empty_silent` | 空体、无标记，且不是天然空成员（ctor/Dispose/Awake…） |
| `legit_empty` | 天然空成员 |
| `restored` | 无证据、无未完成标记、有实质代码 |

### `cards` — 给模型的证据卡

一张卡 = 一个方法所需的全部信息，有界、自包含：

- `signature` + `type_context`（字段名/类型/偏移、属性）——保证模型只用真实存在的名字；
- `evidence.reduced`：**精简后的 native 行为语句**（500 行反编译 → 10~60 条），类初始化、写屏障、接口分发、空指针/越界抛出、状态机簿记全部剔除；
- `evidence.raw`：原文（截断），供歧义时核对；
- `evidence.movenext`：async 方法的真身（逻辑在状态机里，trampoline 里什么都没有）；
- `anchors`：忠实实现**应该**出现的调用 / 写入字段 / 字面量，用于事后打分；
- `output_contract`：要求模型返回的 JSON 形状。

`--markdown` 会同时写一份人读的 `.md`。

### `apply` — 落地

- `body` 只写方法**内部**语句，工具自己补花括号并按成员缩进重排；
- 默认拒绝覆盖**已经没有证据块**的成员（防止过期补丁覆盖已完成实现）；
- 替换前把旧 body（含证据块）快照到 `<patch>.evidence.json`，因为验证还需要它；
- `--dry-run` 报告会写多少、跳过多少、为什么跳过。

### `verify` — 判定

逐成员给出问题标签：

| 标签 | 含义 |
| --- | --- |
| `EVIDENCE` | 证据块还在 → 仍然是待办 |
| `MARKER` | `PARTIAL`/`DRAFT`/`ASSUMED`/`INFER_RESTORE`/`TODO` 还在 |
| `PLACEHOLDER` | `NotImplementedException` / `MECHANICAL_TRANSLATION` |
| `MECHANICAL` | `Il2CppObject` / `sub_XXXXXX` / `->fields` / `0LL` / `StringLiteral_N` |
| `SCAFFOLDING` | 反射伪造分发 |
| `EMPTY` | 无语句且不是天然空成员 |
| `SILENT_CLOSURE` | native 有锚点，body 一个都没提 |
| `LOW_COVERAGE:0.xx` | 锚点覆盖率低于 `--min-coverage`（默认 0.6） |

覆盖率是**代理指标，不是证明**：它能抓出"看起来写完其实没写"，抓不出"写全了但顺序错了"。所以它是分诊工具，不是验收工具。

### `build` — 编译门禁

生成一个只用于诊断的 `.csproj`，用 `DummyDll` 作为引用程序集，并**排除正在被还原成源码的那些程序集**（否则每个类型都存在两份，满屏 CS0433，真正的错误被淹没）。

输出四件事：语法类错误数、残留统计、总错误数、按错误码聚合。门禁：

```json
"gates": { "max_syntax_errors": 0, "check_residue": true, "max_residue": 0 }
```

> 判定顺序永远是：语法类错误 → 残留 → 静默关闭 → 未还原数。总错误数上升不一定是退步，可能是语义检查变深了。

### `helpers` — 运行时辅助函数识别

IL2CPP 的 `il2cpp_codegen_initialize_method`、写屏障、接口分发等**不属于业务逻辑**，但它们的地址每个包都不一样，硬编码就等于把工具绑死在一个游戏上。三种识别方式：

```bash
# 1) 有符号表（最准）：nm -D libil2cpp.so > symbols.txt
il2cpp-forge helpers --symbols symbols.txt --out index/helpers.json

# 2) 没有符号表：从反编译语料里按调用形状统计推断
il2cpp-forge helpers --learn --out index/helpers.json

# 3) 手工：直接写 helpers.json
```

形状推断的规则是可解释的：紧跟在 `if ((byte_X & 1) == 0)` 之后、首参是 `&Method_...` 的 → 类初始化；单参数为 `&...fields.X` 的 → 写屏障；出现在 `_d__N__MoveNext` 里的 → await 调度。置信度低于阈值就不写，宁可少剔除。

---

## 5. 配置

`forge.json`（相对路径按配置文件所在目录解析，整个项目可以整体搬走）：

```json
{
  "project": { "name": "MyGame", "evidence_banner": "UNRESTORED EVIDENCE" },
  "input":   { "apk": "", "libil2cpp": "", "metadata": "" },
  "dump":    { "dump_cs": "dump/dump.cs", "script_json": "dump/script.json",
               "dummy_dll_dir": "dump/DummyDll", "skeleton_src": "" },
  "native":  { "c_dir": "native/split-c", "helper_file": "index/helpers.json",
               "target_assemblies": ["Assembly-CSharp.dll"] },
  "source":  { "src_dir": "work/src" },
  "index":   { "dir": "index" },
  "build":   { "dotnet": "dotnet", "target_framework": "netstandard2.1" },
  "gates":   { "min_coverage": 0.6, "max_syntax_errors": 0, "max_residue": 0 }
}
```

`evidence_banner` 可以改：不同批次的工具会写 `UNRESTORED EVIDENCE` / `NATIVE EVIDENCE` 等不同横幅，判定按"注释横幅行"匹配，不会因为正文里提了一句就误判（这个坑踩过：机械翻译头里的散文会把整个后续代码吞进证据块，导致 190 个成员的 `throw`/`return` 被静默删掉）。

---

## 6. 证据模型（为什么这些规则是这样）

1. **锚点只取业务调用**。native 里是 `System_String__op_Equality`，C# 写的是 `==`；把框架调用算进覆盖率，正确实现也会被判不及格。工具用 `symbols.json` 判断"这个 mangled 类型属于目标程序集还是框架"，而不是硬编码库名前缀。
2. **只统计被写入的字段**。只读字段可能只是方法接收者（`flick = this.fields._flick` 然后 `flick.DisableFlick()`），也可能是反编译器的寄存器噪声，文本上无法区分。
3. **排除自身方法名、泛型尾巴、编译器后备字段、结构体槽位**。`Foo_Int32Enum_` 的尾巴是类型参数不是调用；`v.fields.mode` 是 ValueTuple/awaiter 的槽位，C# 永远写不出这个名字。
4. **async 必须跟进 `MoveNext`**。`FooAsync` 的 native 体只是个 builder trampoline，真正的逻辑在 `Foo_d__N__MoveNext` 里；不跟进就会把每个 async 方法判成空壳。
5. **`StringLiteral_N` 必须解析**。解析不了就让模型别猜文本，而不是让它编一个用户可见的字符串。

---

## 7. 目录结构

```
il2cpp-forge/
├── forge/
│   ├── cli.py                # 所有子命令
│   ├── config.py             # forge.json 模型
│   ├── unity/                # apk/xapk 定位、global-metadata 解析
│   ├── dump/                 # dump.cs 解析、dumper 调用
│   ├── index/                # 索引、script.json 对齐、image base 投票
│   ├── native/               # 反编译语料索引、语句化/精简、锚点、运行时辅助识别
│   ├── csharp/               # C# 成员定位、证据块解剖、重排
│   ├── restore/              # 队列、证据卡、落地、验证
│   ├── build/                # 诊断解析、csproj 生成、残留扫描、门禁
│   ├── report/               # 状态报告
│   └── prompts/              # 给模型的还原/审计契约（随包发布）
├── skills/il2cpp-forge/      # Agent Skill 入口
├── AGENTS.md                 # Agent 操作契约（先读这个）
├── docs/ARCHITECTURE.md      # 设计取舍
├── docs/MIGRATION_FROM_V5.md # 旧脚本 → 新命令对照
├── examples/forge.example.json
├── tests/                    # 标准库 unittest
├── .github/workflows/ci.yml  # 测试 + 打包断言
├── pyproject.toml
└── LICENSE                   # MIT
```

**仓库里不放任何游戏数据。** `forge.json`、`index/`、`dump/`、`cards/`、`native/`、`build/`、`*.evidence.json` 全部在 `.gitignore` 里——它们描述的是某一次具体工作，不是工具本身。

---

## 8. 已知限制

- **不生成 C# 本身**。还原由模型/人完成，工具负责提供证据、落地和判定——这是刻意的：没有证据的自动生成只会产出看起来对的代码。
- **native 语料需要先导出**。工具消费「一个函数一个块」的 `.c` 文件（IDA 按类导出、Ghidra 批量导出都可以），不自己驱动反编译器。
- **反编译器方言目前只适配了 IDA/Hex-Rays**。函数定位本身与方言无关（按 mangled 名 + 花括号匹配），但精简/锚点里有 4 处 IDA 特有假设：临时变量 `v123`、`_OWORD/_QWORD/LOWORD` 宏、`__fastcall`、`recv->klass->vtable._NN_name.methodPtr`。换 Ghidra 需要改 `forge/native/decompiled.py` 的这几条正则和 `forge/native/helpers.py` 的 `C_RUNTIME` 表；其中 vtable 一条会退化（Ghidra 渲染成 `*(code**)(klass+off)`，认不出槽位名，`set_text` 这类虚拟调用会掉回"管道"）。其余假设（`fields.`、`__1__state`、`__t__builder`、`_TypeInfo`）是 IL2CPP 语义，换工具不用动。
- **只在 Unity IL2CPP / ARM64 / Il2CppDumper 上实测过**。Cpp2IL 的 dump 格式、iOS 二进制、32 位 ABI 未验证。
- **image base 需要可对齐的样本**。样本不足时返回 `0x0` 并在 `join.json` 里如实记录，不猜。
- **覆盖率不是证明**。`verify` 通过 ≠ 行为等价；它只排除"明显没写"。
- **泛型共享代码**只能给出候选：一个 native 函数可能对应多个托管实例化，工具不会编造一对一映射。同理，`(方法名, 参数个数)` 收敛不到唯一 owner 时，工具**不建 native 链接**并标 `ambiguous:N`，而不是挑第一个。

---

## 9. 发布到 GitHub

仓库本身已经就绪（代码、文档、测试、CI、LICENSE 都在，且不含任何游戏数据），推之前只要替换 3 处占位：

| 位置 | 改什么 |
| --- | --- |
| `pyproject.toml` → `[project.urls]` | 两处 `YOUR-NAME` |
| `README.md` → 第 2 节的 `git clone` / `pipx` 命令 | 两处 `YOUR-NAME` |
| `LICENSE` 第一行 | 想署个人/组织名时改 |

```bash
git init -b main
git add -A
git commit -m "il2cpp-forge: evidence-gated C# reconstruction for Unity IL2CPP"
git remote add origin git@github.com:YOUR-NAME/il2cpp-forge.git
git push -u origin main
```

CI 会跑全部单元测试（4 个 Python 版本 × Linux/Windows），并断言 wheel 里带着 `forge/prompts/*.md`——卡片缺了契约，模型就会自己编规则。

将来若要改名，集中在 4 处：`pyproject.toml` 的 `name` 和 `[project.scripts]`、`forge/cli.py` 里的 `prog=`、README 标题。

---

## 10. 授权

只处理你有权分析的二进制。不要上传原始二进制、导出或反编译产物到任何第三方服务。第三方工具（Il2CppDumper / Cpp2IL / IDA / Ghidra）各自遵循其许可证。
