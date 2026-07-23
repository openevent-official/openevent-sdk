# OpenEvent SDK 使用指南

[English version](USAGE.md)

本文面向社区用户，说明如何在 Python 项目中安装和使用 `openevent-sdk`。
协议字段和边界语义以 [API 契约](API_cn.md) 为准。

## 安装

从本仓库构建 wheel：

```bash
make build
python3 -m pip install dist/openevent_sdk-0.4.1-py3-none-any.whl
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

`OpenEventClient` 和 `AdminClient` 的构造函数都接受可选 `timeout`，默认值为 30 秒。该值作用于
所有非流式 RPC；构造时传入 `None` 可关闭这些 deadline。`subscribe` 是长期流，不使用客户端
timeout。

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

SDK 自建的 gRPC channel 默认把单条收发消息上限设为 64 MiB，以容纳默认 16 MiB payload 和分页
响应。调用方注入自定义 channel 时必须自行设置足够的 `grpc.max_send_message_length` 和
`grpc.max_receive_message_length`。

### 发布失败后的对账

`Publish` 或 `PublishAutoSeq` 遇到连接中断、取消、`DEADLINE_EXCEEDED` 或部分
`UNAVAILABLE` 时，服务端可能已经完成写入。不要直接重试，否则 `PublishAutoSeq` 可能产生重复
消息，`Publish` 也可能因原 seq 已提交而返回 `ABORTED`。

只有 `UNAUTHENTICATED`、`PERMISSION_DENIED`、`NOT_FOUND`、`INVALID_ARGUMENT`、
`RESOURCE_EXHAUSTED`，以及仅适用于 `Publish` seq CAS 冲突的 `ABORTED`，保证本次发布没有提交。
其他任何非 `OK` 返回或没有 gRPC status 都按结果不确定处理。保证未提交不等于可恢复；详细契约见
[API_cn.md](API_cn.md) 的 2.7 节。

推荐流程：

1. 发布前记录 `get_status(...).max_seq`，并在 payload 的业务协议中加入唯一事件 ID。
2. 结果不确定时先再次调用 GetStatus，记录 `reconcile_max_seq`；如果原发布已提交，该水位必须
   包含它。
3. 从此前的 `max_seq+1` 开始 Fetch，持续以 `next_seq` 翻页，直到扫描位置超过
   `reconcile_max_seq`。Subscribe 可以帮助尽快发现匹配消息，但因为被过滤的 seq 不会返回进度，
   不能单独证明消息不存在。
4. `Publish` 检查指定 seq；`PublishAutoSeq` 在扫描区间内查找同一事件 ID。
5. 找到时按成功处理并使用消息 seq；确认不存在后再决定是否重新发布。

服务端对修改和非流式有状态读提供全局线性化顺序：写 RPC 成功返回后才发起的 GetStatus、Fetch
或其他非流式读 RPC 必须看到该写的完整结果。Subscribe 不属于该顺序，不能用于替代 GetStatus
和 Fetch 的确定水位对账。

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

`subscribe` 返回兼容 gRPC stream 的迭代器；它保留 `cancel()` 等 gRPC call 方法，同时执行下述
SDK 序号检查：

```python
for item in client.subscribe(principal, token, from_seq=0):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` 表示从当前最新位置之后开始等待新消息。如果连接断开，客户端应记录已经处理到的
`seq`，之后从 `seq + 1` 重新订阅或拉取。

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
生成的 `openevent_pb2*.py` 和 `admin_pb2*.py` 不进 git，但会被构建进 wheel。

`make build` 使用 Hatchling 构建 wheel，构建依赖、测试依赖、缓存和临时文件放在 `build/`，
最终产物放在 `dist/`。
