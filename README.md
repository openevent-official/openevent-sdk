# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, the public `system.v1` system-message protocol,
and Python SDK.

The Python SDK provides lightweight clients for messages, objects, Channels, and
administration. Generated gRPC stubs are also available for direct use.
See the [usage guide](docs/USAGE.md) for examples.

## Build and Test

Build, test, and install tasks are wrapped by `make`. Use Python 3.9–3.12 and pip
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

`make install` builds first and installs only the wheel from that successful build.
Installation replaces the SDK even when its version is unchanged, while reusing
installed runtime dependencies that satisfy its requirements. Build and installation
temporary files stay under `build/`; pip's persistent cache is disabled.

Choose an existing virtual environment with `PYTHON`:

```bash
make install PYTHON=/opt/openevent-sdk/bin/python
```

Select the installation environment with `PYTHON`. Use `INSTALL_ARGS` for pip
options such as `--no-index` and `--find-links`.

Other common commands:

```bash
make build  # Build only; write the wheel to dist/
make test   # Build the current source and test the resulting wheel
make clean  # Remove build products and temporary files
```

Run end-to-end tests against a real OpenEvent server:

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

Both `make test` and `make e2e` build a wheel from the current source, then use
`make test-env` to install it into a separate virtual environment under
`build/test-env/`. No preinstalled SDK or test dependencies are required. The SDK
is replaced on every run, even at the same version; third-party dependencies that
satisfy its declarations are reused, and only missing or incompatible dependencies
are installed. Tests do not run if the build or installation fails. Test entry
points ignore external `PYTHONPATH` and `PYTHONHOME` and verify that the SDK is
imported from the test environment, leaving the current environment's SDK unchanged.

End-to-end tests also require an executable server binary. Unit-test temporary
files stay under `build/unit/`, and end-to-end logs under `build/e2e/cases/`.
Pass pytest options with `TEST_ARGS` and select the build interpreter with `PYTHON`.
To run other tests against the same build, first run `make test-env`, then use
`build/test-env/bin/python -B`; this target always rebuilds and installs the current source.

For local development, prepare or refresh the SDK and its dependencies with one
target, then run code with the prepared Python environment:

```bash
make dev
build/test-env/bin/python -B -c 'from openevent.sdk import OpenEventClient'
```

`make dev` reuses the `make test-env` workflow above: it builds and installs the
current wheel and prepares runtime and test dependencies. No prior
`make install` or `PYTHONPATH` setting is needed. Development and tests share
`build/test-env/`; select the build interpreter with `PYTHON`. Rerun `make dev`
after editing `src/` or the proto files; `make test` and `make e2e` also refresh
this environment automatically.

Use `make init` when only generated protocol code and package staging are needed;
it does not prepare runtime dependencies.

## Documentation

- [Business protocol definition](proto/openevent.proto)
- [Admin protocol definition](proto/admin.proto)
- [Usage guide](docs/USAGE.md)
- [API contract](docs/API.md)
- [System protocol](docs/SYSTEM_PROTOCOL.md)
