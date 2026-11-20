# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段、接口行为和限制以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建并安装：

```bash
make install
```

选择 Python 环境和包源的方法见 [README 的构建和测试说明](../README_cn.md#构建和测试)。

## 连接服务

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` 是 gRPC 服务地址。`OpenEventClient` 和 `AdminClient` 创建并管理明文 gRPC 连接，
单条收发消息上限为 64 MiB。需要 TLS 或自定义连接参数时，可直接使用根据协议生成的 gRPC 客户端。

两个客户端都接受 `timeout_ms`，默认 30000 毫秒（30 秒），用于非流式 RPC。
构造或重新赋值时，数值必须大于 0，最大为 2,147,483,647 毫秒（约 24.9 天）。
传入布尔值或其他非法值会抛出 `ValueError`。
`timeout_ms` 控制普通 RPC 等待结果的时间，不控制订阅持续时间或连接保活。
订阅持续到调用方取消、服务端结束或连接失败。

`OpenEventClient` 为订阅启用 gRPC 保活；`AdminClient` 使用 gRPC 默认保活设置。
连接失败时，订阅结束并触发已注册的完成回调；行为见
[Subscribe API](API_cn.md#37-subscribe)。

使用结束后调用 `client.close()`，或用下面的 `with` 写法自动关闭。`close()` 可重复调用，会关闭连接并取消未完成的 RPC。

```python
with OpenEventClient("127.0.0.1:9527") as scoped_client:
    status = scoped_client.get_status(principal, token)
```

### 通过 TLS 代理使用生成的客户端

自己创建 gRPC 连接时，显式把单条消息的发送和接收上限都设为 64 MiB。默认接收上限是 4 MiB，
一个 4 MiB 对象加上 protobuf 字段就超过了它。下面用已取得的对象凭据 `key` 读取整个对象。
将示例地址替换为部署中 TLS 代理的地址。

```python
import grpc
from openevent.sdk.proto import openevent_pb2, openevent_pb2_grpc

max_message_bytes = 64 * 1024 * 1024
options = (
    ("grpc.max_send_message_length", max_message_bytes),
    ("grpc.max_receive_message_length", max_message_bytes),
)
with grpc.secure_channel(
    "events.example.com:443", grpc.ssl_channel_credentials(), options=options
) as channel:
    objects = openevent_pb2_grpc.ObjectStorageServiceStub(channel)
    response = objects.ReadObject(
        openevent_pb2.ReadObjectRequest(
            object_id=key.object_id,
            object_token=key.object_token,
            offset=0,
            nbytes=4 * 1024 * 1024,
        ),
        timeout=30,
    )
```

在自建连接上调用 `Subscribe` 时，还要配置与服务端匹配的保活参数。详见[订阅契约](API_cn.md#37-subscribe)。

### RPC 返回值

| 方法 | 返回值 |
| --- | --- |
| `get_uuid()` | Python `int`，表示一个已分配的 UUID |
| `get_seq_by_uuid(uuid)` | Python `int`，表示该 UUID 对应的已提交消息 seq |
| `subscribe(...)` | gRPC 订阅流的迭代器，每次返回一个 `SubscribeResponse` |
| 其余业务和管理 RPC 封装方法 | 原始 protobuf 响应对象，响应为空时也会返回对应类型的对象 |

SDK 原样返回消息，不解析或过滤系统消息。读取普通频道时也会收到系统消息：调用方先按
`message.principal == 0` 识别并按[系统协议](SYSTEM_PROTOCOL_cn.md)处理，再按频道的业务协议
解析其他消息。

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

`visibility` 和成员权限见 [频道读写权限](API_cn.md#25-频道读写权限)；创建频道时写入的初始化消息、
成员变更消息及保留系统频道见[系统协议](SYSTEM_PROTOCOL_cn.md)。普通频道的 `protocol`
仍表示其业务消息协议。

## 发布消息

每条新消息先用 `get_uuid()` 领取一个 UUID，发布时传入。需要重试时，由调用方使用同一个 UUID 再次发布。

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

如果只想在服务端的下一个序号仍符合预期时发布，先查询当前序号，再用 `publish` 指定下一条序号：

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

`payload` 的 Python 类型是 `bytes`。如果没有收到发布响应，用原来的 `uuid` 重试同一条消息。
重试返回 `ALREADY_EXISTS` 时，调用 `client.get_seq_by_uuid(uuid)` 取得已提交消息的序号。
详见[发布结果不确定时的处理](API_cn.md#29-发布结果不确定时的处理)。

## 写入和引用对象

先用 `write_object` 上传对象。发布消息时，可以把返回的对象编号和 token 组成一个 ObjectKey，放进 `object_keys`。

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

uuid = client.get_uuid()
published = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=b"report ready",
    object_keys=[key],
    uuid=uuid,
)
```

用 ObjectKey 可以一次读完整个对象。对象最大为 4 MiB，从头请求 4 MiB 会返回对象的实际字节。

```python
data = client.read_object(
    object_id=key.object_id,
    object_token=key.object_token,
    offset=0,
    nbytes=4 * 1024 * 1024,
).data
```

需要名称、类型、描述或单独查询大小时，再调用 `get_object_metadata`。对象限制、引用授权和错误规则见
[对象 API](API_cn.md#4-objectstorageservice) 和 [ObjectKey 规则](API_cn.md#27-对象读取凭据与消息引用)。

`batch_get_object_metadata(object_keys: Iterable[ObjectKey])` 可一次查询已有的对象引用列表。
例如，`message` 是一条已经读取且包含对象引用的消息：

```python
keys = message.object_keys
batch = client.batch_get_object_metadata(keys)
for key, metadata in zip(keys, batch.metadata):
    print(key.object_id, metadata.name, metadata.nbytes)
```

方法只发起一次 RPC，沿用 `timeout_ms`，返回原始 `BatchGetObjectMetadataResponse`。
`metadata` 的元素类型为 `ObjectMetadata`，按位置与 `keys` 对应，重复项也保留；
任一项失败时整批抛出 `grpc.RpcError`，不返回部分结果。
SDK 不自动拆批、去重、缓存或重试；数量、参数和错误规则见 [批量查询对象元数据](API_cn.md#44-batchgetobjectmetadata)。

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

上例只读取指定频道，包括其中的系统消息和业务消息，不读取频道 0。
继续读取时，把响应的 `next_seq` 作为新的 `from_seq`；
当 `next_seq > last_seq` 时，此次查询能看到的消息已经读完。完整过滤、分页和错误规则见
[Fetch API](API_cn.md#36-fetch)。

## 订阅消息

`subscribe` 返回一个可逐条读取消息的 gRPC 订阅流。调用 `cancel()` 可以取消订阅，调用 `wait_started(timeout_ms)` 可以等待服务端确认。迭代器按下文规则检查消息序号。

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

订阅迭代器会保留创建它的客户端。即使函数只返回迭代器，调用方仍能继续收消息。
如果调用 `client.close()` 或退出客户端的 `with` 块，该客户端上所有仍在运行的订阅都会取消。

`wait_started(timeout_ms)` 等待 gRPC 初始响应元数据中出现 [Subscribe API](API_cn.md#37-subscribe) 规定的接受标记，
成功返回 `None`，表示服务端曾接受该订阅。只要初始响应元数据中有这个标记，即使第一次调用时订阅已结束，或稍后重复调用，也都会成功。要查看订阅建立后发生的错误，可以继续迭代或调用 `code()`。

没有接受标记时，SDK 等待最终 gRPC 状态：非 `OK` 时原样抛出该调用的 `grpc.RpcError`；`OK` 时抛出
`RuntimeError`，表示订阅未经确认就已结束。参数取值范围与客户端的 `timeout_ms` 相同，只限制这次等待确认的时间。等待超时会取消订阅并抛出 `TimeoutError`。订阅本身没有截止时间。

订阅到达尾部后继续等待新消息。起点、权限变化和断线恢复见 [Subscribe API](API_cn.md#37-subscribe)。

`OpenEventClient.subscribe` 要求同一次订阅收到的消息按 `seq` 严格递增。第一条可以是 0，过滤消息也可以造成跳号。如果收到的序号等于或小于上一条，SDK 会取消订阅并抛出 `RuntimeError`，错误信息中会列出这两个序号。
每次订阅独立检查序号。调用方保存最后处理的 seq，并按 API 恢复规则重新订阅。

## 通过消息发现 Channel

要从头持续发现频道，调用
`client.subscribe(principal, token, from_seq=0, channels=[], only_my_recipient=False)`。
在同一个循环里处理系统消息即可。订阅先推送可见的历史消息，到达尾部后继续等待新消息。

如果应用需要知道启动时已有的消息何时处理完，可以先用 `fetch` 分页读取到第一页的 `last_seq`，
再从该序号加一的位置订阅。

应用自行维护频道列表和恢复进度。系统消息的内容见[系统协议](SYSTEM_PROTOCOL_cn.md)，
如何从最后一页接着订阅，以及读取权限如何生效，见[通过消息发现频道](API_cn.md#38-通过消息发现频道)。

## 定向消息

发布时可以传入 `recipients`。下面使用创建频道示例中已经加入的成员 1002 和 1003：

```python
uuid = client.get_uuid()
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"meeting reminder",
    uuid=uuid,
)
```

谁能读这条提醒由频道的读取权限决定。示例中的频道是公开的，所有已认证用户都能读到它。
开启 `only_my_recipient=True` 筛选后，只有 1002 和 1003 的结果里会出现这条提醒。
详见 [频道读写权限](API_cn.md#25-频道读写权限) 和 [Recipients](API_cn.md#26-recipients)。

## 管理 token

`AdminClient` 访问只运行在独立管理端口的 `AdminService`。

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

token = admin.add_token(principal=1001).token

admin.delete_token(principal=1001, token=token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

上例为身份编号 1001 取得 token，再删除该 `(principal, token)` 组合，最后读取一页管理消息。
消息下一页使用 `next_seq` 继续。凭据组合、参数和删除规则见
[AdminService API](API_cn.md#6-adminservice)；部署时必须保护管理端口。

## 错误处理

gRPC 调用失败时，SDK 会把原始错误交给调用方。可以捕获 `grpc.RpcError`，按状态码处理：

```python
import grpc

uuid = client.get_uuid()
try:
    client.publish_auto_seq(principal, token, channel_id, b"hello", uuid=uuid)
except grpc.RpcError as exc:
    if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
        print("invalid token")
    elif exc.code() == grpc.StatusCode.PERMISSION_DENIED:
        print("no permission")
    else:
        raise
```

常见状态码及其含义见 [API 契约](API_cn.md)。

## 本地开发

生成 proto、构建、安装、测试和清理的命令统一见 [README 的构建和测试](../README_cn.md#构建和测试)。
