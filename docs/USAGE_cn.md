# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段和边界语义以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建 wheel：

```bash
make build
python3 -m pip install dist/openevent_sdk-0.3.0-py3-none-any.whl
```

开发本仓库时，可以直接生成 protobuf 代码并使用源码：

```bash
make init
export PYTHONPATH="$PWD/src"
```

## 连接服务

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` 是 gRPC 服务地址。默认使用 `grpc.insecure_channel`；如果你的部署需要 TLS
或自定义 channel，可以创建 `grpc.Channel` 后传给 `OpenEventClient(target, channel=...)`。

## 创建 Channel

```python
from openevent.sdk.proto import openevent_pb2

created = client.create_channel(
    principal=principal,
    token=token,
    name="general",
    visibility=openevent_pb2.VISIBILITY_PUBLIC,
    protocol="text/plain",
    description="Community discussion",
)

channel_id = created.channel.channel_id
```

可见性：

- `VISIBILITY_PUBLIC`：所有已认证用户可读写。
- `VISIBILITY_PROTECTED`：所有已认证用户可读，成员可写。
- `VISIBILITY_PRIVATE`：成员可读写。

## 发布消息

最简单的方式是让服务端分配全局 `seq`：

```python
payload = "hello OpenEvent".encode("utf-8")

result = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=payload,
)

print(result.seq)
```

如果需要客户端自己做 CAS 发布，可以先查询状态，再使用 `publish`：

```python
status = client.get_status(principal, token)
next_seq = status.max_seq + 1

client.publish(
    principal=principal,
    token=token,
    channel_id=channel_id,
    seq=next_seq,
    payload=b"message with expected seq",
)
```

`payload` 是不透明字节数组，SDK 不解析内容。你可以自行使用 JSON、MessagePack、
自定义二进制协议或普通文本。

## 拉取消息

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
)

for message in response.messages:
    print(message.seq, message.channel_id, message.payload)

next_seq = response.next_seq
last_seq = response.last_seq
```

`response.messages` 包含 `EventMessage`。传入 `channels=[...]` 时只读取指定 channel；
省略或传空列表时读取调用方可见的所有 channel。继续读取时，把上一次响应的 `next_seq`
作为新的 `from_seq`。如果 `next_seq <= last_seq`，继续 Fetch 可能还会扫描到更多已提交消息；
如果 `next_seq > last_seq`，表示本次响应已经到达当前已提交尾部。

如果只想读取定向发送给自己的消息：

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
    only_my_recipient=True,
)
```

## 订阅消息

`subscribe` 返回 gRPC stream：

```python
for item in client.subscribe(principal, token, from_seq=0):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` 表示从当前最新消息之后开始等待新消息。如果连接断开，客户端应记录已经处理到的
`seq`，之后用下一条期望的 `seq` 重新订阅或拉取。

## 定向消息

发布时可以传入 `recipients`：

```python
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"private note for selected members",
)
```

`recipients` 中的 principal 必须是对应 Channel 的成员。读取时设置
`only_my_recipient=True` 会只返回 `recipients` 包含当前 `principal` 的消息。

## 管理 token

`AdminClient` 访问 `AdminService`。该服务通常运行在独立管理端口，是否暴露给外部取决于部署配置。

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

binding = admin.add_token(target_principal=1001).binding
print(binding.principal, binding.token)

for item in admin.list_tokens().bindings:
    print(item.principal, item.token)

admin.delete_token(binding.token)
```

## 错误处理

SDK 直接暴露 gRPC 调用错误。调用方应捕获 `grpc.RpcError` 并根据 status code 处理：

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, b"hello")
except grpc.RpcError as exc:
    if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
        print("invalid token")
    elif exc.code() == grpc.StatusCode.PERMISSION_DENIED:
        print("no permission")
    else:
        raise
```

常见状态码和完整语义见 [API 契约](API_cn.md)。

## 本地开发

常用命令：

```bash
make init
make test
make build
make clean
```

`make init` 会把 Python protobuf 代码生成到
[`src/openevent/sdk/proto/`](../src/openevent/sdk/proto/)。
生成的 `openevent_pb2*.py` 不进 git，但会被构建进 wheel。

`make build` 使用 Hatchling 构建 wheel，构建依赖、测试依赖、缓存和临时文件放在 `build/`，
最终产物放在 `dist/`。
