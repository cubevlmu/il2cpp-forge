"""Find the IL2CPP payload inside an APK / XAPK / split-APK set / loose folder.

Unity Android builds keep the two files that matter in predictable places::

    lib/<abi>/libil2cpp.so
    assets/bin/Data/Managed/Metadata/global-metadata.dat

An XAPK (or an ``.apks`` bundle) is a zip of APKs, so the scan recurses one
level into nested archives.  Split APKs are handled by scanning *all* archives
and merging what each one contributes: the base APK may carry the metadata while
an ABI split carries the library.
"""
from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .metadata import detect_unity_version, read_header

ABI_PREFERENCE = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
LIB_NAMES = ("libil2cpp.so",)
METADATA_NAMES = ("global-metadata.dat",)
UNITY_VERSION_SOURCES = ("globalgamemanagers", "data.unity3d", "unity default resources")


@dataclass
class Payload:
    source: Path
    libil2cpp: Optional[Path] = None
    metadata: Optional[Path] = None
    abi: str = ""
    unity_version: str = ""
    metadata_version: int = 0
    unity_hint: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.libil2cpp and self.metadata)

    def as_dict(self) -> dict:
        return {
            "source": str(self.source),
            "libil2cpp": str(self.libil2cpp) if self.libil2cpp else "",
            "metadata": str(self.metadata) if self.metadata else "",
            "abi": self.abi,
            "unity_version": self.unity_version,
            "metadata_version": self.metadata_version,
            "unity_hint": self.unity_hint,
            "complete": self.complete,
            "notes": self.notes,
        }


def _abi_rank(name: str) -> int:
    for position, abi in enumerate(ABI_PREFERENCE):
        if abi in name:
            return position
    return len(ABI_PREFERENCE)


def _pick(names: Iterable[str], suffixes: tuple[str, ...]) -> List[str]:
    return sorted(
        (n for n in names if n.replace("\\", "/").rsplit("/", 1)[-1] in suffixes),
        key=_abi_rank,
    )


def _scan_zip(archive: zipfile.ZipFile, prefix: str, depth: int) -> dict:
    """Collect candidate entries from one archive (recursing into nested APKs)."""
    found: dict = {"libs": [], "metadata": [], "version": [], "nested": []}
    for name in archive.namelist():
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if base in LIB_NAMES:
            found["libs"].append(prefix + name)
        elif base in METADATA_NAMES:
            found["metadata"].append(prefix + name)
        elif base in UNITY_VERSION_SOURCES:
            found["version"].append(prefix + name)
        elif depth < 2 and name.lower().endswith(".apk"):
            found["nested"].append(prefix + name)
    return found


def _read_nested(archive: zipfile.ZipFile, name: str, depth: int) -> dict:
    try:
        blob = archive.read(name)
        with zipfile.ZipFile(io.BytesIO(blob)) as inner:
            return _scan_zip(inner, prefix=f"{name}!", depth=depth + 1)
    except (KeyError, zipfile.BadZipFile):
        return {"libs": [], "metadata": [], "version": [], "nested": []}


def _extract(archive: zipfile.ZipFile, entry: str, destination: Path) -> Optional[Path]:
    """Copy one entry out, following ``outer.apk!inner/path`` chains."""
    parts = entry.split("!")
    blob: Optional[bytes] = None
    if len(parts) == 1:
        try:
            blob = archive.read(entry)
        except KeyError:
            return None
    else:
        try:
            blob = archive.read(parts[0])
            for index, part in enumerate(parts[1:], start=1):
                with zipfile.ZipFile(io.BytesIO(blob)) as inner:
                    if index == len(parts) - 1:
                        blob = inner.read(part)
                    else:
                        blob = inner.read(part)
        except (KeyError, zipfile.BadZipFile):
            return None
    if blob is None:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(blob)
    return destination


def locate(
    source: Path | str,
    workdir: Path | str,
    prefer_abi: tuple[str, ...] = ABI_PREFERENCE,
    extract: bool = True,
) -> Payload:
    source = Path(source)
    workdir = Path(workdir)
    payload = Payload(source=source)

    if source.is_dir():
        libs = [p for p in source.rglob("libil2cpp.so")]
        metas = [p for p in source.rglob("global-metadata.dat")]
        if libs:
            libs.sort(key=lambda p: _abi_rank(str(p)))
            payload.libil2cpp = libs[0]
            payload.abi = next((abi for abi in ABI_PREFERENCE if abi in str(libs[0])), "")
        if metas:
            payload.metadata = metas[0]
        versions = [p for p in source.rglob("*") if p.name in UNITY_VERSION_SOURCES]
        payload.unity_version = detect_unity_version(*versions, *libs)
        payload.notes.append(f"scanned directory {source}")
    elif zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            found = _scan_zip(archive, "", 0)
            for nested in found["nested"]:
                inner = _read_nested(archive, nested, 0)
                for key in ("libs", "metadata", "version"):
                    found[key].extend(inner[key])
            libs = sorted(set(found["libs"]), key=_abi_rank)
            metas = sorted(set(found["metadata"]))
            if not libs:
                payload.notes.append("no libil2cpp.so found in archive")
            if not metas:
                payload.notes.append("no global-metadata.dat found in archive")
            if extract and libs:
                chosen = libs[0]
                payload.abi = next((abi for abi in prefer_abi if abi in chosen), "")
                target = workdir / "input" / (payload.abi or "lib") / "libil2cpp.so"
                payload.libil2cpp = _extract(archive, chosen, target)
            elif libs:
                payload.abi = next((abi for abi in prefer_abi if abi in libs[0]), "")
            if extract and metas:
                target = workdir / "input" / "metadata" / "global-metadata.dat"
                payload.metadata = _extract(archive, metas[0], target)
            version_entry = found["version"][0] if found["version"] else ""
            version_blob = b""
            if version_entry:
                try:
                    version_blob = archive.read(version_entry)
                except KeyError:
                    version_blob = b""
            if version_blob:
                import re

                match = re.search(rb"(20\d\d\.\d+\.\d+[abfp]\d+)", version_blob)
                payload.unity_version = match.group(1).decode("ascii") if match else ""
    else:
        payload.notes.append(f"{source} is neither a directory nor a zip archive")
        if source.name == "libil2cpp.so":
            payload.libil2cpp = source
        if source.name == "global-metadata.dat":
            payload.metadata = source

    if payload.metadata:
        try:
            header = read_header(payload.metadata)
            payload.metadata_version = header.version
            payload.unity_hint = header.unity_hint
        except (OSError, ValueError) as error:
            payload.notes.append(f"metadata header unreadable: {error}")

    return payload


def stage(source: Path | str, workdir: Path | str) -> Payload:
    """Locate, extract and return a payload whose paths live under ``workdir``."""
    return locate(source, workdir, extract=True)


def copy_into(path: Path | str, destination: Path | str) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination
