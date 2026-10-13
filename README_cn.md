# openevent-sdk

[English version](README.md)

`openevent-sdk` 包含 OpenEvent 共享的 Protocol Buffers 协议、API 契约文档、
公开的 `system.v1` 系统消息协议和 Python SDK。

Python SDK 提供消息、对象、Channel 和管理接口的轻量客户端，也可直接使用生成的 gRPC stub。
调用示例见[使用指南](docs/USAGE_cn.md)。

## 构建和测试

构建、测试和安装统一通过 `make` 执行；协议生成使用 Python 3.9–3.12 和 pip。生成脚本按
[requirements-codegen.txt](requirements-codegen.txt) 将固定版本的 Python 协议生成工具安装到
`build/codegen-deps/`，不依赖 `PATH` 中的 `protoc` 或 `grpc_python_plugin`。
安装已发布 wheel 所需的 Python 和运行依赖版本由 [pyproject.toml](pyproject.toml) 声明；生成工具不进入 SDK wheel。

构建并安装 SDK：

```bash
make install
```

`make install` 先构建，再安装本次成功构建的 wheel。
安装会替换 SDK，即使版本号没有变化；已满足要求的运行依赖会继续使用。
构建和安装的临时文件都放在 `build/` 下，禁用 pip 持久缓存。

通过 `PYTHON` 选择已有的虚拟环境：

```bash
make install PYTHON=/opt/openevent-sdk/bin/python
```

安装位置由 `PYTHON` 选择；`INSTALL_ARGS` 用于传递 `--no-index`、`--find-links` 等 pip 参数。

其他常用命令：

```bash
make build  # 只构建，wheel 输出到 dist/
make test   # 构建当前源码，并测试生成的 wheel
make clean  # 清理构建产物和临时文件
```

使用真实 OpenEvent server 运行端到端测试：

```bash
OPENEVENT_SERVER_BIN=<openevent_server_binary> make e2e
```

`make test` 和 `make e2e` 都先从当前源码构建 wheel，再通过 `make test-env` 安装到
`build/test-env/` 下的独立虚拟环境；无需预先安装 SDK 或测试依赖。每次都替换 SDK，即使版本号
没有变化；已满足声明的第三方依赖继续使用，只补齐缺失或不兼容的依赖。构建或安装失败时不运行测试。
测试忽略外部 `PYTHONPATH` 和 `PYTHONHOME`，并检查 SDK 确实从该测试环境导入，不修改当前环境中的 SDK。

端到端测试还需提供可执行的服务端二进制文件。单元测试临时文件保存在 `build/unit/`，
端到端日志保存在 `build/e2e/cases/`。用 `TEST_ARGS` 传递 pytest 参数，用 `PYTHON` 选择构建解释器。
需要复用同一构建结果运行其他测试时，先执行 `make test-env`，再使用
`build/test-env/bin/python -B`；该入口每次都会重新构建和安装当前源码。

本地开发用一个入口准备或刷新 SDK 和依赖，再使用准备好的 Python 环境运行代码：

```bash
make dev
build/test-env/bin/python -B -c 'from openevent.sdk import OpenEventClient'
```

`make dev` 复用上述 `make test-env` 流程，构建并安装当前 wheel，准备运行和测试依赖；
无需先运行 `make install` 或设置 `PYTHONPATH`。开发和测试使用同一个 `build/test-env/` 环境，
用 `PYTHON` 选择构建解释器。修改 `src/` 或 proto 后重新运行 `make dev`；`make test` 和
`make e2e` 也会自动刷新该环境。

仅需生成协议代码和包暂存文件时可以运行 `make init`；该命令不准备运行依赖。

## 文档

- [业务协议定义](proto/openevent.proto)
- [管理协议定义](proto/admin.proto)
- [使用指南](docs/USAGE_cn.md)
- [API 契约](docs/API_cn.md)
- [系统协议](docs/SYSTEM_PROTOCOL_cn.md)
