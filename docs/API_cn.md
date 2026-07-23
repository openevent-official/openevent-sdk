# OpenEvent API 契约（gRPC）

[English version](API.md)

> 版本: 0.4.1
> 日期: 2026-07-17

本文档描述业务协议 [`proto/openevent.proto`](../proto/openevent.proto) 和管理协议
[`proto/admin.proto`](../proto/admin.proto) 的公开 API 行为。它不描述服务端存储、恢复、部署
拓扑或内部执行机制。

## 1. 基础约定

- 所有接口通过 `gRPC + Protocol Buffers` 提供。
- 除 `AdminService` 外，业务请求都携带 `principal + token`。
- 客户端只依赖 `seq` 作为全局消息位置，不维护独立消息存储 offset。
- `seq` 单调递增且已提交值不复用；客户端不得依赖序号相邻。
- 每条 `EventMessage` 包含 `ts_ms`，表示服务端收到发布请求时的 Unix 毫秒时间戳。
- `payload` 是不透明 bytes，OpenEvent 不解析或校验业务 schema。
- OpenEvent 保留全部已提交历史消息和 Channel metadata。

### 1.1 服务划分

| Service | 职责 |
|---------|------|
| `EventService` | 状态查询、消息发布、批量拉取、流式订阅 |
| `ChannelService` | Channel 创建、查询、列表、成员管理 |
| `AdminService` | token 管理和全部消息的管理查询 |

## 2. 通用语义

### 2.1 gRPC 状态码

| 状态码 | 语义 |
|--------|------|
| `UNAUTHENTICATED` | token 缺失、无效，或与 `principal` 不匹配 |
| `PERMISSION_DENIED` | ACL 检查失败，或调用方无权执行操作 |
| `NOT_FOUND` | Channel 不存在 |
| `ALREADY_EXISTS` | 重复添加成员等资源已存在场景 |
| `INVALID_ARGUMENT` | 字段缺失、值非法，或 recipients 不满足成员约束 |
| `RESOURCE_EXHAUSTED` | payload 超过部署上限 |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | 调用被取消；发布请求的提交结果可能不确定 |
| `DEADLINE_EXCEEDED` | 调用超过 deadline；发布请求的提交结果可能不确定 |
| `UNAVAILABLE` | 服务或依赖当前不可用 |
| `INTERNAL` | 未预期错误 |

### 2.2 消息序列

- `EventMessage.seq` 是全局单调递增消息序号，0 为保留值。
- 已提交 seq 不复用。
- 公开契约不要求 seq 相邻；读取方必须允许跳号并按服务端游标继续扫描。
- `GetStatus`、`Fetch` 和 `Subscribe` 都使用同一消息序列。

### 2.3 Payload

- `Publish` 与 `PublishAutoSeq` 都接受 `payload`，且 payload 可以为空。
- 大小上限由部署实例决定；参考实现默认 16 MiB。
- 超限返回 `RESOURCE_EXHAUSTED`，不产生新消息。

### 2.4 Channel ACL

| 可见性 | 读取权限 | 写入权限 |
|--------|----------|----------|
| `VISIBILITY_PUBLIC` | 所有已认证调用方 | 所有已认证调用方 |
| `VISIBILITY_PROTECTED` | 所有已认证调用方 | Channel 成员 |
| `VISIBILITY_PRIVATE` | Channel 成员 | Channel 成员 |

系统频道 `channel_id=0`：

- 所有已认证调用方可查询。
- 出现在 `CHANNEL_FILTER_ALL`，不出现在 `CHANNEL_FILTER_JOINED/OWNED`。
- 返回固定 `ChannelInfo`：`channel_id=0`、`VISIBILITY_PROTECTED`、无 creator、空 members。
- 不允许发布消息或修改成员。

### 2.5 Recipients

- `recipients` 是消息的定向接收 principal 列表；空列表表示无定向接收方。
- 发布时每个 recipient 都必须是对应 Channel 成员，否则返回 `INVALID_ARGUMENT`。
- `only_my_recipient=true` 时只返回或推送 recipients 包含当前 principal 的消息；空 recipients
  不匹配该过滤条件。

### 2.6 读写线性化与提交顺序

- 所有修改和非流式有状态业务/管理读 RPC 具有一个服务端全局顺序。写请求在该顺序中提交，
  非流式读请求在自己的顺序位置取得一致 snapshot。Subscribe 不属于该全局顺序。
- 发布时 token 和 Channel 写权限按该发布提交前最新的 committed 状态判断；权限校验与消息提交
  之间不会插入另一个 token 或 Channel 修改。
- RemoveMember 或 DeleteToken 先成功时，排在其后的发布分别返回 `PERMISSION_DENIED` 或
  `UNAUTHENTICATED`；发布先成功时消息保持已提交。
- 写请求只在服务端完成同步持久化后返回成功。成功响应表示修改已经提交并满足服务端持久性承诺。
- 如果调用方在写 RPC 成功返回后才发起非流式读 RPC，该读必须观察到该写的完整结果。相互重叠
  的修改和非流式读取按服务端全局顺序观察提交前或提交后的完整状态，不能观察部分提交。
- Subscribe 不提供上述 read-after-write 或与其他 RPC 的串行化保证；需要确定水位和写入可见性时
  使用 GetStatus 和 Fetch。

### 2.7 发布结果不确定时的处理

如果 `Publish` 或 `PublishAutoSeq` 没有返回成功响应，不能仅凭客户端看到的错误判断消息一定没有
提交。连接中断、客户端取消、`DEADLINE_EXCEEDED`、部分 `UNAVAILABLE` 等失败可能发生在服务端
已经开始或完成持久化之后。

对于兼容本契约的 OpenEvent 服务端，发布 RPC 的终态按下表判断。“保证未提交”表示本请求没有创建
`EventMessage`，也没有因本请求推进 `max_seq`；它只针对返回该状态的这一次 RPC，不证明此前同一
业务事件的其他发布尝试没有提交，也不表示错误原因一定可恢复。

| 返回 | 提交保证 |
|------|----------|
| `OK` | 已同步持久化并提交 |
| `UNAUTHENTICATED` | 保证未提交 |
| `PERMISSION_DENIED` | 保证未提交 |
| `NOT_FOUND` | 保证未提交 |
| `INVALID_ARGUMENT` | 保证未提交 |
| `RESOURCE_EXHAUSTED` | 保证未提交 |
| `ABORTED` | 保证未提交；仅适用于 `Publish` 的 seq CAS 冲突，`PublishAutoSeq` 不应返回 |
| 其他任何非 `OK` 返回或没有 gRPC status | 结果不确定 |

因此 `CANCELLED`、`DEADLINE_EXCEEDED`、`UNKNOWN`、`UNAVAILABLE`、`INTERNAL`、连接中断、代理重置
等都必须按结果不确定处理。即使某次 `UNAVAILABLE` 看起来发生在连接建立前，调用方也不能依赖错误
文本推断未提交。只有表中明确标为“保证未提交”的返回，才允许直接进入错误修复和重试判断；保证
未提交但不可恢复的参数、权限或 payload 错误仍不得盲目重试。

上表只适用于由兼容 OpenEvent 服务端产生的终态。如果代理、负载均衡器或客户端本地可能在请求已
转发后合成同名 status，且部署无法证明该 status 发生在转发前，仍必须按结果不确定处理。

调用方不得直接重试结果不确定的发布，而应：

1. 发布前记录当前 `max_seq`，并保留本次请求的 `channel_id`、principal、recipients 和 payload；
   建议在 payload 的业务协议中携带唯一事件 ID。
2. 失败后先调用 GetStatus，记录用于对账的 `reconcile_max_seq`。由于该读排在先前发布之后，若发布
   已提交，该水位必须包含它。
3. 从发布前记录的 `max_seq+1` 开始 Fetch，持续使用 `next_seq` 扫描到超过
   `reconcile_max_seq`。Subscribe 可以用于尽快发现匹配消息，但由于过滤掉的 seq 不产生进度响应，
   不能单独用于证明消息不存在。
4. `Publish` 可优先检查请求中指定的 seq；`PublishAutoSeq` 在上述区间内按唯一事件 ID 或完整请求
   内容检查是否已经写入。
5. 找到匹配消息时按成功处理并使用其 seq；确认未找到后，调用方才可以根据业务策略发起新的
   发布请求。

如果应用可能并发发布内容完全相同的消息，单靠 payload 等字段不能可靠区分调用，必须在业务
payload 中加入唯一事件 ID。

对账读取必须使用仍然有效且有权读取目标 Channel 的身份，并确保 channels/recipient 过滤不会
排除候选消息。如果 token 已失效或 Channel 读取权限已被撤销，业务 Fetch 无法证明发布结果，
需要通过仍有权限的身份或受保护的管理查询完成对账。

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- `max_seq`：最大已提交消息 seq；无消息时为 0。
- `min_seq`：全量保留语义下，有消息时为 1，否则为 0。

客户端可以把 `max_seq+1` 作为下一次 Publish 的候选 seq。并发发布先占用该值时，Publish 返回
`ABORTED`。

### 3.2 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

客户端指定 seq 做 CAS 发布。成功条件：

- token 与 principal 匹配。
- payload 未超过上限。
- `seq == 当前 max_seq + 1`。
- Channel 存在且调用方有写权限。
- recipients 满足 2.5。

| 条件 | 返回 |
|------|------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Channel 不存在 | `NOT_FOUND` |
| 无写权限 | `PERMISSION_DENIED` |
| recipient 非成员 | `INVALID_ARGUMENT` |

成功返回空响应。服务端生成 `ts_ms`，消息随后可被有权限的调用方读取或订阅。

### 3.3 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

成功条件与 Publish 相同，但 seq 由服务端分配。响应中的 seq 是已提交消息序号。

### 3.4 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

请求规则：

- token 与 principal 匹配。
- limit 必须在 `1..1000`。
- channels 为空表示不过滤；非空时只返回这些 Channel 中调用方可读的消息。
- `only_my_recipient=true` 时应用 recipient 过滤。

| `from_seq` | 行为 |
|------------|------|
| `0` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `> max_seq` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `1..max_seq` | 从该 seq 开始扫描可见消息 |

响应语义：

- messages 最多包含 limit 条经过 ACL、channels 和 recipient 过滤的消息。
- 服务端可以因扫描预算或响应大小预算提前结束一页，因此 messages 可能少于 limit，甚至在大量
  不可见消息时为空；调用方必须使用返回的 `next_seq` 继续，不能把短页当作尾部。
- 单条可返回消息可以突破页面软大小预算，但仍受 gRPC 传输硬上限约束；服务端不会因软预算在同一
  游标上反复返回空页。
- `next_seq` 是下一次继续扫描的建议起点。
- `last_seq` 是本次响应观察到的最大已提交消息序号。
- `next_seq <= last_seq` 时可以继续 Fetch；`next_seq > last_seq` 表示到达本次观察到的尾部。
- ACL 或过滤不会让 next_seq 回退。

### 3.5 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

按全局 seq 顺序推送当前调用方可见的消息。

- token 与 principal 匹配。
- Subscribe 不进入非流式读写的全局线性化顺序，不提供相对于其他 RPC 的 read-after-write 保证。
- 不接受 Channel 过滤；订阅范围始终是全局消息流。
- `only_my_recipient=true` 时应用 recipient 过滤。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从当前 `max_seq+1` 开始等待新消息 |
| `> max_seq` | 返回一个 `next_seq=max_seq+1` 响应后结束 stream |
| `1..max_seq` | 从该 seq 开始推送可见消息 |

`SubscribeResponse.message` 表示正常消息；`next_seq` 只用于 `from_seq > max_seq`。客户端取消或
断开后应记录最后已处理 seq，并从 `seq+1` 重新订阅或 Fetch。

## 4. ChannelService

### 4.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- visibility 必须是 public、protected 或 private；未设置时默认为 public。
- 创建者自动加入 members。
- 请求 members 去重，创建者只保留一份。
- 成功响应返回创建后的 ChannelInfo。

### 4.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

- 读取权限见 2.4。
- Channel 不存在返回 `NOT_FOUND`。
- 无读取权限返回 `PERMISSION_DENIED`。

### 4.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

- 当前不支持分页。
- 先按读取权限过滤，再应用 filter。
- `CHANNEL_FILTER_ALL` 返回全部可见 Channel。
- `CHANNEL_FILTER_JOINED` 返回调用方已加入的可见 Channel。
- `CHANNEL_FILTER_OWNED` 返回调用方创建的可见 Channel。
- 非法 filter 返回 `INVALID_ARGUMENT`。

### 4.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 目标已是成员时返回 `ALREADY_EXISTS`。

### 4.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 不允许移除创建者本人。
- 移除非成员目标是幂等成功。

## 5. AdminService

AdminService 定义在 [`proto/admin.proto`](../proto/admin.proto)，只运行在独立管理端口，不携带
业务 principal/token。部署必须通过网络隔离或外部授权保护管理端口。

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListTokens(ListTokensRequest) returns (ListTokensResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 5.1 ListTokens

- page_token 是服务端生成的不透明 exclusive cursor；空字符串表示第一页。
- limit 必填且范围为 `1..1000`。
- binding 按持久化 token key 的无符号字节序升序返回。
- next_page_token 为空表示遍历结束。
- 非法或不支持的 page_token 返回 `INVALID_ARGUMENT`。
- 每页内部是一致视图；token 可增删，因此跨页不是强快照。
- binding 和 page token 都是敏感管理数据，不得写入日志、trace 或错误详情。

### 5.2 ListMessages

```protobuf
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

- 不应用 Channel ACL 或 recipient 过滤。
- from_seq 是包含起点；0 表示从 1 开始。
- limit 必填且范围为 `1..1000`。
- 消息按 seq 升序返回；服务端可以因响应大小预算在达到 limit 前结束一页。
- 单条消息可以突破页面软大小预算，但仍受 gRPC 传输硬上限约束；因大小预算延后的消息不会被游标
  跳过。
- next_seq 是下一页建议起点。
- last_seq 是查询开始时的最大已提交消息序号。
- 起点超过尾部时返回空页和尾部下一位置；`next_seq > last_seq` 表示到达本页观察到的尾部。
- 每页是一致视图，但并发追加可使后续页的 last_seq 增大。

## 6. 兼容性建议

- 两份 proto 共同组成公开协议契约。
- 调用方按 gRPC status code 分支，不依赖错误文本。
- 消费者必须允许 seq 跳号，记录最后已处理 seq，并从 `seq+1` 断点续读。
- payload 的业务 schema 放在 Channel protocol 对应的业务协议中。
