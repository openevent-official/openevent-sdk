"""Installation regressions using local wheels and temporary Python environments."""

import os
from pathlib import Path
import shutil
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


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / 'checkout'
    source.mkdir()
    for name in ('Makefile', 'build.sh'):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'scripts', source / 'scripts')
    return source


def test_make_test_env_rejects_redirected_install_before_building(tmp_path, python, checkout):
    destination = tmp_path / 'redirected'
    config = tmp_path / 'pip.conf'
    config.write_text(f'[install]\ntarget = {destination}\n')
    environment = process_environment()
    environment.update(PIP_CONFIG_FILE=str(config), PIP_NO_INDEX='1')
    result = subprocess.run(
        ['make', 'test-env', f'PYTHON={python}'], cwd=checkout, env=environment,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert result.returncode != 0, result.stdout
    assert 'target' in result.stdout.lower(), result.stdout
    assert not (checkout / 'build').exists()
    assert not destination.exists()


@pytest.fixture
def wheelhouse(tmp_path, checkout):
    # A local build backend keeps the real Make/pip build and install offline.
    wheels = tmp_path / 'wheelhouse'
    wheels.mkdir()
    backend = (
        'from pathlib import Path\n'
        'from shutil import copyfile\n'
        'def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n'
        '    wheel, = Path("input").glob("*.whl")\n'
        '    copyfile(wheel, Path(wheel_directory) / wheel.name)\n'
        '    return wheel.name\n'
    )
    write_wheel(wheels, 'sdk_fixture_backend', '1', {'sdk_fixture_backend.py': backend})
    (checkout / 'input').mkdir()
    (checkout / 'pyproject.toml').write_text(
        '[build-system]\nrequires = ["sdk_fixture_backend==1"]\nbuild-backend = "sdk_fixture_backend"\n'
    )
    return wheels


def test_make_build_keeps_build_dependencies_out_of_pip_target(tmp_path, python, checkout, wheelhouse):
    destination = tmp_path / 'packages'
    config = tmp_path / 'pip.conf'
    config.write_text(f'[global]\nno-index = yes\nfind-links = {wheelhouse.as_uri()}\n'
                      f'[install]\ntarget = {destination}\n')
    wheel = write_wheel(checkout / 'input', 'openevent_sdk', '1', {'openevent/sdk/__init__.py': ''})
    environment = process_environment()
    environment['PIP_CONFIG_FILE'] = str(config)
    subprocess.run(['make', 'build', f'PYTHON={python}'], cwd=checkout, env=environment, check=True)
    assert (checkout / 'dist' / wheel.name).is_file()
    assert not destination.exists()


def test_make_install_replaces_same_version_in_user_site(tmp_path, python, checkout, wheelhouse):
    write_wheel(wheelhouse, 'sdk_fixture_sibling', '1', {'openevent/sibling.py': 'value = 1\n'})
    destination = tmp_path / 'user base'
    environment = process_environment()
    environment.update(PIP_NO_INDEX='1', PIP_FIND_LINKS=wheelhouse.as_uri(),
                       PIP_USER='1', PYTHONUSERBASE=str(destination))

    def make_install(files):
        write_wheel(checkout / 'input', 'openevent_sdk', '1', files, requires=('sdk_fixture_sibling>=1',))
        subprocess.run(['make', 'install', f'PYTHON={python}'], cwd=checkout, env=environment, check=True)

    package_file = 'openevent/sdk/__init__.py'
    make_install({package_file: 'value = "old"\n', 'openevent/sdk/obsolete.py': ''})
    project, = destination.rglob(package_file)
    sibling = project.parents[1] / 'sibling.py'
    assert (project.parent / 'obsolete.py').is_file()
    mtime = sibling.stat().st_mtime_ns
    make_install({package_file: 'value = "new"\n'})
    imported = subprocess.check_output(
        [python, '-B', '-c', 'from openevent.sdk import value; print(value)'],
        cwd=tmp_path, env=environment, text=True,
    )
    assert imported.strip() == 'new'
    assert not (project.parent / 'obsolete.py').exists()
    assert sibling.read_text() == 'value = 1\n'
    assert sibling.stat().st_mtime_ns == mtime
    assert not list(destination.rglob('sdk_fixture_backend.py'))


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
