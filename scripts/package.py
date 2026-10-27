"""Package only reviewed manifest entries; never archive the working directory."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import zipfile


def package(root: Path, output: Path):
    root = root.resolve()
    manifest = root / "docs" / "PUBLISH_MANIFEST.txt"
    paths = [line.strip() for line in manifest.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.startswith("#")]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate publish manifest entry")
    verified = []
    for name in paths:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("Publish manifest must contain safe relative paths")
        if any(part in {".git", "__pycache__", ".state", "state", "data", "work", "logs"} for part in relative.parts):
            raise ValueError("Private or generated directory in publish manifest")
        candidate = root.joinpath(*relative.parts)
        if not candidate.is_file() or candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            raise ValueError("Publish entry is missing, not a file, or escapes the source root")
        if candidate.suffix.lower() in {".pyc", ".log", ".sqlite", ".sqlite3", ".db", ".token"} or candidate.name.startswith(".env"):
            raise ValueError("Runtime state or credentials are not publishable")
        content = candidate.read_bytes()
        content.decode("utf-8-sig")  # Only text source, not binaries or client bundles.
        verified.append((name, content))
    if output.exists():
        raise FileExistsError("Refusing to replace an existing release archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in verified:
            info = zipfile.ZipInfo("workbuddy-manager-gateway/" + name, (2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return {"files": len(verified), "sha256": digest, "archive": output.name}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parent.parent, arguments.output)))
