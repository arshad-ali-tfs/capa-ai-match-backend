"""Exercise the standalone configuration/import boundary without model calls."""
from pathlib import Path
import json
import pytest
from dotenv import dotenv_values
from capa_match import cli
from capa_match.infrastructure.data.repository import SqliteRecordRepository
from capa_match.infrastructure.web import create_service


def test_import_keeps_source_identity_and_all_rows_accounted_for(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setenv('CAPA_DATA_DIR', str(tmp_path / 'data'))
    source = tmp_path / 'source.csv'
    source.write_text('PR_ID,SITE,RECORD_TYPE,DETAILED_DESCRIPTION\n00042,Test Site,Deviation,FICTIONAL TEST: five labels found\n00042,Test Site,Deviation,FICTIONAL TEST: duplicate retained for review\n', encoding='utf-8')
    cli.import_source([str(source)])
    env = dotenv_values(tmp_path / '.env')
    repository = SqliteRecordRepository(Path(env['CAPA_SOURCE_SNAPSHOT']))
    metadata = repository.metadata()
    assert metadata['source_rows'] == 2
    assert metadata['accepted_rows'] == metadata['quarantined_rows'] == 1
    assert repository.get_record('00042').original['DETAILED_DESCRIPTION'] == 'FICTIONAL TEST: five labels found'
    assert '00042' in Path(env['CAPA_SOURCE_CSV']).read_text(encoding='utf-8-sig')
    assert metadata['quarantine_reasons'] == {'duplicate_pr_id': 1}


def test_lfs_pointer_has_clear_setup_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    source = tmp_path / 'source.csv'
    source.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 121397555\n')
    with pytest.raises(SystemExit):
        cli.import_source([str(source)])
    assert 'git lfs pull' in capsys.readouterr().err


def test_paths_are_relative_to_this_repository_from_any_working_directory(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'; repo.mkdir()
    elsewhere = tmp_path / 'elsewhere'; elsewhere.mkdir()
    monkeypatch.setattr(cli, 'ROOT', repo)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv('CAPA_DATA_DIR', './data')
    monkeypatch.setenv('CAPA_SOURCE_CSV', './data/sources/trackwise_records.csv')
    cli.configure()
    assert Path(__import__('os').environ['CAPA_DATA_DIR']) == repo / 'data'
    assert Path(__import__('os').environ['CAPA_SOURCE_CSV']) == repo / 'data/sources/trackwise_records.csv'


def test_service_factory_cannot_expose_another_capability():
    with pytest.raises(ValueError):
        create_service('map')


def test_deployed_configuration_does_not_load_workstation_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('CAPA_SOURCE_CSV=wrong-workstation-source.csv\n')
    monkeypatch.setenv('CAPA_ENV', 'databricks')
    monkeypatch.setenv('CAPA_SOURCE_CSV', '/Volumes/test/source.csv')
    cli.configure()
    assert __import__('os').environ['CAPA_SOURCE_CSV'] == '/Volumes/test/source.csv'


def test_installed_wheel_uses_launch_directory_or_explicit_project_root(tmp_path, monkeypatch):
    launch = tmp_path / 'application'; launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'site-packages/package/cli.py'))
    monkeypatch.delenv('CAPA_PROJECT_ROOT', raising=False)
    assert cli.project_root() == launch
    monkeypatch.setenv('CAPA_PROJECT_ROOT', str(tmp_path / 'explicit'))
    assert cli.project_root() == tmp_path / 'explicit'
