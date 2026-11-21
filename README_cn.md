# openevent-sdk

[English version](README.md)

`openevent-sdk` 包含 OpenEvent 共享的 Protocol Buffers 协议、API 契约文档、
公开的 `system.v1` 系统消息协议和 Python SDK。

可以用 Python SDK 发布和读取消息、存取对象、管理频道或调用管理接口。也可以直接使用根据协议生成的 gRPC 客户端。
调用示例见[使用指南](docs/USAGE_cn.md)。

## 构建和测试

在 SDK 根目录运行 Make。用 `PYTHON` 选择装有 pip 的解释器，默认使用当前终端的 `python3`。系统 Python 和虚拟环境都可使用。从源码生成协议代码使用 Python 3.9–3.12；安装已发布的 wheel 只需满足 [Python 版本和运行依赖声明](pyproject.toml)。

`make install`、`make test-env`、`make test` 和 `make e2e` 要求 pip 22.3 或更高版本。如果用虚拟环境运行 `make test`，创建该环境的基础解释器也要满足这项要求，因为安装测试会复用它的 pip。

已有环境可以直接使用。需要新环境时，可以先执行：

```bash
python3 -B -m venv build/venv
export PYTHON="$PWD/build/venv/bin/python"
```

| 命令 | 用途 |
| --- | --- |
| `make build` | 生成协议模块，将它们与手写 Python 模块打成 wheel，发布到 `dist/`。 |
| `make install` | 构建并安装本次 wheel，替换同版本的旧 SDK，复用符合要求的运行依赖。 |
| `make test-env` | 构建并安装本次 SDK 和 `[test]` 依赖，供所选 Python 运行测试或自己的脚本。 |
| `make test` | 运行 `test-env`，再用同一个 Python 跑单元测试。 |
| `make e2e` | 运行 `test-env`，再用指定的真实服务端跑端到端测试。 |
| `make clean` | 清理 SDK 自己的构建文件、测试数据和发布包，保留 Python 环境、离线依赖和其他项目的产物。 |

在同一工作目录中顺序执行这些操作。同一 Python 环境一次只运行一项安装。

部署时用运行应用的 Python 安装 SDK，例如 `make install PYTHON=/opt/myapp/venv/bin/python`。安装遵循系统的权限和软件包管理规则。如果解释器允许用户安装并能导入用户目录里的包，也可以用 `PIP_USER=1 make install` 或在 `pip.conf` 中设置 `user = true`。

在该环境中重装同版本时，`make install` 替换 SDK 的代码，移除旧版独有的文件，保留共享 `openevent` 目录的其他包。pip 只补齐缺少或版本不符合要求的依赖。

要把 SDK 和依赖装进指定目录，先运行 `make build`，将输出的 wheel 路径赋给变量 `wheel`，再用与部署环境兼容的 Python 运行以下一条命令。

- `python3 -m pip install --target ./packages "$wheel"` 把 Python 包直接安装到 `./packages/`。
- `python3 -m pip install --prefix /opt/myapp "$wheel"` 按 Python 的目录结构把库和脚本装到 `/opt/myapp` 下。

指定目录里的依赖和已有文件由 pip 处理。`make install` 的同版本更新承诺适用于所选 Python 环境，调用时须清除环境变量或 `pip.conf` 中指定其他 `prefix` 或 `target` 的设置。

普通安装和测试准备都在构建前拒绝 pip 的 `--python` 和 `--root` 设置。`test-env`、`test` 和 `e2e` 用所选解释器直接安装并导入本次 SDK，测试准备还会在构建前指出并拒绝显式启用的 `user` 或指定的 `prefix`、`target`。

构建和安装都读取 pip 的包源设置。离线时，在 `build/wheelhouse/` 备齐与当前 Python 和平台匹配的构建与运行依赖，包括间接依赖。要跑测试，还需备齐 `[test]` 依赖。随后执行：

```bash
PIP_NO_INDEX=1 PIP_FIND_LINKS=build/wheelhouse make install
```

将 `OPENEVENT_SERVER_BIN` 指向本次构建成功的服务端程序。一起运行两套测试时，SDK 只构建和安装一次：

```bash
OPENEVENT_SERVER_BIN=/path/to/openevent_server make test e2e
```

用 `TEST_ARGS` 传递 pytest 参数，例如 `make test TEST_ARGS=-q`。单元测试数据放在 `build/unit/`，端到端用例的数据和日志放在 `build/e2e/`。

## 文档

- [业务协议定义](proto/openevent/sdk/proto/openevent.proto)
- [管理协议定义](proto/openevent/sdk/proto/admin.proto)
- [使用指南](docs/USAGE_cn.md)
- [API 契约](docs/API_cn.md)
- [系统协议](docs/SYSTEM_PROTOCOL_cn.md)
