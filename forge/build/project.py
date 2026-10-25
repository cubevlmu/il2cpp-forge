"""Compile the reconstructed tree as a diagnostic.

Compiling proves syntax and typing.  It never proves behaviour -- but it is the
cheapest way to find the bodies that are structurally broken, and a body that
does not parse cannot be reviewed at all.

The generated project compiles the tree against *metadata-only* reference
assemblies (Il2CppDumper's ``DummyDll``).  Assemblies that are being rebuilt as
source are excluded, otherwise every type exists twice and the compiler reports
CS0433 for the whole tree instead of the real errors.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .diagnostics import BuildReport, parse_output, scan_residue

CSPROJ_TEMPLATE = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>{framework}</TargetFramework>
    <LangVersion>{langversion}</LangVersion>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <Nullable>disable</Nullable>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <Deterministic>true</Deterministic>
    <DebugType>none</DebugType>
    <ProduceReferenceAssembly>false</ProduceReferenceAssembly>
    <NoWarn>CS0169;CS0414;CS0649;CS0067;CS0168;CS0219;CS8632;CS0108;CS0114</NoWarn>
    <WarningLevel>0</WarningLevel>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{sources}" />
  </ItemGroup>
  <ItemGroup>
{references}
  </ItemGroup>
</Project>
"""


@dataclass
class BuildResult:
    csproj: Path
    exit_code: int
    report: BuildReport
    output: str = ""

    def as_dict(self) -> dict:
        payload = self.report.as_dict()
        payload["csproj"] = str(self.csproj)
        payload["exit_code"] = self.exit_code
        return payload


def generate_csproj(
    src_dir: Path | str,
    reference_dir: Path | str | None,
    out_path: Path | str,
    exclude: Iterable[str] = (),
    framework: str = "netstandard2.1",
    langversion: str = "latest",
) -> Path:
    src_dir = Path(src_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    excluded = {name.lower() for name in exclude}
    references: List[str] = []
    if reference_dir:
        reference_dir = Path(reference_dir)
        if reference_dir.is_dir():
            for dll in sorted(reference_dir.glob("*.dll")):
                if dll.stem.lower() in excluded:
                    continue
                relative = _relative_hint(out_path.parent, dll)
                references.append(
                    f'    <Reference Include="{dll.stem}">'
                    f"<HintPath>{relative}</HintPath><Private>false</Private></Reference>"
                )
    sources = _relative_hint(out_path.parent, src_dir / "**" / "*.cs")
    out_path.write_text(
        CSPROJ_TEMPLATE.format(
            framework=framework,
            langversion=langversion,
            sources=sources,
            references="\n".join(references) if references else "    <!-- no reference assemblies -->",
        ),
        encoding="utf-8",
    )
    return out_path


def _relative_hint(base: Path, target: Path) -> str:
    try:
        return str(target.resolve().relative_to(base.resolve())).replace("/", "\\")
    except ValueError:
        return str(target.resolve())


def run_build(
    csproj: Path | str,
    dotnet: str = "dotnet",
    timeout: int = 2400,
    no_incremental: bool = True,
) -> BuildResult:
    csproj = Path(csproj)
    executable = shutil.which(dotnet) or dotnet
    command = [executable, "build", str(csproj), "-v", "q", "--nologo"]
    if no_incremental:
        command.append("--no-incremental")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        code = completed.returncode
    except FileNotFoundError:
        output = f"error: dotnet executable not found ({dotnet})"
        code = 127
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or "") if isinstance(expired.stdout, str) else ""
        output += f"\nerror: build timed out after {timeout}s"
        code = 124
    report = parse_output(output)
    return BuildResult(csproj=csproj, exit_code=code, report=report, output=output)


def compile_tree(
    src_dir: Path | str,
    reference_dir: Path | str | None,
    out_path: Path | str,
    exclude: Iterable[str] = (),
    framework: str = "netstandard2.1",
    dotnet: str = "dotnet",
    timeout: int = 2400,
    residue: bool = True,
    langversion: str = "latest",
) -> BuildResult:
    csproj = generate_csproj(src_dir, reference_dir, out_path, exclude, framework, langversion)
    result = run_build(csproj, dotnet=dotnet, timeout=timeout)
    if residue:
        result.report.as_dict()["residue"] = scan_residue(src_dir)
    return result


def residue_of(src_dir: Path | str) -> Dict[str, int]:
    return scan_residue(src_dir)
