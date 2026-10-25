"""Small shared helpers: JSON/JSONL IO, hashing, console output."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, List


def read_json(path: Path | str, default: Any = None) -> Any:
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path | str, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_jsonl(path: Path | str) -> Iterator[dict]:
    path = Path(path)
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path | str, rows: Iterable[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path | str, base: Path | str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def iter_files(root: Path | str, suffixes: tuple[str, ...]) -> List[Path]:
    root = Path(root)
    out: List[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            out.append(path)
    return out


def info(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    raise SystemExit(f"error: {message}")


def human_int(value: int) -> str:
    return f"{value:,}"
