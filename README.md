# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, and Python SDK.

The Python SDK is a lightweight wrapper around generated gRPC stubs. It does not
add application-level business semantics. Its high-level subscription iterator
only suppresses repeated or backward message `seq` values within one stream; the
generated stub remains available for raw stream access.

## Directory Layout

```text
openevent-sdk/
├── proto/
│   ├── admin.proto
│   └── openevent.proto
├── docs/
│   └── API.md
├── Makefile
├── build.sh
├── generate_python_proto.sh
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
dist/openevent_sdk-0.4.1-py3-none-any.whl
```

Build and install the generated wheel:

```bash
make install
```

Pass `pip install` options through `INSTALL_ARGS` when a custom install path is
needed:

```bash
make install INSTALL_ARGS="--target /opt/openevent-sdk"
make install INSTALL_ARGS="--prefix /opt/openevent-sdk"
```

Run tests. If there are no test files yet, this runs SDK import and protobuf
smoke checks:

```bash
make test
```

Run end-to-end tests against a real OpenEvent server:

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

End-to-end tests use the `openevent-sdk>=0.4.1` package already installed in
the current Python environment. They do not install this repository into a
temporary dependency directory or generate SDK protobuf files.

Clean build products and temporary files:

```bash
make clean
```

## Documentation

- [Business protocol definition](proto/openevent.proto)
- [Admin protocol definition](proto/admin.proto)
- [Usage guide](docs/USAGE.md)
- [API contract](docs/API.md)
- [Business client](src/openevent/sdk/client.py)
- [Admin client](src/openevent/sdk/admin_client.py)

`docs/API.md` only documents public fields, RPC behavior, error semantics, and
compatibility guidance. It does not describe server implementation details.
