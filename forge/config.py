"""Project configuration.

A forge project is one JSON file (``forge.json`` by default) plus the paths it
points at.  Nothing about a specific game, module, Unity version or RVA is
compiled into the tool: every such value is either discovered at run time or
written into this file by ``forge detect``/``forge index``.

Path resolution rule: relative paths are resolved against the directory that
holds the config file, so a project directory can be moved or copied to another
machine without editing anything.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_NAMES = ("forge.json", "il2cpp-forge.json", ".il2cpp-forge.json")

DEFAULT_EVIDENCE_BANNER = "UNRESTORED EVIDENCE"

# Markers that mean "this body is not finished".  ``INFER:`` is deliberately
# absent: it records an accepted inference, not an open item.
UNFINISHED_MARKER_WORDS = (
    "PARTIAL",
    "DRAFT",
    "ASSUMED",
    "INFER_RESTORE",
    "INFERRED",
    "TODO",
    "FIXME",
    "UNFINISHED",
    "WIP",
    "NOT_SUPPORTED",
)
ACCEPTED_MARKER_WORDS = ("INFER", "INFERRED_DETAIL")

DEFAULTS: Dict[str, Any] = {
    "project": {
        "name": "",
        "evidence_banner": DEFAULT_EVIDENCE_BANNER,
    },
    "input": {
        "apk": "",
        "libil2cpp": "",
        "metadata": "",
    },
    "dump": {
        "tool": "auto",
        "dump_cs": "",
        "script_json": "",
        "stringliteral_json": "",
        "skeleton_src": "",
        "dummy_dll_dir": "",
    },
    "native": {
        "c_dir": "",
        "helper_file": "",
        "framework_prefixes": [],
        "target_assemblies": ["Assembly-CSharp.dll"],
    },
    "source": {
        "src_dir": "",
    },
    "index": {
        "dir": "",
    },
    "build": {
        "csproj": "",
        "reference_dir": "",
        "dotnet": "dotnet",
        "target_framework": "netstandard2.1",
        "extra_props": {},
    },
    "gates": {
        "min_coverage": 0.6,
        "max_syntax_errors": 0,
        "check_residue": True,
        "max_residue": 0,
        "require_evidence_removal": True,
        "allow_mechanical_leak": False,
    },
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    """Resolved project configuration."""

    path: Optional[Path] = None
    data: Dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, path: Path | str) -> "Config":
        path = Path(path).expanduser().resolve()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return cls(path=path, data=_merge(DEFAULTS, raw))

    @classmethod
    def discover(cls, start: Path | str | None = None) -> "Config":
        """Find ``forge.json`` walking upwards; return defaults when absent."""
        here = Path(start or os.getcwd()).expanduser().resolve()
        for folder in [here, *here.parents]:
            for name in CONFIG_NAMES:
                candidate = folder / name
                if candidate.is_file():
                    return cls.load(candidate)
        return cls(path=None)

    # -- access -----------------------------------------------------------
    @property
    def root(self) -> Path:
        if self.path is not None:
            return self.path.parent
        return Path.cwd()

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.data.get(name, {}))

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def path_of(self, dotted: str, must_exist: bool = False) -> Optional[Path]:
        """Resolve a configured path against the config directory."""
        value = self.get(dotted, "")
        if not value:
            return None
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if must_exist and not candidate.exists():
            return None
        return candidate

    # -- mutation ---------------------------------------------------------
    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path else (self.path or (self.root / "forge.json"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.path = target.resolve()
        return self.path

    # -- derived helpers ---------------------------------------------------
    @property
    def framework_prefixes(self) -> List[str]:
        """Type-name prefixes whose calls can never appear verbatim in C#.

        Auto-derived from the dump's image list by ``forge index`` (every image
        that is not a target assembly), overridable in the config.
        """
        configured = [p for p in self.get("native.framework_prefixes", []) if p]
        if configured:
            return configured
        return []

    @property
    def target_assemblies(self) -> List[str]:
        return list(self.get("native.target_assemblies", ["Assembly-CSharp.dll"]))

    @property
    def evidence_banner(self) -> str:
        return str(self.get("project.evidence_banner", DEFAULT_EVIDENCE_BANNER))
