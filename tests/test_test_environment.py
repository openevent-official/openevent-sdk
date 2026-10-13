"""Tests run the freshly built SDK, even when an older same-version SDK exists."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_install import process_environment, write_wheel


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'Makefile', tmp_path / 'Makefile')
    shutil.copyfile(ROOT / 'scripts/prepare_test_env.py',
                    tmp_path / 'scripts/prepare_test_env.py')
    for name in ('test.sh', 'test-e2e.sh'):
        shutil.copy2(ROOT / name, tmp_path / name)
    for name in ('dist', 'wheelhouse', 'built'):
        (tmp_path / name).mkdir()
    # Model a successful builder producing a single current wheel. The actual
    # source-to-wheel build is also exercised by the enclosing make test.
    builder = tmp_path / 'build.sh'
    builder.write_text('#!/bin/sh\nset -eu\nrm -f dist/*.whl\ncp built/*.whl dist/\n')
    builder.chmod(0o755)
    return tmp_path


def test_test_env_uses_current_wheel_and_reuses_satisfied_dependencies(workspace):
    wheelhouse = workspace / 'wheelhouse'
    write_wheel(wheelhouse, 'sdk_test_dependency', '1.0', {'sdk_test_dependency.py': 'one'})
    environment = process_environment()
    environment.update(PIP_NO_INDEX='1', PIP_FIND_LINKS=str(wheelhouse))
    # A caller's source checkout must not shadow the installed test wheel.
    shadow = workspace / 'shadow'
    (shadow / 'openevent/sdk').mkdir(parents=True)
    (shadow / 'openevent/__init__.py').write_text('')
    (shadow / 'openevent/sdk/__init__.py').write_text('raise RuntimeError("old SDK")\n')
    environment.update(PYTHONPATH=str(shadow), PYTHONHOME=str(workspace / 'invalid-home'))

    def prepare(value, obsolete=False):
        files = {'openevent/__init__.py': '', 'openevent/sdk/__init__.py': 'value = {!r}\n'.format(value)}
        if obsolete:
            files['openevent/sdk/obsolete.py'] = 'old\n'
        write_wheel(workspace / 'built', 'openevent_sdk', '0.12.0', files,
                    requires=('sdk_test_dependency>=1',))
        result = subprocess.run(['make', 'test-env', 'PYTHON=' + sys.executable],
                                cwd=workspace, env=environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert result.returncode == 0, result.stdout

    prepare('first', obsolete=True)
    python = workspace / 'build/test-env/bin/python'
    clean_environment = process_environment()
    site = Path(subprocess.check_output(
        [str(python), '-B', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
        env=clean_environment, text=True,
    ).strip())
    dependency = site / 'sdk_test_dependency.py'
    previous_mtime = dependency.stat().st_mtime_ns

    write_wheel(wheelhouse, 'sdk_test_dependency', '2.0', {'sdk_test_dependency.py': 'two'})
    prepare('second')
    assert dependency.read_text() == 'one'
    assert dependency.stat().st_mtime_ns == previous_mtime
    assert not (site / 'openevent/sdk/obsolete.py').exists()
    value = subprocess.check_output(
        [str(python), '-B', '-c', 'from openevent.sdk import value; print(value)'],
        env=clean_environment, text=True,
    ).strip()
    assert value == 'second'


@pytest.mark.parametrize('target', ['test', 'e2e'])
def test_failed_build_does_not_run_tests_using_an_old_environment(workspace, target):
    builder = workspace / 'build.sh'
    builder.write_text('#!/bin/sh\nexit 1\n')
    write_wheel(workspace / 'dist', 'openevent_sdk', '0.11.0', {})
    python = workspace / 'build/test-env/bin/python'
    python.parent.mkdir(parents=True)
    marker = workspace / 'old-tests-ran'
    python.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    python.chmod(0o755)
    environment = process_environment()
    environment['OPENEVENT_SERVER_BIN'] = sys.executable
    result = subprocess.run(['make', target, 'PYTHON=' + sys.executable],
                            cwd=workspace, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode != 0, result.stdout
    assert not marker.exists()
