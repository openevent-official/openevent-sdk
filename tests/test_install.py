"""Installation regressions using local wheels and temporary Python environments."""

import os
from pathlib import Path
import re
import subprocess
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def write_wheel(directory, name, version, files, requires=(), extras=()):
    metadata = '{}-{}.dist-info'.format(name, version)
    contents = dict(files)
    contents[metadata + '/METADATA'] = (
        'Metadata-Version: 2.1\nName: {}\nVersion: {}\n'.format(name, version)
        + ''.join('Provides-Extra: {}\n'.format(extra) for extra in extras)
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
    return path


def process_environment():
    environment = {name: value for name, value in os.environ.items() if not name.startswith('PIP_')}
    environment.update(PIP_NO_COMPILE='1', PIP_NO_CACHE_DIR='1',
                       PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1')
    return environment


@pytest.fixture
def python(tmp_path):
    directory = tmp_path / 'chosen environment'
    venv.EnvBuilder(system_site_packages=True).create(directory)
    return directory / 'bin/python'


def test_make_install_rejects_redirected_pip_config(tmp_path, python):
    outside = tmp_path / 'redirected'
    (python.parent.parent / 'pip.conf').write_text(f'[install]\nroot = {outside}\n')
    # Let pip read the chosen environment's config without loading user settings.
    empty_config = tmp_path / 'empty-pip.conf'
    empty_config.touch()
    environment = process_environment()
    environment.update(PIP_CONFIG_FILE=str(empty_config), PIP_NO_INDEX='1')
    result = subprocess.run(
        ['make', 'install', f'PYTHON={python}'], cwd=ROOT, env=environment,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0, result.stdout
    # A setting can appear as --root, install.root or a bare key in a configuration diagnostic.
    setting = re.compile(r'(?<![\w/.-])(--|(?:install|global)\.|pip_)?root(?![\w/-])')
    assert any(
        match.group(1) or any(word in line for word in ('config', 'setting', 'option'))
        for line in result.stdout.lower().splitlines()
        if (match := setting.search(line))
    ), result.stdout
    assert not outside.exists()


def test_same_version_replaces_sdk_and_resolves_only_needed_dependencies(tmp_path, python):
    stage = tmp_path / 'current wheel'
    stage.mkdir()
    wheels = tmp_path / 'wheelhouse'
    wheels.mkdir()
    environment = process_environment()
    environment.update(PIP_NO_INDEX='1', PIP_FIND_LINKS=wheels.name)
    site_packages = Path(subprocess.check_output(
        [str(python), '-B', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
        env=environment, text=True,
    ).strip())

    def sdk(value, obsolete=False, requires=('sdk_install_dependency>=1',)):
        files = {'openevent/sdk/__init__.py': f'value = {value!r}\n'}
        if obsolete:
            files['openevent/sdk/obsolete.py'] = ''
        dependencies = (*requires, 'sdk_test_dependency==1; extra == "test"')
        return write_wheel(stage, 'openevent_sdk', '0.8.1', files, dependencies, extras=('test',))

    def install(wheel, test_env=False):
        options = ['--test-env'] if test_env else []
        return subprocess.run(
            [python, '-B', ROOT / 'scripts/install.py', *options, wheel],
            cwd=tmp_path,
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

    write_wheel(wheels, 'sdk_install_dependency', '1.0', {'sdk_install_dependency.py': 'one'})
    write_wheel(wheels, 'sdk_test_dependency', '1.0', {'sdk_test_dependency.py': ''})
    first = install(sdk('old', obsolete=True))
    assert first.returncode == 0, first.stdout
    dependency = site_packages / 'sdk_install_dependency.py'
    project = site_packages / 'openevent/sdk/__init__.py'
    test_dependency = site_packages / 'sdk_test_dependency.py'
    assert dependency.read_text(encoding='utf-8') == 'one'
    assert project.read_text(encoding='utf-8') == "value = 'old'\n"
    assert not test_dependency.exists()
    dependency_mtime = dependency.stat().st_mtime_ns

    write_wheel(wheels, 'sdk_install_dependency', '2.0', {'sdk_install_dependency.py': 'two'})
    second = install(sdk('new'), test_env=True)
    assert second.returncode == 0, second.stdout
    assert project.read_text(encoding='utf-8') == "value = 'new'\n"
    assert not (project.parent / 'obsolete.py').exists()
    assert test_dependency.is_file()
    assert dependency.read_text(encoding='utf-8') == 'one'
    assert dependency.stat().st_mtime_ns == dependency_mtime

    upgraded = install(sdk('new dependency', requires=('sdk_install_dependency>=2',)))
    assert upgraded.returncode == 0, upgraded.stdout
    assert dependency.read_text(encoding='utf-8') == 'two'
    assert project.read_text(encoding='utf-8') == "value = 'new dependency'\n"

    failed = install(sdk('must not install', requires=('sdk_install_missing_dependency==1',)))
    assert failed.returncode != 0, failed.stdout
    assert project.read_text(encoding='utf-8') == "value = 'new dependency'\n"
