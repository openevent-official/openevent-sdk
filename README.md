# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, the public `system.v1` initialization-message protocol,
and Python SDK.

The Python SDK provides lightweight clients for messages, objects, Channels, and
administration. Generated gRPC stubs are also available for direct use.
See the [usage guide](docs/USAGE.md) for examples.

## Build and Test

Build, test, and install tasks are wrapped by `make`. Use Python 3.8–3.12 and pip
for protocol generation.
The generation script installs pinned Python protocol generators from
[requirements-codegen.txt](requirements-codegen.txt) into `build/codegen-deps/`;
it does not depend on `protoc` or `grpc_python_plugin` from `PATH`.
Python and runtime dependency versions for published wheels are declared in [pyproject.toml](pyproject.toml).
The generators are not included in the SDK wheel.

Build and install the SDK:

```bash
make install
```

Installation replaces the SDK even when its version is unchanged, while reusing
installed runtime dependencies that satisfy its requirements.

Choose an existing virtual environment with `PYTHON`:

```bash
make install PYTHON=/opt/openevent-sdk/bin/python
```

Select the installation environment with `PYTHON`. Use `INSTALL_ARGS` for pip
options such as `--no-index` and `--find-links`.

Other common commands:

```bash
make build  # Build only; write the wheel to dist/
make test   # Run SDK unit tests
make clean  # Remove build products and temporary files
```

Run end-to-end tests against a real OpenEvent server:

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

End-to-end tests require `openevent-sdk>=0.11.1`, `pytest`, and `packaging` to be
installed in the current Python environment, plus an executable server binary.
Logs are kept under `build/e2e/cases/`. Pass pytest options with `TEST_ARGS` and
select Python with `PYTHON`.

Prepare an importable package for local development:

```bash
make init
PYTHONPATH="$PWD/build/python" python3 -B -c 'from openevent.sdk import OpenEventClient'
```

Rerun `make init` after editing `src/` or the proto files; `make build` and
`make test` refresh it automatically.

## Documentation

- [Business protocol definition](proto/openevent.proto)
- [Admin protocol definition](proto/admin.proto)
- [Usage guide](docs/USAGE.md)
- [API contract](docs/API.md)
- [System protocol](docs/SYSTEM_PROTOCOL.md)
