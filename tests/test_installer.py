"""Installer contract checks using fake Docker commands, without starting services."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install


class InstallerHelperTests(unittest.TestCase):
    def test_engine_minimum_includes_localhost_publication_fix(self):
        for version in ('28.0.0', '29.7.2', '28.1.0-desktop.1'):
            install.validate_engine_version(version)
        for version in ('20.10.0', '27.5.1', 'garbage', '28.0'):
            with self.assertRaises(ValueError):
                install.validate_engine_version(version)

    def test_all_numeric_bounds_match_runtime_contract(self):
        from gateway.contracts import INTEGER_SETTINGS, FLOAT_SETTINGS, numeric_settings
        with tempfile.TemporaryDirectory() as directory:
            for key, (_, minimum, maximum) in INTEGER_SETTINGS.items():
                for value in (minimum, maximum):
                    values = {key: str(value)}
                    numeric_settings(values)
                    install.validate_environment(self.environment(directory, **values))
                for value in (minimum - 1, maximum + 1, '1.5', 'nan'):
                    values = {key: str(value)}
                    with self.assertRaises(ValueError):
                        numeric_settings(values)
                    with self.assertRaises(ValueError):
                        install.validate_environment(self.environment(directory, **values))
            for key, (_, maximum) in FLOAT_SETTINGS.items():
                for value in ('0', '-1', 'nan', 'inf', str(maximum + 1)):
                    with self.assertRaises(ValueError):
                        install.validate_environment(self.environment(directory, **{key: value}))

    def environment(self, directory, **overrides):
        path = Path(directory) / ".env"
        values = {"LOCAL_WEB_API_TOKEN": "a" * 64, "CRAWL4AI_API_TOKEN": "b" * 64, "SEARXNG_SECRET_KEY": "c" * 64,
                  "GATEWAY_BIND_ADDRESS": "127.0.0.1", "GATEWAY_PORT": "8080", "LOCAL_WEB_GATEWAY_URL": "http://127.0.0.1:8080"}
        values.update(overrides)
        path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
        return path

    def test_supported_compose_releases_and_old_versions(self):
        for version in ("2.24.0", "v2.24.1-desktop.1", "2.40.0", "5.0.0"):
            install.validate_compose_version(version)
        for version in ("2.20.0", "1.29.2", "garbage", "2.24"):
            with self.assertRaises(ValueError):
                install.validate_compose_version(version)

    def test_actual_resource_limits_and_cpu_features(self):
        install.validate_resources(install.MIN_MEMORY, install.MIN_CPUS, install.MIN_DISK)
        for memory, cpus, disk in ((install.MIN_MEMORY - 1, 2, install.MIN_DISK), (install.MIN_MEMORY, 1, install.MIN_DISK), (install.MIN_MEMORY, 2, install.MIN_DISK - 1)):
            with self.assertRaises(ValueError):
                install.validate_resources(memory, cpus, disk)
        install.validate_cpu("x86_64", "flags : fpu sse4_2 popcnt\nflags : fpu popcnt sse4_2\n")
        for machine, flags in (("aarch64", "flags : sse4_2 popcnt"), ("x86_64", "flags : popcnt"), ("x86_64", "flags : sse4_2 popcnt\nflags : sse4_2"), ("x86_64", "")):
            with self.assertRaises(ValueError):
                install.validate_cpu(machine, flags)

    def test_custom_port_and_wildcard_binding_generate_usable_host_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.environment(directory, GATEWAY_BIND_ADDRESS="0.0.0.0", GATEWAY_PORT="9080")
            values = install.validate_environment(path)
            self.assertEqual(values["host_url"], "http://127.0.0.1:9080")
            self.assertEqual(values["client_url"], values["host_url"])
            path = self.environment(directory, GATEWAY_BIND_ADDRESS="::", GATEWAY_PORT="9080", LOCAL_WEB_GATEWAY_URL="https://gateway.example.org")
            values = install.validate_environment(path)
            self.assertEqual(values["host_url"], "http://[::1]:9080")
            self.assertEqual(values["client_url"], "https://gateway.example.org")

    def test_configuration_validation_rejects_credentials_invalid_ports_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            for overrides in ({"GATEWAY_PORT": "0"}, {"GATEWAY_PORT": "65536"}, {"GATEWAY_BIND_ADDRESS": "localhost"}, {"LOCAL_WEB_API_TOKEN": "short"}, {"CRAWL4AI_API_TOKEN": "a" * 64}, {"LOCAL_WEB_GATEWAY_URL": "http://secret:password@example.org"}, {"SEARXNG_TIMEOUT_SECONDS": "nan"}, {"ENRICHMENT_CONCURRENCY": "0"}):
                path = self.environment(directory, **overrides)
                with self.assertRaises(ValueError):
                    install.validate_environment(path)

    def test_config_output_never_prints_secrets_and_token_command_only_prints_gateway_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.environment(directory)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with patch.object(sys, "argv", ["install.py", "--env-file", str(path), "config"]):
                    self.assertEqual(install.main(), 0)
            for token in ("a" * 64, "b" * 64, "c" * 64):
                self.assertNotIn(token, output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                install.show_connection(path)
            self.assertIn("a" * 64, output.getvalue())
            self.assertNotIn("b" * 64, output.getvalue())
            self.assertNotIn("c" * 64, output.getvalue())
            self.assertIn(".env", output.getvalue())
            self.assertIn("--show-token", output.getvalue())

    def test_report_preserves_verification_results_without_accepting_untrusted_stage_text(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "installation-report.json"
            install.safe_report(report, initialize=True)
            install.safe_report(report, stage="prerequisites", status="passed")
            values = json.loads(report.read_text())
            values["checks"] = [{"name": "live-search", "status": "passed"}]
            report.write_text(json.dumps(values))
            install.safe_report(report, stage="verification", status="verified")
            values = json.loads(report.read_text())
            self.assertEqual(values["installer_status"], "verified")
            self.assertEqual(values["checks"][0]["status"], "passed")
            with self.assertRaises(ValueError):
                install.safe_report(report, stage="token=a-secret", status="failed")
            with self.assertRaises(ValueError):
                install.safe_report(report, stage="a" * 64, status="failed")
            self.assertNotIn("a-secret", report.read_text())

    def test_invalid_env_is_reported_without_secondary_parse_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("invalid assignment", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as output:
                with patch.object(sys, "argv", ["install.py", "--env-file", str(path), "config"]):
                    self.assertEqual(install.main(), 1)
            self.assertIn("Invalid .env assignment", output.getvalue())


FAKE_DOCKER = r'''#!PYTHON
import json, os, pathlib, sys
a = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(a) + '\n')
if a[:1] == ['info']:
    print(os.environ.get('FAKE_DOCKER_INFO', 'linux|4|8589934592|29.7.2'))
elif a[:2] == ['context', 'inspect']:
    print(os.environ.get('FAKE_DOCKER_HOST', 'unix:///var/run/docker.sock'))
elif a[:2] == ['image', 'inspect']:
    print('sha256:tested-gateway-image')
elif a[:2] == ['compose', 'version']:
    print('2.24.0')
elif a[:1] == ['compose']:
    if 'images' in a:
        print('sha256:tested-gateway-image')
    elif 'port' in a:
        sys.exit(1)
elif a[:1] == ['run']:
    if '/workspace/scripts/verify.py' in a:
        sys.exit(9 if os.environ.get('FAKE_FAIL') == 'verification' else 0)
    if '/workspace/scripts/install.py' in a:
        p = a.index('/workspace/scripts/install.py') + 1
        command, rest = a[p], a[p+1:]
        if command == 'config':
            print('127.0.0.1\n8080\nhttp://127.0.0.1:8080\nhttp://127.0.0.1:8080')
        elif command == 'token':
            print('LOCAL_WEB_API_TOKEN=' + 'a' * 64)
        elif command == 'report':
            path = pathlib.Path('artifacts/installation-report.json')
            report = {'installer_stages': []} if '--initialize' in rest else json.loads(path.read_text())
            if '--status' in rest:
                status = rest[rest.index('--status') + 1]
                report['installer_stages'].append({'stage': rest[rest.index('--stage') + 1], 'status': status})
                if status != 'passed': report['installer_status'] = status
            path.write_text(json.dumps(report))
'''


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "Bash installer orchestration runs on Linux")
class BashInstallerTests(unittest.TestCase):
    def run_installer(self, *arguments, fail=None, docker_info=None, docker_host=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        shutil.copyfile(install.ROOT / "install.sh", root / "install.sh")
        executable = root / "docker"
        executable.write_text(FAKE_DOCKER.replace("#!PYTHON", f"#!{sys.executable}"), encoding="utf-8")
        executable.chmod(0o755)
        env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}", FAKE_DOCKER_LOG=str(root / "commands.jsonl"), COMPOSE_PROJECT_NAME="test-install")
        env.pop("DOCKER_HOST", None)
        env.pop("FAKE_FAIL", None)
        if docker_info:
            env['FAKE_DOCKER_INFO'] = docker_info
        if docker_host:
            env['FAKE_DOCKER_HOST'] = docker_host
        if fail:
            env["FAKE_FAIL"] = fail
        result = subprocess.run(["bash", str(root / "install.sh"), *arguments], env=env, capture_output=True, text=True, timeout=30)
        calls = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
        return result, calls, root

    def test_prerequisite_failures_record_a_terminal_status(self):
        for values in ({'docker_info': 'windows|4|8589934592|29.7.2'},
                       {'docker_host': 'tcp://remote:2376'}):
            result, calls, root = self.run_installer(**values)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads((root / 'artifacts/installation-report.json').read_text())
            self.assertEqual(report['installer_status'], 'failed')
            self.assertFalse(any('up' in call for call in calls))

    def test_rotation_flag_reaches_secret_setup(self):
        result, calls, _ = self.run_installer('--rotate-secrets')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/workspace/scripts/setup.py' in call and '--rotate-secrets' in call for call in calls))

    def test_install_honors_project_waits_runs_regressions_and_prints_token_last(self):
        result, calls, root = self.run_installer("--wait-timeout", "180")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any("--project-name" in call and "test-install" in call for call in calls))
        self.assertTrue(any("up" in call and "--wait" in call and "180" in call for call in calls))
        self.assertTrue(any("unittest" in call for call in calls))
        self.assertEqual(calls[-1][-1], "token")
        self.assertIn("a" * 64, result.stdout)
        self.assertNotIn("a" * 64, (root / "artifacts/installation-report.json").read_text())
        self.assertFalse(any("down" in call for call in calls))
        self.assertFalse(any("rm" in call for call in calls))

    def test_failed_verification_returns_failure_and_preserves_stack(self):
        result, calls, root = self.run_installer(fail="verification")
        self.assertEqual(result.returncode, 9)
        self.assertIn("acceptance verification failed", result.stderr)
        self.assertFalse(any(call[-1:] == ["token"] for call in calls))
        self.assertFalse(any("down" in call for call in calls))
        self.assertFalse(any("rm" in call for call in calls))
        self.assertEqual(json.loads((root / "artifacts/installation-report.json").read_text())["installer_status"], "verification_failed")

    def test_verify_does_not_rebuild_or_restart_application_services(self):
        result, calls, _ = self.run_installer("--verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("build" in call or "/workspace/scripts/setup.py" in call for call in calls))
        self.assertFalse(any("up" in call for call in calls))
        self.assertTrue(any("/workspace/scripts/verify.py" in call for call in calls))

    def test_show_token_does_not_change_the_installation(self):
        result, calls, _ = self.run_installer("--show-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("compose" in call or "pull" in call for call in calls))
        self.assertEqual(calls[-1][-1], "token")


if __name__ == "__main__":
    unittest.main()
