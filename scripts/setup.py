"""Create a private .env with independent random secrets; safely repeatable."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from common import ROOT, SECRET_KEYS, is_placeholder, parse_env


def restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    # Apply the ACL before writing secrets into the temporary file.
    identity = subprocess.run(["whoami"], check=True, capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{identity}:(F)"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("Cannot restrict .env permissions. Use a local NTFS directory with working icacls permissions.")


def write_private(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink for the environment file.")
    if not path.parent.is_dir():
        raise ValueError("The environment file's parent directory must already exist.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        restrict_permissions(temporary)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure(path: Path, rotate: bool = False) -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    defaults = parse_env(template)
    if path.is_symlink():
        raise ValueError("Refusing to use a symlink for the environment file.")
    existing = path.exists()
    content = path.read_text(encoding="utf-8-sig") if existing else template
    values = parse_env(content)
    for key, value in defaults.items():
        if key not in values:
            content = content.rstrip() + f"\n{key}={value}\n"
            values[key] = value

    kept: set[str] = set()
    for key in SECRET_KEYS:
        value = values[key]
        if not rotate and not is_placeholder(value):
            if len(value) < 32 or any(character.isspace() for character in value):
                raise ValueError(f"{key} is too short or contains whitespace. Use --rotate-secrets to replace all three secrets.")
            if value in kept:
                raise ValueError("Secrets must be distinct. Use --rotate-secrets to replace all three secrets.")
            kept.add(value)

    generated = 0
    for key in SECRET_KEYS:
        if rotate or is_placeholder(values[key]):
            value = secrets.token_hex(32)
            while value in kept:
                value = secrets.token_hex(32)
            kept.add(value)
            values[key] = value
            content = re.sub(rf"^{re.escape(key)}\s*=.*$", f"{key}={value}", content, flags=re.MULTILINE)
            generated += 1

    write_private(path, content.rstrip() + "\n")
    action = "Updated" if existing else "Created"
    print(f"{action} private environment file: {path}")
    print(f"Generated {generated} secret(s); existing secrets are preserved unless --rotate-secrets is used.")
    print("Next: docker compose config --quiet; then docker compose up -d --build.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--rotate-secrets", action="store_true", help="Replace all three secrets. Recreate containers and update API clients afterward.")
    args = parser.parse_args()
    try:
        configure(args.env_file.absolute(), args.rotate_secrets)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
