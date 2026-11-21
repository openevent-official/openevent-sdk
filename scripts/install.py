"""Install the wheel passed directly by the current build."""

from contextlib import contextmanager, nullcontext
from importlib import metadata
from importlib.util import cache_from_source
import os
from pathlib import Path
import site
import subprocess
import sys
from zipfile import ZipFile

from pip._internal.commands import create_command
from pip._internal.commands.install import decide_user_install
from pip._internal.locations import get_scheme
from pip._internal.utils.wheel import wheel_dist_info_dir


def check_install_environment(parser, options, test_env):
    forbidden = ['--python', '--root']
    if test_env:
        forbidden.extend(['--target', '--prefix', '--user'])
    for flag in forbidden:
        option = parser.get_option(flag)
        if option and getattr(options, option.dest):
            sys.exit(f'pip has {flag} set. Remove it from the pip configuration or environment '
                     'to install the wheel in the selected environment')


def verify_test_install():
    site = Path(get_scheme('openevent-sdk').purelib)
    expected = (site / 'openevent/sdk/__init__.py').resolve()
    imported = subprocess.check_output(
        [sys.executable, '-B', '-c', 'import openevent.sdk; print(openevent.sdk.__file__)'], text=True,
    ).strip()
    if Path(imported).resolve() != expected:
        sys.exit(f'{sys.executable} imported the SDK from {imported}, expected {expected}')


@contextmanager
def clean_previous_user_files(wheel):
    """A venv's pip can write to a visible user site but won't uninstall there."""
    scheme = get_scheme('openevent-sdk', user=True)
    previous = next(metadata.distributions(name='openevent-sdk', path=[scheme.purelib]), None)
    if previous is None:
        yield
        return
    records = previous.files
    if records is None:
        sys.exit(f'Cannot replace the SDK in {scheme.purelib}: its installed file list is missing')
    old_files = {Path(entry.locate()) for entry in records}
    yield
    with ZipFile(wheel) as archive:
        info = Path(scheme.purelib) / wheel_dist_info_dir(archive, 'openevent-sdk')
    current = metadata.Distribution.at(info)
    obsolete = old_files.difference(Path(entry.locate()) for entry in current.files)
    # A cached .pyc can match a same-size, same-second source replacement.
    obsolete.update(Path(cache_from_source(str(path))) for path in old_files if path.suffix == '.py')
    user_base = Path(scheme.data).resolve()
    for path in obsolete:
        directory = path.parent.resolve()
        if not directory.is_relative_to(user_base):
            continue
        (directory / path.name).unlink(missing_ok=True)
        while directory != user_base and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent


def main():
    arguments = sys.argv[1:]
    test_env = arguments[:1] == ['--test-env']
    if test_env:
        arguments = arguments[1:]
    parser = create_command('install').parser
    options, _ = parser.parse_args([])
    if arguments == ['--check-environment']:
        check_install_environment(parser, options, test_env)
        return
    wheel_name, = arguments
    wheel = Path(wheel_name).resolve()
    if test_env:
        # Fail on a read-only environment instead of letting pip choose the user site.
        os.environ['PIP_USER'] = '0'
    pip = [sys.executable, '-B', '-m', 'pip', 'install', '--no-compile']
    requirement = str(wheel) + '[test]' if test_env else str(wheel)
    cleanup = nullcontext()
    if (not test_env and sys.prefix != sys.base_prefix and site.ENABLE_USER_SITE
            and decide_user_install(options.use_user_site)):
        cleanup = clean_previous_user_files(wheel)
    with cleanup:
        subprocess.run(pip + [requirement], check=True)
        subprocess.run(pip + ['--force-reinstall', '--no-deps', wheel], check=True)
    if test_env:
        verify_test_install()


if __name__ == '__main__':
    main()
