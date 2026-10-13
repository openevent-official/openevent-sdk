"""Install the wheel from make build into the reusable, isolated test environment."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    wheels = list((ROOT / 'dist').glob('openevent_sdk-*.whl'))
    if len(wheels) != 1:
        sys.exit('Exactly one SDK wheel from a successful make build is required')

    directory = ROOT / 'build/test-env'
    temporary = ROOT / 'build/test-env-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PIP_NO_COMPILE='1',
                       TMPDIR=str(temporary), PIP_NO_CACHE_DIR='1',
                       PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_REQUIRE_VIRTUALENV='1')
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    # Installation destinations belong to this test environment, not the user's
    # pip configuration. Index and other dependency-source settings still apply.
    for name in ('PIP_TARGET', 'PIP_PREFIX', 'PIP_ROOT', 'PIP_USER'):
        environment.pop(name, None)

    identity = '{}\n{}\n{}\n'.format(sys.executable, sys.prefix, sys.version)
    marker = directory / '.base-python'
    python = directory / 'bin/python'
    if not marker.is_file() or marker.read_text() != identity or not python.exists():
        if directory.exists():
            shutil.rmtree(directory)
        # Keep installed third-party packages visible; pip adds only missing or
        # incompatible requirements to the test environment.
        subprocess.run([sys.executable, '-B', '-m', 'venv', '--system-site-packages',
                        str(directory)], env=environment, check=True)
        marker.write_text(identity)

    pip = [str(python), '-B', '-m', 'pip', 'install', '--no-compile']
    subprocess.run(pip + [str(wheels[0]) + '[test]'], env=environment, check=True)
    # Same-version source changes must replace the SDK without reinstalling its
    # dependencies or modifying the caller's Python environment.
    subprocess.run(pip + ['--force-reinstall', '--no-deps', str(wheels[0])],
                   env=environment, check=True)
    subprocess.run([str(python), '-B', '-c', '''
import importlib.metadata
from pathlib import Path
import sys
import openevent.sdk

prefix = Path(sys.prefix).resolve()
package = Path(openevent.sdk.__file__).resolve()
distribution = Path(importlib.metadata.distribution('openevent-sdk').locate_file('')).resolve()
if prefix not in package.parents or prefix not in distribution.parents:
    raise RuntimeError('Tests must import the SDK installed in build/test-env')
print('SDK test environment:', sys.executable)
'''], cwd=ROOT, env=environment, check=True)


if __name__ == '__main__':
    main()
