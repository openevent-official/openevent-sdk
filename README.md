# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, the public `system.v1` system-message protocol,
and Python SDK.

The Python SDK provides lightweight clients for messages, objects, Channels, and
administration. Generated gRPC stubs are also available for direct use.
See the [usage guide](docs/USAGE.md) for examples.

## Build and Test

Run Make from the SDK root. Use `PYTHON` to select a writable Python environment with pip installed. It defaults to the current shell's `python3`. Building from source uses Python 3.9–3.12 for protocol generation. Installing a published wheel requires only the [declared Python version and runtime dependencies](pyproject.toml).

If you run `make test` in a virtual environment, the Python interpreter used to create that environment must also have pip installed. The installation tests create temporary virtual environments and reuse pip from that base interpreter.

You can use an existing environment. To create one, run:

```bash
python3 -B -m venv build/venv
export PYTHON="$PWD/build/venv/bin/python"
```

| Command | Purpose |
| --- | --- |
| `make build` | Generate the protocol modules, package them with the hand-written Python modules, and publish the wheel to `dist/`. |
| `make install` | Build and install the current wheel, replace an older copy of the same SDK version, and reuse compatible runtime dependencies. |
| `make test-env` | Build and install the current SDK and its `[test]` dependencies so the selected Python can run tests or your own scripts. |
| `make test` | Run `test-env`, then run the unit tests with the same Python. |
| `make e2e` | Run `test-env`, then run end-to-end tests against the supplied server executable. |
| `make clean` | Remove the SDK's build files, test data, and release packages. Preserve Python environments, offline dependencies, and artifacts from other projects. |

Run these operations sequentially in a given checkout, with one installation at a time per Python environment. Before building, the installation targets check where pip would install packages according to its environment variables and `pip.conf`. They report settings that redirect installation. The `test-env` target also rejects installation into the user site.

The build and installation share pip's package source settings. For an offline installation, put the build and runtime dependencies for your Python and platform in `build/wheelhouse/`, including their transitive dependencies. Include the `[test]` dependencies to run tests. Then run:

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=build/wheelhouse make install
```

Set `OPENEVENT_SERVER_BIN` to the executable from the current successful server build. Running both suites together builds and installs the SDK once:

```bash
OPENEVENT_SERVER_BIN=/path/to/openevent_server make test e2e
```

Pass pytest options with `TEST_ARGS`, for example `make test TEST_ARGS=-q`. Unit-test data lives in `build/unit/`. End-to-end data and logs live in `build/e2e/`.

## Documentation

- [Business protocol definition](proto/openevent/sdk/proto/openevent.proto)
- [Admin protocol definition](proto/openevent/sdk/proto/admin.proto)
- [Usage guide](docs/USAGE.md)
- [API contract](docs/API.md)
- [System protocol](docs/SYSTEM_PROTOCOL.md)
