"""Package a committed source tree; never start the bot or read runtime state."""

import gzip
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed git argv, never a shell
        ["git", *args],  # noqa: S607 - standard Git executable
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def main() -> None:
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("Commit all tracked changes before packaging")
    version_match = re.search(
        r'^version = "([0-9.]+)"$', (ROOT / "pyproject.toml").read_text(), re.M
    )
    if version_match is None:
        raise SystemExit("Missing package version")
    version = version_match.group(1)
    commit = git("rev-parse", "HEAD").decode().strip()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    expected = [
        dist / f"nexa_guardian-{version}-py3-none-any.whl",
        dist / f"nexa_guardian-{version}.tar.gz",
        dist / "requirements-runtime.txt",
    ]
    if not all(path.is_file() for path in expected):
        raise SystemExit("Run uv build on this commit first")
    bundle = dist / f"nexa-guardian-{version}-deployment.tar.gz"
    source_tar = git("archive", "--format=tar", f"--prefix=nexa-guardian-{version}/", "HEAD")
    with (
        bundle.open("wb") as stream,
        gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed,
    ):
        compressed.write(source_tar)
    metadata = dist / "release-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "project": "nexa-guardian",
                "version": version,
                "source_commit": commit,
                "database_schema": 9,
                "deployment_performed": False,
            },
            indent=2,
        )
        + "\n"
    )
    assets = [*expected, bundle, metadata]
    (dist / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets
        )
    )
    print(f"Packaged {len(assets)} assets for {commit}")


if __name__ == "__main__":
    main()
