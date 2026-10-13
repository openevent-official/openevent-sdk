# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段和边界语义以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建并安装：

```bash
make install
```

选择 Python 环境和安装参数的方法见 [README 的构建和测试说明](../README_cn.md#构建和测试)。

## 连接服务

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` 是 gRPC 服务地址。`OpenEventClient` 和 `AdminClient` 创建并管理明文 gRPC 连接，
单条收发消息上限为 64 MiB。需要 TLS 或自定义连接参数时，使用 SDK 生成的 gRPC stub。

两种 client 都接受 `timeout_ms`，默认 30000 毫秒（30 秒），用于非流式 RPC。
构造或重新赋值时须使用大于 0 的有限数值（不含 bool），非法值抛出 `ValueError`。
`timeout_ms` 控制普通 RPC 等待结果的时间，不控制订阅持续时间或连接保活。
订阅持续到调用方取消、服务端结束或连接失败。

`OpenEventClient` 为订阅启用 gRPC 保活；`AdminClient` 使用 gRPC 默认保活设置。
连接失败时，订阅结束并触发已注册的完成回调；行为见
[Subscribe API](API_cn.md#37-subscribe)。

使用结束后调用 `client.close()`，或使用 context manager。`close()` 可重复调用，会关闭连接并取消未完成的 RPC。

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

SDK 原样返回消息，不解析或过滤系统消息。读取普通 Channel 时也会收到系统消息：调用方先按
`message.principal == 0` 识别并按[系统协议](SYSTEM_PROTOCOL_cn.md)处理，再按 Channel 的业务协议
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

`visibility` 和成员权限见 [Channel ACL](API_cn.md#25-channel-acl)；创建 Channel 时写入的初始化消息、
成员变更消息及保留系统 Channel 见[系统协议](SYSTEM_PROTOCOL_cn.md)。普通 Channel 的 `protocol`
仍表示其业务消息协议。

## 发布消息

每次逻辑发布前调用 `get_uuid()` 领取一个 UUID，发布时显式传入；重试由调用方处理。

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

`payload` 使用 bytes。发布结果不确定时，保留原 UUID 并按
[恢复流程](API_cn.md#29-发布结果不确定时的处理)处理；使用 `client.get_seq_by_uuid(uuid)` 查询已提交消息的位置。

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

使用 ObjectKey 授权读取 metadata 和字节范围：

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

对象限制、引用授权和错误规则见
[对象 API](API_cn.md#4-objectstorageservice) 和 [ObjectKey 规则](API_cn.md#27-对象-capability-与消息引用)。

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

上例只读取指定 Channel，包括其中的系统消息和业务消息，不读取 Channel 0。
继续读取时，把响应的 `next_seq` 作为新的 `from_seq`；
以 `next_seq > last_seq` 判断是否到达本页观察到的尾部。完整过滤、分页和错误规则见
[Fetch API](API_cn.md#36-fetch)。

## 订阅消息

`subscribe` 返回兼容 gRPC stream 的迭代器；它提供 `cancel()` 和 `wait_started(timeout_ms)`，同时执行下述
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

`wait_started(timeout_ms)` 等待初始 metadata 中出现 [Subscribe API](API_cn.md#37-subscribe) 规定的接受标记，
成功返回 `None`，并记住该 stream 曾被接受。即使首次调用时 stream 已经结束，只要初始 metadata 包含
该标记，仍然成功；后续重复调用也成功。它不保证 stream 当前仍然打开，接受后的错误由迭代或 `code()` 获取。

没有接受标记时，SDK 等待最终 gRPC 状态：非 `OK` 时原样抛出该调用的 `grpc.RpcError`；`OK` 时抛出
`RuntimeError`，表示订阅未经确认就已结束。参数校验与 client 的 `timeout_ms` 相同，仅用于本次建立等待；
等待超时会取消 stream 并抛出 `TimeoutError`，不会给整个 stream 设置 deadline。

订阅到达尾部后继续等待新消息。起点、权限变化和断线恢复见 [Subscribe API](API_cn.md#37-subscribe)。

高层 `OpenEventClient.subscribe` 迭代器要求同一条 stream 内的消息 `seq` 严格递增；首条可以是 0，
过滤造成的序号间隔也合法。重复或回退表示 stream 违反协议，SDK 会取消该 stream 并抛出 `RuntimeError`，
异常信息包含前一个序号和本次序号。
每次订阅独立检查序号。调用方保存最后处理的 seq，并按 API 恢复规则重新订阅。

## 通过消息发现 Channel

应用可以从历史消息重建所需的 Channel 列表，再通过订阅发现新 Channel，无需定时调用 `ListChannels`。
启动时用 `get_status` 记录消息水位 `H`，从 `from_seq=0` 开始用 `fetch(channels=[], only_my_recipient=False)`
读取到该水位，处理系统事件并恢复应用状态；随后用
`subscribe(from_seq=H+1, channels=[], only_my_recipient=False)` 继续接收当前有权读取的消息。

系统消息的内容见[系统协议](SYSTEM_PROTOCOL_cn.md)；完整的读取、交接与权限规则见
[通过消息发现 Channel](API_cn.md#38-通过消息发现-channel)。SDK 不维护 Channel 列表或恢复进度。

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

token = admin.add_token(principal=1001).token

admin.delete_token(principal=1001, token=token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

上例为 principal 1001 取得 token，再删除该 `(principal, token)` 组合，最后读取一页管理消息。
消息下一页使用 `next_seq` 继续。凭据组合、参数和删除规则见
[AdminService API](API_cn.md#6-adminservice)；部署时必须保护管理端口。

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
