"""Install the unique wheel produced by the successful make build prerequisite."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    arguments = sys.argv[1:]
    destinations = ('--target', '--prefix', '--root')
    if (any(argument.startswith('-t') or any(
            argument == option or argument.startswith(option + '=')
            for option in destinations) for argument in arguments)
            or any(os.environ.get(name) for name in ('PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT'))):
        sys.exit('make install installs into the selected Python environment; '
                 'choose its interpreter with PYTHON instead of --target, --prefix, or --root')

    wheels = list((ROOT / 'dist').glob('openevent_sdk-*.whl'))
    if len(wheels) != 1:
        sys.exit('Exactly one SDK wheel from a successful make build is required')

    # Reuse the build script's working directories; its exports do not survive
    # into this separate make recipe.
    temporary = ROOT / 'build/tmp'
    pycache = ROOT / 'build/pycache'
    temporary.mkdir(parents=True, exist_ok=True)
    pycache.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PIP_NO_COMPILE='1',
                       TMPDIR=str(temporary), PYTHONPYCACHEPREFIX=str(pycache),
                       PIP_NO_CACHE_DIR='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    pip = [sys.executable, '-B', '-m', 'pip', 'install', '--no-compile']
    # Resolve declared dependencies normally, then replace only the SDK itself.
    # A dependency failure must leave the installed SDK unchanged.
    subprocess.run(pip + arguments + [str(wheels[0])], env=environment, check=True)
    subprocess.run(pip + ['--force-reinstall', '--no-deps'] + arguments + [str(wheels[0])],
                   env=environment, check=True)


if __name__ == '__main__':
    main()
