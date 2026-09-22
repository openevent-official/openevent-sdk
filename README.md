# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, the public `system.v1` initialization-message protocol,
and Python SDK.

The Python SDK is a lightweight wrapper around generated gRPC stubs. It does not
add application-level business semantics. Its high-level subscription iterator
suppresses repeated or backward message `seq` values within one stream. The
high-level client exposes event, immutable object storage, and Channel RPCs, including individual UUID retrieval from
an internal pool for deduplicated publishing and committed-sequence lookup by UUID;
generated stubs remain available when raw gRPC access is required.

## Directory Layout

```text
openevent-sdk/
├── proto/
│   ├── admin.proto
│   └── openevent.proto
├── docs/
│   ├── API.md
│   ├── SYSTEM_PROTOCOL.md
│   └── USAGE.md
├── Makefile
├── build.sh
├── generate_python_proto.sh
├── scripts/
│   └── install.py
├── test.sh
├── src/
│   └── openevent/
│       └── sdk/
│           ├── __init__.py
│           ├── admin_client.py
│           ├── client.py
│           └── proto/
│               └── __init__.py
└── pyproject.toml
```

`src/openevent/sdk/proto/*_pb2*.py` is generated from `proto/openevent.proto`
and `proto/admin.proto` and is not tracked by Git. Generate it before local
debugging, builds, or tests.

## Build and Test

Build, test, and install tasks are wrapped by `make`. The `build/` directory is
reserved for temporary build dependencies, test dependencies, caches, and
temporary files. Wheel artifacts are written to `dist/`.

Generate Python protobuf modules for local debugging:

```bash
make init
```

Build only, without installing into the current Python environment:

```bash
make build
```

The wheel is written to:

```text
dist/openevent_sdk-0.8.1-py3-none-any.whl
```

Build and install the generated wheel:

```bash
make install
```

Installation uses only the wheel from the successful build. It checks the wheel's
declared dependencies, reuses compatible installed dependencies, and then replaces
only the SDK itself, even when its version is unchanged.

Choose an existing virtual environment with `PYTHON`:

```bash
make install PYTHON=/opt/openevent-sdk/bin/python
```

`INSTALL_ARGS` accepts ordinary `pip install` options, such as `--no-index` and
`--find-links`. Changing the destination with `--target`/`-t`, `--prefix`, `--root`,
or `PIP_TARGET`, `PIP_PREFIX`, `PIP_ROOT` is not supported; use `PYTHON` instead.

Run SDK unit tests, generating the protocol and installing the package in a test-only directory:

```bash
make test
```

Run end-to-end tests against a real OpenEvent server:

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

End-to-end tests use the `openevent-sdk>=0.8.0` package already installed in
the current Python environment. They do not install this repository into a
temporary dependency directory or generate SDK protobuf files. The current Python
environment also needs `pytest` and `packaging`. Each case uses independent data
and temporary ports, starts and stops its server, and keeps logs under
`build/e2e/cases/`. Pass pytest options with `TEST_ARGS` and select Python with `PYTHON`.

Clean build products and temporary files:

```bash
make clean
```

## Documentation

- [Business protocol definition](proto/openevent.proto)
- [Admin protocol definition](proto/admin.proto)
- [Usage guide](docs/USAGE.md)
- [API contract](docs/API.md)
- [System protocol](docs/SYSTEM_PROTOCOL.md)
- [Business client](src/openevent/sdk/client.py)
- [Admin client](src/openevent/sdk/admin_client.py)

`docs/API.md` only documents public fields, RPC behavior, error semantics, and
compatibility guidance. It does not describe server implementation details.
`docs/SYSTEM_PROTOCOL.md` is the authoritative public schema for the permanent
seq-0 initialization message in the reserved system Channel.
