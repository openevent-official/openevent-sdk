"""Installation regressions using local wheels and temporary Python environments."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='install-test-', dir=str(build)) as temporary:
        root = Path(temporary)
        (root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/install.py', root / 'scripts/install.py')
        (root / 'dist').mkdir()
        yield root


def write_wheel(directory, name, version, files, requires=()):
    metadata = '{}-{}.dist-info'.format(name, version)
    contents = dict(files)
    contents[metadata + '/METADATA'] = (
        'Metadata-Version: 2.1\nName: {}\nVersion: {}\n'.format(name, version)
        + ''.join('Requires-Dist: {}\n'.format(requirement) for requirement in requires)
    )
    contents[metadata + '/WHEEL'] = (
        'Wheel-Version: 1.0\nGenerator: sdk-installation-test\n'
        'Root-Is-Purelib: true\nTag: py3-none-any\n'
    )
    record = metadata + '/RECORD'
    contents[record] = ''.join('{},,\n'.format(path) for path in [*contents, record])
    path = directory / '{}-{}-py3-none-any.whl'.format(name, version)
    with zipfile.ZipFile(path, 'w') as archive:
        for member, content in contents.items():
            archive.writestr(member, content)


def process_environment():
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith('PIP_') and name != 'PYTHONPATH'}
    environment.update(PYTHONDONTWRITEBYTECODE='1', PIP_NO_COMPILE='1',
                       PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1')
    return environment


def test_same_version_replaces_sdk_and_resolves_only_needed_dependencies(workspace):
    wheels = workspace / 'dist'
    # Reuse the existing pip without downloading or bootstrapping one.
    builder = venv.EnvBuilder(system_site_packages=True)
    environment_path = workspace / 'environment'
    builder.create(environment_path)
    python = builder.ensure_directories(environment_path).env_exe
    environment = process_environment()
    site_packages = Path(subprocess.check_output(
        [str(python), '-B', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
        env=environment, text=True,
    ).strip())

    def sdk(value, obsolete=False, requires=('sdk_install_dependency>=1',)):
        files = {'sdk_install_project.py': value}
        if obsolete:
            files['sdk_install_obsolete.py'] = 'old'
        write_wheel(wheels, 'openevent_sdk', '0.8.1', files, requires)

    def install(*arguments):
        return subprocess.run(
            [str(python), '-B', str(workspace / 'scripts/install.py'),
             '--no-index', '--find-links', str(wheels), *arguments],
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    write_wheel(wheels, 'sdk_install_dependency', '1.0', {'sdk_install_dependency.py': 'one'})
    sdk('old', obsolete=True)
    first = install()
    assert first.returncode == 0, first.stdout
    dependency = site_packages / 'sdk_install_dependency.py'
    project = site_packages / 'sdk_install_project.py'
    assert dependency.read_text(encoding='utf-8') == 'one'
    assert project.read_text(encoding='utf-8') == 'old'
    dependency_mtime = dependency.stat().st_mtime_ns

    write_wheel(wheels, 'sdk_install_dependency', '2.0', {'sdk_install_dependency.py': 'two'})
    sdk('new')
    second = install()
    assert second.returncode == 0, second.stdout
    assert project.read_text(encoding='utf-8') == 'new'
    assert not (site_packages / 'sdk_install_obsolete.py').exists()
    assert dependency.read_text(encoding='utf-8') == 'one'
    assert dependency.stat().st_mtime_ns == dependency_mtime

    sdk('dry run')
    dry_run = install('--dry-run')
    assert dry_run.returncode == 0, dry_run.stdout
    assert project.read_text(encoding='utf-8') == 'new'

    sdk('new dependency', requires=('sdk_install_dependency>=2',))
    upgraded = install()
    assert upgraded.returncode == 0, upgraded.stdout
    assert dependency.read_text(encoding='utf-8') == 'two'
    assert project.read_text(encoding='utf-8') == 'new dependency'

    sdk('must not install', requires=('sdk_install_missing_dependency==1',))
    failed = install()
    assert failed.returncode != 0, failed.stdout
    assert project.read_text(encoding='utf-8') == 'new dependency'


@pytest.mark.parametrize('arguments', [
    ['--target', '/unused'], ['--target=/unused'], ['-t', '/unused'], ['-t/unused'],
    ['--prefix', '/unused'], ['--prefix=/unused'], ['--root', '/unused'], ['--root=/unused'],
])
def test_alternate_destinations_are_rejected_before_installation(arguments):
    result = subprocess.run(
        [sys.executable, '-B', str(ROOT / 'scripts/install.py'), *arguments],
        env=process_environment(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0
    assert 'choose its interpreter with PYTHON' in result.stdout


@pytest.mark.parametrize('variable', ['PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT'])
def test_alternate_destination_environment_is_rejected(variable):
    environment = process_environment()
    environment[variable] = '/unused'
    result = subprocess.run(
        [sys.executable, '-B', str(ROOT / 'scripts/install.py')],
        env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0
    assert 'choose its interpreter with PYTHON' in result.stdout


@pytest.mark.parametrize('versions', [[], ['0.8.0', '0.8.1']])
def test_install_requires_one_sdk_wheel(workspace, versions):
    for version in versions:
        write_wheel(workspace / 'dist', 'openevent_sdk', version, {})
    result = subprocess.run(
        [sys.executable, '-B', str(workspace / 'scripts/install.py')],
        env=process_environment(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0
    assert 'Exactly one SDK wheel' in result.stdout


def test_failed_build_does_not_run_install_with_an_old_wheel(workspace):
    shutil.copyfile(ROOT / 'Makefile', workspace / 'Makefile')
    write_wheel(workspace / 'dist', 'openevent_sdk', '0.8.0', {})
    build = workspace / 'build.sh'
    build.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    build.chmod(0o755)
    marker = workspace / 'install-ran'
    (workspace / 'scripts/install.py').write_text(
        'from pathlib import Path\nPath({!r}).touch()\n'.format(str(marker)), encoding='utf-8',
    )
    result = subprocess.run(
        ['make', 'install', 'PYTHON=' + sys.executable], cwd=str(workspace),
        env=process_environment(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0
    assert not marker.exists()
