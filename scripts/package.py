"""Build a deployable SourceFerry ZIP from an explicit allowlist; exclude local secrets."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from common import ROOT, SECRET_KEYS, is_placeholder, read_env

FILES = (
    "CONTRIBUTING.md",
    "pyproject.toml",
    "requirements-dev.txt",
    "docs/API.md",
    "docs/CONFIGURATION.md",
    "docs/OPERATIONS.md",
    "gateway/__init__.py",
    "gateway/contracts.py",
    "gateway/middleware.py",
    "gateway/requirements.in",
    "gateway/bootstrap.in",
    "gateway/bootstrap.txt",
    "crawler/Dockerfile",
    "crawler/.dockerignore",
    "crawler/requirements.in",
    "crawler/requirements.txt",
    "crawler/os-packages.txt",
    "scripts/lock_dependencies.py",
    "integration/check_egress.py",
    "tests/test_request_limits.py",
    "tests/test_installation_windows.py",

    ".env.example",
    ".gitignore",
    ".gitattributes",
    "docker-compose.yml",
    "install.sh",
    "install.ps1",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "VALIDATION.md",
    "gateway/.dockerignore",
    "gateway/Dockerfile",
    "gateway/app.py",
    "gateway/requirements.txt",
    "searxng/settings.yml",
    "scripts/api.py",
    "scripts/common.py",
    "scripts/install.py",
    "scripts/package.py",
    "scripts/setup.py",
    "scripts/smoke_test.py",
    "scripts/verify.py",
    "tests/test_gateway.py",
    "tests/test_installation.py",
    "tests/test_installer.py",
    "tests/test_verification.py",
    "verification/canary.html",
    "verification/cases.json",
    "third_party/SEARXNG-LICENSE.txt",
    "third_party/CRAWL4AI-LICENSE.txt",
)


def build(output: Path) -> None:
    root = ROOT.resolve()
    for name in FILES:
        source = ROOT / name
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe package source: {name}.")
    settings = read_env(ROOT / ".env.example")
    if any(not is_placeholder(settings.get(key, "")) for key in SECRET_KEYS):
        raise ValueError(".env.example contains a non-placeholder secret; refusing to package it.")
    if output.is_symlink() or output.resolve() in {(ROOT / name).resolve() for name in FILES}:
        raise ValueError("Output cannot overwrite a package source or symlink.")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="gateway-package-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(FILES):
                info = zipfile.ZipInfo(f"sourceferry/{name}", date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name == "install.sh" else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, (ROOT / name).read_bytes())
        # mkstemp creates a private 0600 file. This allowlisted release must be
        # readable by the host runner when a root container builds it.
        temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    checksum.chmod(0o644)
    print(f"Created {output} ({len(FILES)} allowlisted files)")
    print(f"Created {checksum}")
    print("Local .env, environments, caches, logs, and source notes are excluded.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "sourceferry.zip")
    args = parser.parse_args()
    try:
        build(args.output.absolute())
    except (OSError, ValueError) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
