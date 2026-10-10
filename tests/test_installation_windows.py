"""Run the actual PowerShell environment functions with private temporary fixtures."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'nt', 'Requires native Windows PowerShell and NTFS')
class WindowsEnvironmentTests(unittest.TestCase):
    def test_parser_rotation_comments_and_preserved_settings(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            shutil.copyfile(root / '.env.example', fixture / '.env.example')
            keys = ('LOCAL_WEB_API_TOKEN', 'CRAWL4AI_API_TOKEN', 'SEARXNG_SECRET_KEY')
            (fixture / '.env').write_bytes((''.join(
                f' \t{key} = "{letter * 64}" # keep {key}\r\n'
                for key, letter in zip(keys, 'abc')) + 'GATEWAY_PORT=9080\r\n').encode())
            script = fixture / 'fixture.ps1'
            script.write_text(r'''
param([string]$Source, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$Tokens = $null; $ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count) { throw 'Installer syntax error' }
$Ast.FindAll({param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$InstallRoot = $Fixture
$EnvFile = Join-Path $Fixture '.env'
$RotateSecrets = $true
Initialize-Environment
$First = Read-Environment ([IO.File]::ReadAllText($EnvFile))
$RotateSecrets = $false
Initialize-Environment
$Second = Read-Environment ([IO.File]::ReadAllText($EnvFile))
foreach ($Key in $First.Keys) { if ($First[$Key] -cne $Second[$Key]) { throw 'Rerun changed configuration' } }
$Rejected = $false
try { Read-Environment "A=1`n A=2" } catch { $Rejected = $true }
if (-not $Rejected) { throw 'Duplicate accepted' }
$First | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Fixture 'parsed.json') -Encoding UTF8
''', encoding='utf-8')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(script), '-Source', str(root / 'install.ps1'),
                                     '-Fixture', str(fixture)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = json.loads((fixture / 'parsed.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(values['GATEWAY_PORT'], '9080')
            for key, letter in zip(keys, 'abc'):
                self.assertRegex(values[key], r'^[0-9a-f]{64}$')
                self.assertNotEqual(values[key], letter * 64)
                self.assertIn(f'# keep {key}', (fixture / '.env').read_text())
