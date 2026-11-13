"""Isolate offline tests from workstation credentials, source and case storage."""
import os
from pathlib import Path
import tempfile

_test_root = tempfile.TemporaryDirectory(prefix='capa-match-tests-')
os.environ['CAPA_SOURCE_SNAPSHOT'] = ''
os.environ['CAPA_SOURCE_CSV'] = ''
os.environ['CAPA_DATA_DIR'] = _test_root.name
os.environ['CAPA_ENV'] = 'local'
