# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段和边界语义以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建 wheel：

```bash
make build
python3 -m pip install dist/openevent_sdk-0.6.0-py3-none-any.whl
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

`OpenEventClient` 和 `AdminClient` 的构造函数都接受 `timeout`，默认值为 30 秒。该值必须是有限且
大于 0 的数值；`None`、bool、0、负数、NaN 和无穷值都会抛出 `ValueError`。同一 deadline 作用于
所有 RPC，包括 `subscribe` 整条 stream 的生命周期，因此高层 client 调用不会无限等待。

client 不再使用时应调用 `client.close()`，也可以把它用作 context manager。`close()` 可以重复调用。
client 只关闭自己创建的 gRPC channel；通过 `channel=...` 传入的 channel 始终由调用方管理和关闭。
析构函数只是在遗漏显式关闭时兜底，因为 Python 不保证它何时执行。

```python
with OpenEventClient("127.0.0.1:9527") as scoped_client:
    status = scoped_client.get_status(principal, token)
```

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

每次逻辑发布前从 SDK 取出一个 UUID。SDK 在内部维护并补充内存 UUID 池。最简单的方式是让服务端分配全局 `seq`：

```python
payload = "hello OpenEvent".encode("utf-8")
uuid = client.get_uuid()

result = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=payload,
    uuid=uuid,
)

print(result.seq)
```

如果需要客户端自己做 CAS 发布，可以先查询状态，再使用 `publish`：

```python
status = client.get_status(principal, token)
next_seq = status.max_seq + 1
uuid = client.get_uuid()

client.publish(
    principal=principal,
    token=token,
    channel_id=channel_id,
    seq=next_seq,
    payload=b"message with expected seq",
    uuid=uuid,
)
```

`payload` 是不透明字节数组，SDK 不解析内容。你可以自行使用 JSON、MessagePack、
自定义二进制协议或普通文本。

发布结果不确定时，保留 UUID 并按 API 契约 2.9 节的恢复流程处理。最终的重复 UUID 步骤可以无需拉取历史消息即取得已提交 seq：

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, payload, uuid=uuid)
except grpc.RpcError as exc:
    if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
        raise
    committed_seq = client.get_seq_by_uuid(uuid)
```

`get_seq_by_uuid()` 返回已被 committed 消息消费的 UUID 所对应的 seq。其协议行为以 API 契约 3.3 节为准。

SDK 自建的 gRPC channel 默认把单条收发消息上限设为 64 MiB，以容纳默认 16 MiB payload 和分页
响应。调用方注入自定义 channel 时必须自行设置足够的 `grpc.max_send_message_length` 和
`grpc.max_receive_message_length`。

## 写入和引用对象

高层 client 暴露对象存储方法，发布消息时也可以直接传入 `object_keys`。

```python
from openevent.sdk.proto import openevent_pb2

written = client.write_object(
    principal=principal,
    token=token,
    name="report.json",
    type="application/json",
    description="Agent output",
    data=b'{"result":"ok"}',
)
key = openevent_pb2.ObjectKey(
    object_id=written.object_id,
    object_token=written.object_token,
)

published = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=b"report ready",
    object_keys=[key],
    uuid=client.get_uuid(),
)
```

使用 ObjectKey 读取 metadata 和字节范围。对象读取不携带 principal token，因为 ObjectKey 本身
就是授权：

```python
metadata = client.get_object_metadata(
    object_id=key.object_id,
    object_token=key.object_token,
)
chunk = client.read_object(
    object_id=key.object_id,
    object_token=key.object_token,
    offset=0,
    nbytes=metadata.nbytes,
)
```

对象不可变、不能为空且最大 4 MiB。ObjectKey 是永久、可转交的 bearer credential；把它附加到
消息会向所有能读取该消息的调用方披露 capability，包括管理端读取者。禁止记录对象 token。
精确限制、范围规则、错误和 WriteObject 结果不确定语义见 [API_cn.md](API_cn.md) 的 2.7 和 4 节。

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

`response.messages` 包含 `EventMessage`。传入 `channels=[...]` 时，列表中的每个 Channel 都必须存在且
调用方有读取权限；校验通过后只读取指定 Channel。任一 Channel 校验失败都会拒绝整个请求。省略或传空
列表时读取调用方可见的所有 Channel。继续读取时，把上一次响应的 `next_seq`
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

`subscribe` 返回兼容 gRPC stream 的迭代器；它保留 `cancel()` 等 gRPC call 方法，同时执行下述
SDK 序号检查：

```python
for item in client.subscribe(
    principal,
    token,
    from_seq=0,
    channels=[channel_id],
):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` 表示从当前最新位置之后开始等待新消息。传入 `channels=[...]` 时，列表中的每个 Channel
都必须存在且调用方有读取权限；校验通过后只接收这些 Channel 中调用方可读的消息。省略或传入空列表
时不按 Channel ID 过滤。订阅运行期间显式 Channel 失去读取权限时，stream 返回 `PERMISSION_DENIED` 并
结束。客户端 timeout 到期时 stream 以 `DEADLINE_EXCEEDED` 结束。任何中断发生前都应记录已经处理到的 `seq`，之后从 `seq + 1` 重新订阅
或拉取。

服务端开始正常或致命关闭时，会主动结束已经建立的 stream；客户端同样应记录最后处理到的 `seq`，
之后从 `seq + 1` 重新订阅或拉取。

高层 `OpenEventClient.subscribe` 迭代器会在同一条 stream 内按消息 `seq` 丢弃重复或回退的消息。
跨连接仍需由调用方持久化最后处理的 `seq`，并从 `seq + 1` 恢复；需要原始 stream 时可以直接
使用生成式 gRPC stub。

## 定向消息

发布时可以传入 `recipients`：

```python
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"private note for selected members",
    uuid=client.get_uuid(),
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
print("已创建 token，principal:", binding.principal)

page_token = ""
while True:
    token_page = admin.list_tokens(page_token=page_token, limit=100)
    print("本页 token binding 数量:", len(token_page.bindings))
    if not token_page.next_page_token:
        break
    page_token = token_page.next_page_token

admin.delete_token(binding.token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

`binding.token`、ListTokens 返回的 token 和分页使用的 page token 都是敏感凭据。示例只在内存中
使用这些值，不应把它们写入日志、trace、错误信息或普通控制台输出。

`ListTokens` 使用不透明 `page_token` 分页，`next_page_token=""` 表示结束；token 可增删，因此
跨页不构成强快照。`ListMessages` 是管理查询，不经过业务 Channel ACL 过滤；下一页使用
`next_seq` 作为包含起点的游标。部署时必须保护管理端口。

## 错误处理

SDK 直接暴露 gRPC 调用错误。调用方应捕获 `grpc.RpcError` 并根据 status code 处理：

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, b"hello", uuid=client.get_uuid())
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
生成的 `openevent_pb2*.py` 和 `admin_pb2*.py` 不进 git，但会被构建进 wheel。

`make build` 使用 Hatchling 构建 wheel，构建依赖、测试依赖、缓存和临时文件放在 `build/`，
最终产物放在 `dist/`。
