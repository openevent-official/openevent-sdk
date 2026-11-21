# openevent-sdk

[中文版](README_cn.md)

`openevent-sdk` contains the shared OpenEvent Protocol Buffers schema, API
contract documentation, the public `system.v1` system-message protocol,
and Python SDK.

The Python SDK provides lightweight clients for messages, objects, Channels, and
administration. Generated gRPC stubs are also available for direct use.
See the [usage guide](docs/USAGE.md) for examples.

## Build and Test

Run Make from the SDK root. Use `PYTHON` to select an interpreter with pip installed. It defaults to the current shell's `python3`. Both system Python and virtual environments are supported. Building from source uses Python 3.9–3.12 for protocol generation. Installing a published wheel requires only the [declared Python version and runtime dependencies](pyproject.toml).

`make install`, `make test-env`, `make test`, and `make e2e` require pip 22.3 or later. If you run `make test` in a virtual environment, its base interpreter must also meet this requirement because the installation tests use its copy of pip.

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

Run these operations sequentially in a given checkout, with one installation at a time per Python environment.

For deployment, install the SDK with the interpreter that runs the application, for example `make install PYTHON=/opt/myapp/venv/bin/python`. Installation follows the system's permissions and package management policy. If the interpreter allows user installs and can import packages from the user directory, you can also use `PIP_USER=1 make install` or set `user = true` in `pip.conf`.

When reinstalling the same version in that environment, `make install` replaces the SDK code, removes files unique to the old build, and preserves other packages in the shared `openevent` directory. Pip installs only missing dependencies or versions needed to meet the requirements.

To install the SDK and its dependencies into a specific directory, run `make build`, set the shell variable `wheel` to the wheel path printed by the build, and run one of the following commands with a Python compatible with the deployment environment.

- `python3 -m pip install --target ./packages "$wheel"` places the Python packages directly in `./packages/`.
- `python3 -m pip install --prefix /opt/myapp "$wheel"` installs libraries and scripts under `/opt/myapp` using Python's installation layout.

Pip handles dependencies and existing files in those directories. The same-version update guarantee applies to `make install` in the selected Python environment. When running that command, clear any environment or `pip.conf` settings that redirect installation to a separate prefix or target.

Both installation and test setup reject pip's `--python` and `--root` settings before building. The selected interpreter installs and imports the current SDK for `test-env`, `test`, and `e2e`. Test setup also reports and rejects an explicitly enabled `user` or a configured `prefix` or `target` before building.

Both the build and installation read pip's package source settings. For an offline installation, put the build and runtime dependencies for your Python and platform in `build/wheelhouse/`, including their transitive dependencies. Include the `[test]` dependencies to run tests. Then run:

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
