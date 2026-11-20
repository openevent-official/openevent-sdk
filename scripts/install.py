"""Install the wheel passed directly by the current build."""

import os
from pathlib import Path
import subprocess
import sys

from pip._internal.commands import create_command
from pip._internal.locations import get_scheme


def check_install_environment(test_env):
    parser = create_command('install').parser
    options, _ = parser.parse_args([])
    destinations = ['--python', '--target', '--prefix', '--root']
    if test_env:
        destinations.append('--user')
    for flag in destinations:
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


def main():
    arguments = sys.argv[1:]
    test_env = arguments[:1] == ['--test-env']
    if test_env:
        arguments = arguments[1:]
    if arguments == ['--check-environment']:
        check_install_environment(test_env)
        return
    wheel_name, = arguments
    wheel = str(Path(wheel_name).resolve())

    if test_env:
        # Fail on a read-only environment instead of letting pip choose the user site.
        os.environ['PIP_USER'] = '0'
    pip = [sys.executable, '-B', '-m', 'pip', 'install', '--no-compile']
    requirement = wheel + '[test]' if test_env else wheel
    subprocess.run(pip + [requirement], check=True)
    subprocess.run(pip + ['--force-reinstall', '--no-deps', wheel], check=True)
    if test_env:
        verify_test_install()


if __name__ == '__main__':
    main()
