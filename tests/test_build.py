"""Check which build artifacts SDK cleanup removes."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_clean_preserves_developer_environment_and_unrelated_releases(tmp_path):
    shutil.copy2(ROOT / 'Makefile', tmp_path / 'Makefile')
    keep = [
        tmp_path / 'build/venv/bin/python',
        tmp_path / 'build/wheelhouse/a_dependency-1-py3-none-any.whl',
        tmp_path / 'dist/other_project-1-py3-none-any.whl',
    ]
    remove = [
        tmp_path / 'build/generated/openevent/sdk/proto/admin_pb2.py',
        tmp_path / 'dist/openevent_sdk-1-py3-none-any.whl',
    ]
    for path in keep + remove:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('marker')

    subprocess.run(['make', 'clean'], cwd=tmp_path, check=True)
    for path in keep:
        assert path.read_text() == 'marker'
    for path in remove:
        assert not path.exists()
