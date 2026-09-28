# openevent-sdk

[English version](README.md)

`openevent-sdk` 包含 OpenEvent 共享的 Protocol Buffers 协议、API 契约文档、
公开的 `system.v1` 初始化消息协议和 Python SDK。

Python SDK 提供消息、对象、Channel 和管理接口的轻量客户端，也可直接使用生成的 gRPC stub。
调用示例见[使用指南](docs/USAGE_cn.md)。

## 构建和测试

构建、测试和安装统一通过 `make` 执行。需要 Python、`protoc` 和 `grpc_python_plugin`，后两个命令必须在 `PATH` 中。

构建并安装 SDK：

```bash
make install
```

安装会替换 SDK，即使版本号没有变化；已满足要求的运行依赖会继续使用。

通过 `PYTHON` 选择已有的虚拟环境：

```bash
make install PYTHON=/opt/openevent-sdk/bin/python
```

安装位置由 `PYTHON` 选择；`INSTALL_ARGS` 用于传递 `--no-index`、`--find-links` 等 pip 参数。

其他常用命令：

```bash
make build  # 只构建，wheel 输出到 dist/
make test   # 运行 SDK 单元测试
make clean  # 清理构建产物和临时文件
```

使用真实 OpenEvent server 运行端到端测试：

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

端到端测试要求当前 Python 环境已安装 `openevent-sdk>=0.11.0`、`pytest` 和 `packaging`，
并提供可执行的服务端二进制文件。日志保存在 `build/e2e/cases/`。
用 `TEST_ARGS` 传递 pytest 参数，用 `PYTHON` 选择 Python 环境。

本地开发时准备可导入的包：

```bash
make init
PYTHONPATH="$PWD/build/python" python3 -B -c 'from openevent.sdk import OpenEventClient'
```

修改 `src/` 或 proto 后重新运行 `make init`；`make build` 和 `make test` 会自动刷新。

## 文档

- [业务协议定义](proto/openevent.proto)
- [管理协议定义](proto/admin.proto)
- [使用指南](docs/USAGE_cn.md)
- [API 契约](docs/API_cn.md)
- [系统协议](docs/SYSTEM_PROTOCOL_cn.md)
