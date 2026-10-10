"""Exercise secret setup and the supplied command-line clients without the web."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import api
import common
import package
import setup as installation
import smoke_test


class InstallationTests(unittest.TestCase):
    def test_indented_quoted_commented_crlf_credentials_are_rotated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            original = ''.join(f' \t{key} = "{index * 64}" # keep {key}\r\n'
                               for key, index in zip(common.SECRET_KEYS, 'abc'))
            path.write_bytes(original.encode())
            with contextlib.redirect_stdout(io.StringIO()):
                installation.configure(path, rotate=True)
            changed = common.read_env(path)
            for key, index in zip(common.SECRET_KEYS, 'abc'):
                self.assertNotEqual(changed[key], index * 64)
                self.assertRegex(changed[key], r'^[0-9a-f]{64}$')
                self.assertIn(f'# keep {key}', path.read_text())
            self.assertEqual(len({changed[key] for key in common.SECRET_KEYS}), 3)

    def test_literal_environment_comments_quotes_and_bad_quotes(self):
        self.assertEqual(common.parse_env(' A="value # literal" # comment\r\n\tB=plain # tail\n'),
                         {'A': 'value # literal', 'B': 'plain'})
        for text in ('A="unfinished', 'A="value"oops'):
            with self.assertRaises(ValueError):
                common.parse_env(text)

    def test_generated_secrets_are_private_distinct_and_preserved_on_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                installation.configure(path)
                first = common.read_env(path)
                installation.configure(path)
            self.assertEqual(first, common.read_env(path))
            secrets = [first[key] for key in common.SECRET_KEYS]
            self.assertEqual(len(set(secrets)), 3)
            for value in secrets:
                self.assertRegex(value, r"^[0-9a-f]{64}$")
                self.assertNotIn(value, output.getvalue())
            self.assertEqual(first["ENRICHMENT_CONCURRENCY"], "4")
            self.assertEqual(first["SNIPPET_MAX_CHARS"], "1200")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rotation_replaces_secrets_and_preserves_custom_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with contextlib.redirect_stdout(io.StringIO()):
                installation.configure(path)
                first = common.read_env(path)
                content = path.read_text().replace("GATEWAY_PORT=8080", "GATEWAY_PORT=9080")
                path.write_text(content, encoding="utf-8")
                installation.configure(path, rotate=True)
            second = common.read_env(path)
            self.assertEqual(second["GATEWAY_PORT"], "9080")
            for key in common.SECRET_KEYS:
                self.assertNotEqual(first[key], second[key])

    def test_duplicate_env_settings_are_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = "LOCAL_WEB_API_TOKEN=first\nLOCAL_WEB_API_TOKEN=second\n"
            path.write_text(original)
            with self.assertRaises(ValueError):
                installation.configure(path)
            self.assertEqual(path.read_text(), original)

    def test_duplicate_secrets_are_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = "\n".join(f"{key}={'a' * 64}" for key in common.SECRET_KEYS) + "\n"
            path.write_text(original)
            with self.assertRaises(ValueError):
                installation.configure(path)
            self.assertEqual(path.read_text(), original)


class ReleasePackageTests(unittest.TestCase):
    def test_release_archive_and_checksum_are_readable_by_other_users(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sourceferry.zip'
            previous_umask = os.umask(0o077) if os.name != 'nt' else None
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    package.build(output)
            finally:
                if previous_umask is not None:
                    os.umask(previous_umask)
            checksum = output.with_suffix('.zip.sha256')
            if os.name != 'nt':
                for path in (output, checksum):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual(checksum.read_text(), f'{digest}  {output.name}\n')
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('sourceferry/install.sh', archive.namelist())
                self.assertNotIn('sourceferry/.env', archive.namelist())


class ClientTests(unittest.TestCase):
    def test_clients_load_token_from_env_and_smoke_check_the_full_contract(self):
        token = "b" * 64
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.respond(200, {"status": "ok"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                authenticated = self.headers.get("Authorization") == f"Bearer {token}"
                observed.append((self.path, authenticated))
                if not authenticated:
                    self.respond(401, {"detail": "Unauthorized"})
                elif self.path == "/fetch":
                    self.respond(200, {
                        "url": payload["url"],
                        "content": {"success": True, "markdown": "# Example Domain\nExample content."},
                        "truncated": False,
                    })
                else:
                    self.respond(200, {
                        "sources": [{"url": "https://example.com", "title": "Example", "snippet": "Example content."}],
                        "truncated": False,
                    })

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"LOCAL_WEB_API_TOKEN={token}\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f"http://127.0.0.1:{server.server_port}"
            try:
                # Environment settings in the user's session must not affect fixtures.
                # Windows SSL initialization needs SystemRoot even for a local
                # HTTP fixture because urllib constructs its HTTPS handler too.
                system_environment = {key: value for key, value in os.environ.items()
                                      if key.upper() in {'SYSTEMROOT', 'WINDIR'}}
                with patch.dict(os.environ, system_environment, clear=True):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        with patch.object(sys, "argv", ["smoke_test.py", "--env-file", str(path), "--base-url", address, "--wait-seconds", "0"]):
                            self.assertEqual(smoke_test.main(), 0)
                        with patch.object(sys, "argv", ["api.py", "--env-file", str(path), "--base-url", address, "fetch", "https://example.com"]):
                            self.assertEqual(api.main(), 0)
                    self.assertIn("Smoke checks passed", output.getvalue())
                    self.assertNotIn(token, output.getvalue())
                self.assertIn(("/search", False), observed)
                self.assertIn(("/fetch", False), observed)
                self.assertIn(("/search", True), observed)
                self.assertIn(("/fetch", True), observed)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
