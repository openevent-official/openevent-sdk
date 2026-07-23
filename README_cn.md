# openevent-sdk

[English version](README.md)

`openevent-sdk` 包含 OpenEvent 共享的 Protocol Buffers 协议、API 契约文档
和 Python SDK。

Python SDK 是生成的 gRPC stub 的轻量封装，不增加应用层业务语义。高层订阅迭代器只在单条
stream 内过滤重复或回退的消息 `seq`；需要原始 stream 时仍可直接使用生成式 stub。

## 目录结构

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

`src/openevent/sdk/proto/*_pb2*.py` 由 `proto/openevent.proto` 和
`proto/admin.proto` 生成，不进 git；构建、测试或本地调试前由脚本生成到该目录。

## 构建和测试

构建、测试和安装统一通过 `make` 执行。`build/` 是保留的临时构建目录，只放构建依赖、测试依赖、缓存和临时文件；最终 wheel 输出到 `dist/`。

本地调试前生成 Python protobuf 模块：

```bash
make init
```

只构建、不安装到当前 Python 环境：

```bash
make build
```

构建完成后，wheel 位于：

```text
dist/openevent_sdk-0.4.1-py3-none-any.whl
```

构建并安装生成的 wheel：

```bash
make install
```

需要指定安装路径时，通过 `INSTALL_ARGS` 传递 `pip install` 参数：

```bash
make install INSTALL_ARGS="--target /opt/openevent-sdk"
make install INSTALL_ARGS="--prefix /opt/openevent-sdk"
```

运行测试；当前没有测试文件时会执行 SDK 导入和 protobuf smoke check：

```bash
make test
```

使用真实 OpenEvent server 运行端到端测试：

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

端到端测试使用当前 Python 环境中已经安装好的 `openevent-sdk>=0.4.1` 包；不会把
本仓库安装到临时依赖目录，也不会在 e2e 脚本里生成 SDK protobuf 文件。

清理构建产物和临时文件：

```bash
make clean
```

## 文档

- [业务协议定义](proto/openevent.proto)
- [管理协议定义](proto/admin.proto)
- [使用指南](docs/USAGE_cn.md)
- [API 契约](docs/API_cn.md)
- [业务客户端](src/openevent/sdk/client.py)
- [管理客户端](src/openevent/sdk/admin_client.py)

`docs/API.md` 只描述公开字段、RPC 行为、错误语义和兼容性建议，不记录服务端
实现细节。
