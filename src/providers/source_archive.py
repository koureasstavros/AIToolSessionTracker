"""Safe archive helpers for provider transcript source files."""
from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

MANIFEST = "manifest.json"


def create_archive(provider: str, archive: Path, sources: Iterable[tuple[Path, str]]) -> Path:
    """Archive source files and directories with their provider-relative targets."""
    entries: list[dict[str, str]] = []
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for source, target in sources:
            source = source.resolve()
            if source.is_dir():
                files = sorted(path for path in source.rglob("*") if path.is_file())
                base = source.parent
            elif source.is_file():
                files = [source]
                base = source.parent
            else:
                continue
            for path in files:
                relative = path.relative_to(base).as_posix()
                member = f"sources/{len(entries):06d}-{Path(relative).name}"
                output.write(path, member)
                entries.append({"member": member, "target": (PurePosixPath(target) / relative).as_posix()})
        output.writestr(MANIFEST, json.dumps({"version": 1, "provider": provider, "files": entries}, indent=2))
    return archive


def inject_archive(provider: str, archive: Path, destination: Path) -> list[Path]:
    """Inject an archive into a provider directory, rejecting unsafe paths."""
    with zipfile.ZipFile(archive) as source:
        try:
            manifest = json.loads(source.read(MANIFEST))
        except (KeyError, json.JSONDecodeError) as error:
            raise ValueError("Archive does not contain a valid manifest") from error
        if manifest.get("provider") != provider:
            raise ValueError("Archive belongs to a different provider")
        written: list[Path] = []
        for entry in manifest.get("files", []):
            member = entry.get("member")
            target = entry.get("target")
            if not isinstance(member, str) or not isinstance(target, str):
                raise ValueError("Archive manifest contains an invalid file entry")
            relative = PurePosixPath(target)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Archive contains an unsafe target path")
            target_path = destination / Path(*relative.parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                target_path = target_path.with_name(f"{target_path.stem}-{uuid.uuid4().hex[:8]}{target_path.suffix}")
            with source.open(member) as input_file, target_path.open("wb") as output_file:
                output_file.write(input_file.read())
            written.append(target_path)
        return written
