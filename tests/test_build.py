"""Generation keeps a complete, fresh development package outside the source tree."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def test_init_stages_importable_package_without_writing_source():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='generation-test-', dir=str(build)) as temporary:
        root = Path(temporary)
        for name in ('src', 'proto'):
            shutil.copytree(ROOT / name, root / name)
        for name in ('Makefile', 'generate_python_proto.sh', 'requirements-codegen.txt'):
            shutil.copy2(ROOT / name, root / name)

        source = root / 'src'
        marker = source / 'openevent/sdk/build_marker.py'
        marker.write_text('generation = 1\n')
        stage = root / 'build/python'
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')

        def snapshot():
            return {path.relative_to(source): path.read_bytes()
                    for path in source.rglob('*') if path.is_file()}

        def generate():
            before = snapshot()
            subprocess.run(['make', 'init', 'PYTHON=' + sys.executable], cwd=str(root),
                           env=environment, check=True)
            assert snapshot() == before

        generate()
        assert (stage / marker.relative_to(source)).read_text() == 'generation = 1\n'
        (stage / 'openevent/sdk/removed.py').write_text('stale\n')
        marker.write_text('generation = 2\n')
        generate()
        assert not (stage / 'openevent/sdk/removed.py').exists()

        environment['PYTHONPATH'] = str(stage) + os.pathsep + environment.get('PYTHONPATH', '')
        subprocess.run([sys.executable, '-B', '-c', '''
from pathlib import Path
from openevent.sdk import AdminClient, OpenEventClient, build_marker
from openevent.sdk.proto import admin_pb2, admin_pb2_grpc, openevent_pb2, openevent_pb2_grpc
assert build_marker.generation == 2
assert Path.cwd() / 'build/python' in Path(admin_pb2.__file__).parents
assert admin_pb2.AddTokenResponse(token='new').token == 'new'
assert openevent_pb2.EventMessage(seq=1).seq == 1
assert admin_pb2_grpc.AdminServiceStub
assert openevent_pb2_grpc.EventServiceStub
'''], cwd=str(root), env=environment, check=True)
