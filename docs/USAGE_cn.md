# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段和边界语义以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建 wheel：

```bash
make build
python3 -m pip install dist/openevent_sdk-0.8.0-py3-none-any.whl
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
大于 0 的数值；`None`、bool、0、负数、NaN 和无穷值都会抛出 `ValueError`，构造和重新赋值使用相同校验。该 deadline 作用于
全部非流式 RPC。`OpenEventClient.subscribe` 特意不设置 RPC deadline：空闲但健康的订阅会一直保持，
直到调用方取消、服务端结束或 gRPC 传输失败。

`OpenEventClient` 自己创建的 channel 会对 Subscribe 使用公开的 gRPC 传输层保活策略。这是传输层连通性
检查，不是 OpenEvent 消息，因此不会出现在订阅中。连接断开后，订阅会结束；如果注册了完成回调，也会执行，
不需要等待单个 RPC 的 deadline。通过 `channel=...` 传入的 channel 由调用方拥有和配置；用它订阅时，调用方
必须设置服务端接受的保活策略。PING 间隔和兼容性要求以 [Subscribe API 契约](API_cn.md#37-subscribe) 为准。

client 不再使用时应调用 `client.close()`，也可以把它用作 context manager。`close()` 可以重复调用。
client 只关闭自己创建的 gRPC channel；通过 `channel=...` 传入的 channel 始终由调用方管理和关闭。
析构函数只是在遗漏显式关闭时兜底，因为 Python 不保证它何时执行。

```python
with OpenEventClient("127.0.0.1:9527") as scoped_client:
    status = scoped_client.get_status(principal, token)
```

### RPC 返回值

| 方法 | 返回值 |
| --- | --- |
| `get_uuid()` | Python `int`，表示一个已分配的 UUID |
| `get_seq_by_uuid(uuid)` | Python `int`，表示该 UUID 对应的已提交消息 seq |
| `subscribe(...)` | 兼容 gRPC stream 的迭代器，每次产出一个 `SubscribeResponse` |
| 其余业务和管理 RPC 封装方法 | 对应的原始 protobuf response，包括返回空 response 的 RPC |

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
    members=[1002, 1003],
)

channel_id = created.channel.channel_id
```

可见性：

- `VISIBILITY_PUBLIC`：所有已认证用户可读写。
- `VISIBILITY_PROTECTED`：所有已认证用户可读，成员可写。
- `VISIBILITY_PRIVATE`：成员可读写。

保留系统 Channel 的 `channel_id=0`、name 为 `system`、protocol 为 `system.v1`。它由服务端拥有且只读。
每个完成初始化的服务端都永久保存它的 `seq=0` 初始化消息；payload schema 见
[系统协议](SYSTEM_PROTOCOL_cn.md)。业务 principal 必须大于 0。

## 发布消息

每次逻辑发布前调用 `get_uuid()` 取出一个 UUID。每个 client 有自己的内存池；池空时调用一次
`AllocateUuids(count=1024)`，按返回顺序逐个取出。池不持久化、不在 client 间共享，关闭 client 或退出进程
会丢弃尚未取出的 UUID。发布时显式传入 UUID，SDK 不自动填充或重试。

最简单的方式是让服务端分配全局 `seq`：

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

SDK 自建的 gRPC channel 把单条收发消息上限固定为 64 MiB。调用方注入自定义 channel 时必须自行设置
足够的 `grpc.max_send_message_length` 和 `grpc.max_receive_message_length`；读取旧消息所需的传输上限
不随当前 payload 写入限制降低，见 [Payload API](API_cn.md#23-payload)。

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
    from_seq=0,
    limit=100,
    channels=[channel_id],
)

for message in response.messages:
    print(message.seq, message.channel_id, message.payload)

next_seq = response.next_seq
last_seq = response.last_seq
```

上例只读取指定业务 Channel，并过滤系统 Channel。继续读取时，把响应的 `next_seq` 作为新的 `from_seq`；
`next_seq > last_seq` 表示到达本页观察到的尾部。短页或空页不一定表示结束，完整过滤、分页和错误规则见
[Fetch API](API_cn.md#36-fetch)。

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

`subscribe` 返回兼容 gRPC stream 的迭代器；它提供 `cancel()` 和 `wait_started(timeout)`，同时执行下述
SDK 序号检查：

```python
for item in client.subscribe(
    principal,
    token,
    from_seq=0,
    channels=[channel_id],
):
    message = item.message
    print(message.seq, message.payload)
```

`wait_started(timeout)` 等待服务端发送初始响应 metadata。调用方需要区分“服务端已经接受订阅”和“仍在尝试
建立连接”时可使用它。`timeout` 必须是大于 0 的有限秒数；超时会取消本次 stream 并抛出 `TimeoutError`。
服务端接受前发生的 RPC 错误原样抛出。成功返回不为已经建立的 stream 设置 deadline，也不等待任何
OpenEvent 消息。

上例从 seq 0 开始扫描所选业务 Channel，到达尾部后继续等待新消息。起点边界、权限变化导致的结束或
过滤，以及服务端关闭后的恢复方式统一见 [Subscribe API](API_cn.md#37-subscribe)。

高层 `OpenEventClient.subscribe` 迭代器会在同一条 stream 内按消息 `seq` 丢弃重复或回退的消息。
SDK 不自动重连，去重水位也不跨连接。调用方保存最后处理的 seq，并按 API 恢复规则处理重放；需要原始
stream 时可以直接使用生成式 gRPC stub。

## 定向消息

发布时可以传入 `recipients`。下面使用创建 Channel 示例中已经加入的成员 1002 和 1003：

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

读取时设置 `only_my_recipient=True` 可筛选发给自己的消息；成员要求与过滤语义见
[Recipients API](API_cn.md#26-recipients)。

## 管理 token

`AdminClient` 访问只运行在独立管理端口的 `AdminService`。

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

binding = admin.add_token(target_principal=1001).binding
print("已创建 token，principal:", binding.principal)

admin.delete_token(binding.token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

上例创建并删除 token，再读取一页管理消息。消息下一页使用 `next_seq` 继续。
管理查询契约见 [AdminService API](API_cn.md#6-adminservice)；部署时必须保护管理端口。

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

生成 proto、构建、安装、测试和清理的命令统一见 [README 的构建和测试](../README_cn.md#构建和测试)。
