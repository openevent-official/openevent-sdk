# openevent-sdk

[English version](README.md)

`openevent-sdk` 包含 OpenEvent 共享的 Protocol Buffers 协议、API 契约文档、
公开的 `system.v1` 系统消息协议和 Python SDK。

可以用 Python SDK 发布和读取消息、存取对象、管理频道或调用管理接口。也可以直接使用根据协议生成的 gRPC 客户端。
调用示例见[使用指南](docs/USAGE_cn.md)。

## 构建和测试

在 SDK 根目录运行 Make。用 `PYTHON` 选择已安装 pip 且允许写入的 Python 环境，默认使用当前终端的 `python3`。从源码生成协议代码使用 Python 3.9–3.12；安装已发布的 wheel 只需满足 [Python 版本和运行依赖声明](pyproject.toml)。

如果用虚拟环境运行 `make test`，创建该环境的 Python 解释器也必须装有 pip。安装测试会创建临时虚拟环境，复用这个基础解释器的 pip。

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

在同一工作目录中顺序执行这些操作，同一 Python 环境一次只运行一项安装。安装入口在构建前检查环境变量和 `pip.conf` 中的安装位置。如果配置会把包装到别处，就报错并指出具体设置；`test-env` 还会拒绝安装到用户目录。

构建和安装共用 pip 的包源设置。离线时，在 `build/wheelhouse/` 备齐与当前 Python 和平台匹配的构建与运行依赖，包括间接依赖。要跑测试，还需备齐 `[test]` 依赖。随后执行：

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
