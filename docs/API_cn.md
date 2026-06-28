# OpenEvent API 契约（gRPC）

[English version](API.md)

> 版本: 0.3.0  
> 日期: 2026-04-30

本文档描述 [`proto/openevent.proto`](../proto/openevent.proto) 暴露给各项目共同依赖的 API 行为。它面向客户端、服务端实现、同步组件、测试工具和语言 SDK；不描述具体服务端的存储、恢复、部署拓扑或内部执行机制。

---

## 1. 基础约定

- 所有接口通过 `gRPC + Protocol Buffers` 提供。
- 除 `AdminService` 外，业务请求都携带 `principal + token`。
- 客户端只依赖 `seq` 作为消息位置；任何内部 offset 不属于公开协议。
- 每条已写入日志记录以 `EventMessage` 返回，其中包含 `ts_ms`，表示服务端收到写入请求时的 Unix 毫秒时间戳。
- `payload` 是不透明字节数组，OpenEvent 不解析内容，也不做 schema 校验。

- OpenEvent 保留全部已提交历史消息，Channel 创建后持续保留

- Channel 创建后持续存在；已发布消息按全局 `seq` 读取。

### 1.1 服务划分

| Service | 职责 |
|---------|------|
| `EventService` | 状态查询、消息发布、批量拉取、流式订阅 |
| `ChannelService` | Channel 创建、查询、列表、成员管理 |
| `AdminService` | token 的增删与列表查询 |

---

## 2. 通用语义

### 2.1 gRPC 状态码

调用方应按 gRPC Status Code 处理错误：

| 状态码 | 语义 |
|--------|------|
| `UNAUTHENTICATED` | token 缺失、无效，或与 `principal` 不匹配 |
| `PERMISSION_DENIED` | ACL 检查失败，或调用方无权执行该操作 |
| `NOT_FOUND` | Channel 不存在 |
| `ALREADY_EXISTS` | 重复添加成员等资源已存在场景 |
| `INVALID_ARGUMENT` | 字段缺失、值非法，或 `recipients` 不满足成员约束 |
| `RESOURCE_EXHAUSTED` | `payload` 超过部署实例允许的上限 |
| `ABORTED` | `Publish` 的 `req.seq != max_seq + 1` |
| `UNAVAILABLE` | 服务当前不可用或依赖不可用 |
| `INTERNAL` | 未预期错误 |

### 2.2 Payload

- `Publish` 与 `PublishAutoSeq` 请求都包含 `payload`。
- `payload` 可为空。
- `payload` 大小上限由具体部署实例决定；OpenEvent 参考实现默认上限为 16 MiB。
- 超过上限时返回 `RESOURCE_EXHAUSTED`，且不会产生新消息。

### 2.3 Channel ACL

| 可见性 | 读取权限 | 写入权限 |
|--------|----------|----------|
| `VISIBILITY_PUBLIC` | 所有已认证调用方 | 所有已认证调用方 |
| `VISIBILITY_PROTECTED` | 所有已认证调用方 | Channel 成员 |
| `VISIBILITY_PRIVATE` | Channel 成员 | Channel 成员 |

系统频道 `channel_id=0`：

- 所有已认证调用方可查询。
- 会出现在 `CHANNEL_FILTER_ALL` 列表结果中。
- 不会出现在 `CHANNEL_FILTER_JOINED` 或 `CHANNEL_FILTER_OWNED` 结果中。
- 返回的 `ChannelInfo` 固定为：`channel_id=0`、`visibility=VISIBILITY_PROTECTED`、`creator` 不设置、`members=[]`。
- 不允许发布消息，也不允许修改成员。

### 2.4 Recipients

- `recipients` 是消息的定向接收方 principal 列表。
- `recipients=[]` 表示无定向接收方。
- 发布消息时，`recipients` 中每个 principal 都必须是对应 Channel 的成员。
- 任一 recipient 不在 Channel 成员列表中时，发布请求返回 `INVALID_ARGUMENT`，且不会产生新消息。
- 读取或订阅时，如果 `only_my_recipient=true`，只返回或推送 `recipients` 包含当前 `principal` 的消息；`recipients=[]` 的消息不会匹配该过滤条件。

---

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

返回当前全局消息范围：

- `max_seq`：当前最大已发布消息 seq；无消息时为 `0`。
- `min_seq`：当前最小可用 seq；全量保留语义下，有消息时为 `1`，无消息时为 `0`。

客户端可用 `max_seq + 1` 计算下一次 `Publish` 的期望 seq。

### 3.2 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

客户端指定 `seq` 做 CAS 发布。

成功条件：

- token 与 `principal` 匹配。
- `payload` 未超过上限。
- `seq == 当前 max_seq + 1`。
- Channel 存在且允许当前 `principal` 写入。
- `recipients` 满足 2.4 的成员约束。

错误语义：

| 条件 | 返回 |
|------|------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Channel 不存在 | `NOT_FOUND` |
| 无写权限 | `PERMISSION_DENIED` |
| recipient 非 Channel 成员 | `INVALID_ARGUMENT` |

成功返回空响应；消息随后可被有权限的调用方读取或订阅。
服务端会在消息中写入 `ts_ms`。

### 3.3 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

由服务端分配本次发布的全局 seq。

成功条件与 `Publish` 相同，但调用方不提供 `seq`。成功响应中的 `seq` 是已发布消息的全局 seq。
服务端会在消息中写入 `ts_ms`。

### 3.4 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

按全局 `seq` 批量拉取当前调用方可见的消息。

请求规则：

- token 与 `principal` 匹配。
- `limit` 必须在 `1..1000`，否则返回 `INVALID_ARGUMENT`。
- `channels` 是可选 channel id 过滤条件。为空时扫描调用方可见的所有 channel；非空时只返回这些
  channel 中调用方有读取权限的消息。
- `only_my_recipient=true` 时只返回 `recipients` 包含当前 `principal` 的消息。

起点语义：

| `from_seq` | 行为 |
|------------|------|
| `0` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `> max_seq` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `1..max_seq` | 从该 seq 开始扫描并返回可见消息 |

响应语义：

- `messages` 最多包含 `limit` 条经过 ACL、`channels` 和 recipient 过滤后的可见 `EventMessage`。
- 每条 `messages` 都包含写入时生成的 `ts_ms`。
- `next_seq` 表示下一次继续读取的建议起点。
- `last_seq` 表示本次 Fetch 响应观察到的最高已提交全局 seq。
- 如果 `next_seq <= last_seq`，调用方可以继续从 `next_seq` 扫描；如果 `next_seq > last_seq`，
  表示本次 Fetch 已到达当前已提交尾部。
- ACL、channel 或 recipient 过滤不会让 `next_seq` 回退。

### 3.5 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

订阅全局消息流中当前调用方可见的消息。

请求规则：

- token 与 `principal` 匹配。
- 不接受 `channel_id`；订阅范围始终是全局流。
- `only_my_recipient=true` 时只推送 `recipients` 包含当前 `principal` 的消息。

起点语义：

| `from_seq` | 行为 |
|------------|------|
| `0` | 从当前 `max_seq+1` 开始等待新消息 |
| `> max_seq` | 返回一个仅包含 `next_seq=max_seq+1` 的响应后结束 stream |
| `1..max_seq` | 从该 seq 开始推送可见消息 |

`SubscribeResponse` 使用 `oneof result`：

- `message`：正常推送的可见 `EventMessage`。
- `next_seq`：仅用于 `from_seq > max_seq` 场景，提示调用方下一次可使用的起点。

每条订阅推送的 `message` 都包含写入时生成的 `ts_ms`。

客户端取消或断开后，本次 stream 结束；如需继续消费，应使用已记录的 seq 重新发起订阅。

---

## 4. ChannelService

### 4.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

创建 Channel。

- `visibility` 必须是 `VISIBILITY_PUBLIC`、`VISIBILITY_PROTECTED` 或 `VISIBILITY_PRIVATE`。
- proto3 默认值下，未设置 `visibility` 等同于 `VISIBILITY_PUBLIC`。
- 创建者会自动加入成员列表。
- 请求中的 `members` 会去重；创建者只保留一份。
- 成功响应返回创建后的 `ChannelInfo`。

### 4.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

按 `channel_id` 查询 Channel。

- 读取权限见 2.3。
- Channel 不存在返回 `NOT_FOUND`。
- 无读取权限返回 `PERMISSION_DENIED`。

### 4.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

列出当前调用方可见的 Channel。

- 不支持分页。
- 先按读取权限过滤可见 Channel。
- 再按 `filter` 过滤：
  - `CHANNEL_FILTER_ALL`：全部可见 Channel。
  - `CHANNEL_FILTER_JOINED`：调用方已加入的可见 Channel。
  - `CHANNEL_FILTER_OWNED`：调用方创建的可见 Channel。
- proto3 默认值下，未设置 `filter` 等同于 `CHANNEL_FILTER_ALL`。
- `filter` 非法时返回 `INVALID_ARGUMENT`。

### 4.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

添加 Channel 成员。

- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 目标 principal 已经是成员时返回 `ALREADY_EXISTS`。

### 4.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

移除 Channel 成员。

- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 不允许移除创建者本人，返回 `PERMISSION_DENIED`。
- 移除非成员目标是幂等成功。

---

## 5. AdminService

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListTokens(ListTokensRequest) returns (ListTokensResponse);
```

`AdminService` 管理业务 token。请求消息不包含调用者 `principal/token`；部署环境应决定谁可以调用这些管理接口。

接口语义：

- `AddToken`：为 `target_principal` 创建 token，并返回 `TokenBinding`。
- `DeleteToken`：删除 `target_token`；删除后该 token 不能再用于业务请求。
- `ListTokens`：返回当前全部 token 绑定。

---

## 6. 兼容性建议

- 新项目应优先依赖 [`proto/openevent.proto`](../proto/openevent.proto) 和本文档描述的行为，不依赖任何具体实现内部细节。
- 调用方应按 gRPC Status Code 分支处理错误，不依赖错误消息文本。
- 调用方应保存已成功处理的最大 seq，用于 Fetch 或 Subscribe 断点续读。
