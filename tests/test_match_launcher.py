"""Exercise the actual PowerShell entry point with a controlled Docker CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scenario', ['full', 'prepare', 'failed_build', 'failed_index', 'failed_verify', 'unrelated', 'stop', 'missing_lfs', 'legacy', 'legacy_autoremove', 'wrong_legacy_mount'])
def test_launcher_boundaries(tmp_path, scenario):
    shell = shutil.which('powershell') or shutil.which('pwsh')
    if not shell: pytest.skip('PowerShell is required')
    repo = tmp_path / 'repository with spaces'
    repo.mkdir()
    for name in ('Run-Match.ps1', 'docker.env.example'): shutil.copyfile(ROOT / name, repo / name)
    asset = repo / 'data/sources/trackwise_records.csv'
    asset.parent.mkdir(parents=True)
    asset.write_text('FICTIONAL source')
    (repo / 'runtime-assets.json').write_text(json.dumps(dict(files=[dict(path='data/sources/trackwise_records.csv', size_bytes=asset.stat().st_size, sha256='fixture')])))
    if scenario == 'missing_lfs': asset.write_text('pointer')
    data = tmp_path / 'persistent data'
    (data / '.setup').mkdir(parents=True)
    (data / 'retained.txt').write_text('existing work')
    (data / '.setup/match.json').write_text(json.dumps(dict(CAPA_SOURCE_SNAPSHOT='/data/sources/fixture.sqlite3',
        CAPA_SOURCE_CSV='/data/sources/fixture.csv', MATCH_INDEX_DIR='/data/match-managed/fixture',
        MATCH_MODEL_NAME='sentence-transformers/all-mpnet-base-v2', MATCH_MODEL_CACHE='/data/models/huggingface',
        MATCH_ALLOWED_SITES_JSON='["Site A"]', records=2, sites=1, generation='generations/fixture')))
    calls, harness = tmp_path / 'calls.jsonl', tmp_path / 'harness.ps1'
    harness.write_text(r'''
$ErrorActionPreference='Stop'
function global:docker {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArguments)
    @{args=@($CommandArguments)} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:TEST_CALLS
    $global:LASTEXITCODE=0
    if ($CommandArguments[0] -eq 'info') { 'linux'; return }
    if ($CommandArguments[0] -eq 'container' -and $CommandArguments[1] -eq 'ls') {
        if ($env:TEST_SCENARIO -in @('unrelated','legacy','legacy_autoremove','wrong_legacy_mount')) { 'fixture-container' }; return
    }
    if ($CommandArguments[0] -eq 'container' -and $CommandArguments[1] -eq 'inspect') {
        if ($env:TEST_SCENARIO -in @('legacy','legacy_autoremove','wrong_legacy_mount')) {
            $source=$env:TEST_DATA
            if ($env:TEST_SCENARIO -eq 'wrong_legacy_mount') { $source+='-other' }
            @{Image='sha256:old';AutoRemove=($env:TEST_SCENARIO -eq 'legacy_autoremove');Config=@{Image='capa-ai-match:local';Labels=@{}};State=@{Running=$true};Mounts=@(@{Type='bind';Destination='/data';Source=$source})} | ConvertTo-Json -Compress -Depth 6
            return
        }
        '{"Config":{"Labels":{"capa.match.project":"another-checkout"}},"State":{"Running":true}}'; return
    }
    if ($CommandArguments[0] -eq 'image') { 'sha256:fixture'; return }
    if ($CommandArguments[0] -eq 'network') { 'capa-ai'; return }
    if ($CommandArguments[0] -eq 'build') {
        Write-Error 'Fictional normal build progress on stderr'
        if ($env:TEST_SCENARIO -eq 'failed_build') { $global:LASTEXITCODE=11 }
    }
    if ($CommandArguments[0] -eq 'run' -and $CommandArguments -contains 'prepare' -and $env:TEST_SCENARIO -eq 'failed_index') { $global:LASTEXITCODE=12 }
    if ($CommandArguments[0] -eq 'exec' -and $CommandArguments -contains 'verify' -and $env:TEST_SCENARIO -eq 'failed_verify') { $global:LASTEXITCODE=13 }
}
function global:git {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArguments)
    $global:LASTEXITCODE=0
    if ($GitArguments -contains 'pull') { $global:LASTEXITCODE=14 }
}
$parameters=@{ContainerName='match-fixture'; DataDirectory=$env:TEST_DATA; Port=28102}
if ($env:TEST_SCENARIO -eq 'prepare') { $parameters.PrepareOnly=$true }
if ($env:TEST_SCENARIO -eq 'stop') { $parameters.Stop=$true }
& $env:TEST_SCRIPT @parameters
''', encoding='utf8')
    env = dict(os.environ, TEST_CALLS=str(calls), TEST_DATA=str(data), TEST_SCRIPT=str(repo / 'Run-Match.ps1'), TEST_SCENARIO=scenario)
    result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)], env=env,
                            capture_output=True, text=True, timeout=60)
    recorded = [json.loads(line)['args'] for line in calls.read_text(encoding='utf-8-sig').splitlines()]
    assert (data / 'retained.txt').read_text() == 'existing work'
    if scenario in ('failed_build', 'failed_index', 'unrelated', 'missing_lfs', 'wrong_legacy_mount'):
        assert result.returncode != 0
        assert not any('--detach' in row or row[0] == 'stop' or row[:2] == ['container', 'rm'] for row in recorded)
        return
    if scenario == 'failed_verify':
        assert result.returncode != 0
        assert 'MATCH ready:' not in result.stdout
        return
    assert result.returncode == 0, result.stdout + result.stderr
    if scenario == 'stop':
        assert not any(row[0] in ('build', 'run') for row in recorded)
        return
    preparation = next(row for row in recorded if row[0] == 'run' and 'prepare' in row)
    assert f'type=bind,source={data},target=/data' in preparation
    assert any('target=/bundle,readonly' in item for item in preparation)
    assert preparation[preparation.index('--limit') + 1] == '0'
    services = [row for row in recorded if '--detach' in row]
    if scenario == 'prepare': assert not services
    else:
        assert services[0][services[0].index('--publish') + 1] == '127.0.0.1:28102:8102'
        assert any(row[0] == 'exec' and 'verify' in row for row in recorded)
        if scenario in ('legacy', 'legacy_autoremove'):
            assert any(row[0] == 'stop' for row in recorded)
            assert any(row[:2] == ['container', 'rm'] for row in recorded) is (scenario == 'legacy')
    assert 'OPENAI_API_KEY' not in (repo / '.local/match-fixture/runtime.env').read_text()
