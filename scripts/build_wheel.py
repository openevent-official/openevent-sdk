"""Build a wheel without redirecting its isolated build dependencies."""

from configparser import RawConfigParser
import os
import sys
from tempfile import NamedTemporaryFile

from pip._internal.configuration import Configuration


def main(wheel_dir):
    # pip already installs build requirements with --no-user and its own --prefix.
    redirects = ('target', 'root', 'python')
    for name in redirects:
        os.environ.pop('PIP_' + name.upper(), None)

    config = RawConfigParser()
    config.optionxform = lambda key: key.lower().replace('_', '-').removeprefix('--')
    if os.environ.get('PIP_CONFIG_FILE') != os.devnull:
        for _, paths in Configuration(isolated=False).iter_config_files():
            config.read(paths)
    redirects_in_config = [(section, name) for section in config.sections()
                           for name in redirects if config.get(section, name, fallback=None)]
    if redirects_in_config:
        # Empty values override redirects from site and global files. Preserve package sources.
        for section, name in redirects_in_config:
            config.set(section, name, '')
        # build.sh removes this private wheel directory after pip exits.
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=wheel_dir, delete=False) as stream:
            config.write(stream)
            os.environ['PIP_CONFIG_FILE'] = stream.name

    os.execv(sys.executable, [sys.executable, '-B', '-m', 'pip', 'wheel',
                              '--no-deps', '--wheel-dir', wheel_dir, '.'])


if __name__ == '__main__':
    main(sys.argv[1])
