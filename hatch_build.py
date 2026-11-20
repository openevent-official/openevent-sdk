from pathlib import Path
import shutil

from grpc_tools import protoc
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ProtocolBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        proto_root = root / "proto"
        package = Path("openevent/sdk/proto")
        generated = root / "build/generated"
        if generated.exists():
            shutil.rmtree(generated)
        generated.mkdir(parents=True)

        result = protoc.main([
            "protoc",
            f"-I{proto_root}",
            f"--python_out={generated}",
            f"--grpc_python_out={generated}",
            str(proto_root / package / "openevent.proto"),
            str(proto_root / package / "admin.proto"),
        ])
        if result:
            raise RuntimeError("Could not generate OpenEvent protocol modules")
        for module in (generated / package).glob("*.py"):
            build_data["force_include"][str(module)] = (package / module.name).as_posix()
