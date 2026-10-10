# Development and validation

Use Python 3.12 in the pinned Linux x86-64 gateway image. Production locks contain the exact wheels for that target; they are not portable Windows/ARM lockfiles.

```bash
docker build -t sourceferry-dev gateway
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/workspace:ro" -w /workspace sourceferry-dev python -B -m unittest discover -s tests -v
```

For lint and type checks, build the development stage with `docker build --target tools -t sourceferry-tools gateway`. Install `requirements-dev.txt` in that disposable environment with the gateway dependencies, then run `ruff check gateway scripts tests integration` and `mypy`. The runtime images remove pip after installing their dependencies; development tools belong in the tools stage. Run `shellcheck install.sh` and `bash -n install.sh`. The native Windows fixture extracts the real PowerShell functions, tests credential rotation and rerun preservation, and checks parser syntax without starting Docker:

```powershell
python -B -m unittest discover -s tests -p test_installation_windows.py -v
```

Run browser egress checks in the patched crawler image:

```bash
docker build -t sourceferry-crawler-dev crawler
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true --shm-size 1g -v "$PWD:/workspace:ro" --entrypoint python sourceferry-crawler-dev /workspace/integration/check_egress.py
```

Refresh dependency locks only in the target Linux/Python environment: edit the appropriate `.in` file, run `python scripts/lock_dependencies.py gateway/requirements.in gateway/requirements.txt` (and the bootstrap/crawler equivalents), review every changed version/hash, then rebuild and test. The resolver downloads wheels and writes their SHA-256 hashes. Do not add unpinned network installation steps to production Dockerfiles.

Review `crawler/os-packages.txt` with each crawler-base upgrade. These exact Debian versions must exist in the base's repositories. Keep the package list consistent with the base's Debian release and validate every version change with a rebuild.

Use `python scripts/package.py` to build the release ZIP and checksum. Its explicit allowlist must include each new runtime module and required operational document. Test the extracted ZIP, not only the checkout. Never package `.env`, local reports containing secrets, caches, or local environments.

After changes affecting deployment or upstream versions, run the installer against an isolated project and repeat live verification. Record the checks in `VALIDATION.md`. Offline tests do not establish current search-engine availability; external engines and sites can change between runs.
