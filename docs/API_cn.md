# OpenEvent API 契约（gRPC）

[English version](API.md)

> 版本: 0.8.0
> 日期: 2026-09-08

本文档描述业务协议 [`proto/openevent.proto`](../proto/openevent.proto) 和管理协议
[`proto/admin.proto`](../proto/admin.proto) 的公开 API 行为。它不描述服务端存储、恢复、部署
拓扑或内部执行机制。

## 1. 基础约定

- 所有接口通过 `gRPC + Protocol Buffers` 提供。
- 客户端只依赖 `seq` 作为全局消息位置，不维护独立消息存储 offset。
- 有效初始化的服务端始终包含永久保留的系统初始化消息 `seq=0`；业务消息从 `seq=1` 开始。
- `seq` 单调递增且已提交值不复用；业务消息按服务端当前 `max_seq + 1` 递增。
- 每条业务 `EventMessage` 包含 `ts_ms`，表示服务端收到发布请求时的 Unix 毫秒时间戳；seq 0 的值是
  `system.v1` 定义的初始化事件时间。
- 每条业务 `EventMessage` 都包含由 `AllocateUuids` 分配的非 0 `uuid`。系统初始化消息是唯一例外，使用
  `uuid=0` 和 `principal=0`。
- 业务 `payload` 是不透明 bytes，OpenEvent 不解析或校验其业务 schema。服务端生成的 seq=0 系统 payload
  遵循独立的 `system.v1` 协议。
- 业务 principal 必须大于 0。`principal=0` 保留给服务端生成的系统行为；业务请求在 principal、token
  binding principal、Channel creator/member、recipient 或对象创建者位置使用 0 时返回 `INVALID_ARGUMENT`。
- OpenEvent 全量保留已提交消息、Channel metadata 和不可变对象；当前不提供消息或对象删除、保留期限、
  历史压缩或后台垃圾回收。这是当前版本明确选择的保留策略。

### 1.1 服务划分

| Service | 职责 |
|---------|------|
| `EventService` | 状态查询、消息发布、批量拉取、流式订阅 |
| `ObjectStorageService` | 不可变对象写入、metadata 查询和部分数据读取 |
| `ChannelService` | Channel 创建、查询、列表、成员管理 |
| `AdminService` | token 管理和全部消息的管理查询 |

## 2. 通用语义

### 2.1 gRPC 状态码

| 状态码 | 语义 |
|--------|------|
| `UNAUTHENTICATED` | token 缺失、无效，或与 `principal` 不匹配 |
| `PERMISSION_DENIED` | ACL 检查失败，或调用方无权执行操作 |
| `NOT_FOUND` | Channel 不存在，或对象 capability 不存在/无效 |
| `ALREADY_EXISTS` | 资源已存在，例如重复添加成员或 UUID 已被消息使用 |
| `INVALID_ARGUMENT` | 字段缺失、值非法，或 recipients 不满足成员约束 |
| `RESOURCE_EXHAUSTED` | payload 超过上限，或对象文件存储空间/quota/inode 不足 |
| `OUT_OF_RANGE` | ReadObject offset 大于对象大小，或 Subscribe 起点超过 `max_seq + 1` |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | 调用被取消；写入结果可能不确定 |
| `DEADLINE_EXCEEDED` | 调用超过 deadline；写入结果可能不确定 |
| `UNAVAILABLE` | 服务或依赖当前不可用 |
| `DATA_LOSS` | 持久化存储损坏；服务端会退出，而不是继续提供部分状态 |
| `INTERNAL` | 未预期错误 |

`RESOURCE_EXHAUSTED` 的对象存储范围只包括对象文件写入、同步和清理过程中的空间、quota 或 inode
耗尽；RocksDB 写入失败不属于该状态，按存储可用性故障返回 `UNAVAILABLE`，并触发服务端退出。

致命存储故障使服务端停止接收新调用并以非零状态退出。触发故障的 RPC 尽力返回原始错误：存储损坏为
`DATA_LOSS`，其他致命存储故障为 `UNAVAILABLE`；已经进入执行阶段的普通 RPC 可以完成，活动订阅会结束。

### 2.2 消息序列

- `EventMessage.seq` 是全局单调递增消息序号。
- `seq=0` 是永久保留的系统初始化消息，不是空序列哨兵。`min_seq=0` 且 `max_seq=0` 表示当前只有该消息。
- 已提交 seq 不复用。
- 业务消息 seq 按 `max_seq + 1` 递增；读取方按服务端返回的游标继续扫描。
- `GetStatus`、`Fetch` 和 `Subscribe` 都使用同一消息序列。

### 2.3 Payload

- `Publish` 与 `PublishAutoSeq` 都接受 `payload`，且 payload 可以为空。
- 写入大小上限由部署配置 `max_payload_bytes` 决定，只用于发布请求的 payload 校验。
- 超限返回 `RESOURCE_EXHAUSTED`，不产生新消息。
- 已提交消息的读取不受当前写入上限限制。下调配置并重启后，Fetch、Subscribe 和管理端 ListMessages
  仍须返回原有消息的完整 payload，不得因其超过新的写入上限而拒绝、跳过或截断。分页预算只决定
  返回的页边界。

### 2.4 消息 UUID

- UUID 0 保留给系统初始化消息。
- 每个 Publish 和 PublishAutoSeq 请求都必须携带一个由 AllocateUuids 返回的非 0 UUID。
- 初始化消息参与普通 UUID 查询，`GetSeqByUuid(0)` 返回 `seq=0`；客户端不能分配或发布
  UUID 0。业务发布使用未由服务端分配的 UUID 时返回 `INVALID_ARGUMENT`，已被 committed 消息消费时返回
  `ALREADY_EXISTS`。
- 认证成功后，已被消费的 UUID 必须先于 payload、ObjectKey、Channel、ACL 和 Publish seq-CAS 校验返回
  `ALREADY_EXISTS`。重复处理不比较任何这些请求字段；一个 UUID 最多消费一条消息。
- Python 高层 client 只通过 `get_uuid()` 逐个提供 UUID；其内部池行为见使用指南。生成式 gRPC stub 按协议提供
  AllocateUuids。
- 分配与查询 RPC 分别以 3.2 和 3.3 节为准。发布结果不确定时的完整恢复流程只以 2.9 节为准。

### 2.5 Channel ACL

| 可见性 | 读取权限 | 写入权限 |
|--------|----------|----------|
| `VISIBILITY_PUBLIC` | 所有已认证调用方 | 所有已认证调用方 |
| `VISIBILITY_PROTECTED` | 所有已认证调用方 | Channel 成员 |
| `VISIBILITY_PRIVATE` | Channel 成员 | Channel 成员 |

系统 Channel `channel_id=0` 对所有已认证调用方只读；固定字段、列表归属和初始化消息定义见
[`SYSTEM_PROTOCOL_cn.md`](SYSTEM_PROTOCOL_cn.md)。

### 2.6 Recipients

- `recipients` 是消息的定向接收 principal 列表；空列表表示无定向接收方。
- 发布时每个 recipient 都必须是对应 Channel 成员，否则返回 `INVALID_ARGUMENT`。
- `only_my_recipient=true` 时只返回或推送 recipients 包含当前 principal 的消息；空 recipients
  不匹配该过滤条件。

### 2.7 对象 Capability 与消息引用

- `ObjectKey` 由 `object_id + object_token` 组成，是永久 bearer capability。任何同时持有两者的
  调用方都能读取对象 metadata 和 data，不需要 principal token 或 Channel 权限。
- 对象 capability 可以转交，当前不能撤销、轮换、删除或在创建后收窄权限。必须把
  `object_token` 视为秘密。
- Publish 和 PublishAutoSeq 最多接受 1024 个 `object_keys`。服务端不去重，并按请求顺序保存。
- 每个引用都必须包含非 0 object ID 和非空 token。格式错误返回 `INVALID_ARGUMENT`；对象不存在、
  尚未提交或 token 不匹配返回 `NOT_FOUND`。
- 任一引用无效都会拒绝整条消息，`max_seq` 不推进。引用校验不读取对象 data。
- Fetch、Subscribe 和管理端 ListMessages 返回完整 ObjectKey；任何有权读取消息的调用方都会取得
  消息中的全部 capability。

### 2.8 读写线性化与提交顺序

- 所有修改和非流式有状态业务/管理读 RPC 具有一个服务端全局顺序。写请求在该顺序中提交，
  非流式读请求在自己的顺序位置取得一致 snapshot。Subscribe 不属于该全局顺序。
- 发布时 token 和 Channel 写权限按该发布提交前最新的 committed 状态判断；权限校验与消息提交
  之间不会插入另一个 token 或 Channel 修改。
- protected/private Channel 的 RemoveMember 先成功时，被移除成员排在其后的发布返回
  `PERMISSION_DENIED`；public Channel 不依赖成员列表，移除成员不改变其写权限。DeleteToken 先成功时，
  使用该 token 的后续发布返回 `UNAUTHENTICATED`；发布先成功时消息保持已提交。
- WriteObject 处理期间可以与其他操作交错；只有完整持久提交的对象才对读取和消息引用可见，
  对象的完整提交是该写入的顺序点。
- 写请求只在服务端完成同步持久化后返回成功。成功响应表示修改已经提交并满足服务端持久性承诺。
- 如果调用方在写 RPC 成功返回后才发起非流式读 RPC，该读必须观察到该写的完整结果。相互重叠
  的修改和非流式读取按服务端全局顺序观察提交前或提交后的完整状态，不能观察部分提交。
- Subscribe 的批次可见性和起点规则见 3.7 节。需要固定消息水位时，用 GetStatus 记录 `max_seq`，再用
  Fetch 读取并只处理不超过该水位的消息；这不固定跨 RPC 的权限状态。

### 2.9 发布结果不确定时的处理

如果 `Publish` 或 `PublishAutoSeq` 没有返回成功响应，不能仅凭客户端看到的错误判断消息一定没有
提交。连接中断、客户端取消、`DEADLINE_EXCEEDED`、部分 `UNAVAILABLE` 等失败可能发生在服务端
已经开始或完成持久化之后。必须保留该调用使用的 UUID，并用同一 UUID 重试 Publish 或 PublishAutoSeq；重试返回
`ALREADY_EXISTS` 时，用该 UUID 调用 `GetSeqByUuid` 取得原 committed 消息的 seq，不扫描消息或 UUID 历史。
使用新 UUID 是新的发布尝试。

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
| `ALREADY_EXISTS` | 保证未提交；UUID 已被此前 committed 消息使用 |
| 其他任何非 `OK` 返回或没有 gRPC status | 结果不确定 |

服务端在提交前观察到取消或 deadline 时不提交本次消息；提交已经开始后，取消或 deadline 不能保证
阻止持久化。调用方无法仅凭最终的 `CANCELLED` 或 `DEADLINE_EXCEEDED` 区分这两种情况。

因此 `CANCELLED`、`DEADLINE_EXCEEDED`、`UNKNOWN`、`UNAVAILABLE`、`INTERNAL`、连接中断、代理重置
等都必须按结果不确定处理。即使某次 `UNAVAILABLE` 看起来发生在连接建立前，调用方也不能依赖错误
文本推断未提交。只有表中明确标为“保证未提交”的返回，才允许直接进入错误修复和重试判断；保证
未提交但不可恢复的参数、权限或 payload 错误仍不得盲目重试。

上表只适用于由兼容 OpenEvent 服务端产生的终态。如果代理、负载均衡器或客户端本地可能在请求已
转发后合成同名 status，且部署无法证明该 status 发生在转发前，仍必须按结果不确定处理。

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- 请求必须提供有效的 `principal` 和 `token`；`principal=0` 返回 `INVALID_ARGUMENT`，认证失败返回
  `UNAUTHENTICATED`。
- `max_seq`：最大已提交消息 seq；只有系统初始化消息时为 0。
- `min_seq`：有效初始化的服务端始终为 0，因为 seq 0 永久保留。

客户端可以把 `max_seq+1` 作为下一次 Publish 的候选 seq。并发发布先占用该值时，Publish 返回
`ABORTED`。

### 3.2 AllocateUuids

```protobuf
rpc AllocateUuids(AllocateUuidsRequest) returns (AllocateUuidsResponse);
```

请求只包含必须位于 `1..1024` 的 `count`。响应恰好包含 count 个全局单调递增且连续的 UUID，按递增顺序
返回。分配不认证，也不创建消息。成功调用会持久化推进分配水位；即使没有消息消费，已分配 UUID 也不会重复使用。仅分配不会使 UUID
可被 GetSeqByUuid 查到。

### 3.3 GetSeqByUuid

```protobuf
rpc GetSeqByUuid(GetSeqByUuidRequest) returns (GetSeqByUuidResponse);
```

请求只包含 `uuid`，不携带 `principal` 或 `token`。该不认证读取不改变分配或消息状态。UUID 为 0 时，系统初始化消息返回
`seq=0`。其他 UUID 除非已被 committed 消息消费，否则返回 `NOT_FOUND`；未分配 UUID 和已分配但未使用 UUID 均属于此情况。
成功时 `seq` 是消费该 UUID 的 committed 消息序号。

### 3.4 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

客户端指定 seq 做 CAS 发布。成功条件：

- token 与 principal 匹配。
- payload 未超过上限。
- `seq == 当前 max_seq + 1`。
- Channel 存在且调用方有写权限。
- recipients 满足 2.6。
- object_keys 满足 2.7。

UUID 校验、重复优先顺序和恢复规则以 2.4 和 2.9 节为准。

| 条件 | 返回 |
|------|------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Channel 不存在 | `NOT_FOUND` |
| 无写权限 | `PERMISSION_DENIED` |
| recipient 非成员 | `INVALID_ARGUMENT` |
| ObjectKey 超过 1024 个、object ID 为 0 或对象 token 为空 | `INVALID_ARGUMENT` |
| 对象不存在/未提交或对象 token 不匹配 | `NOT_FOUND` |

成功返回空响应。服务端生成 `ts_ms`，消息随后可被有权限的调用方读取或订阅。

### 3.5 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

成功条件与 Publish 相同，但 seq 由服务端分配。UUID 校验、重复优先顺序和恢复规则以 2.4 和 2.9 节为准。
响应中的 seq 是已提交消息序号。

### 3.6 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

请求规则：

- token 与 principal 匹配。
- limit 必须在 `1..1000`。
- channels 为空表示不过滤；非空时，列表中的每个 Channel 都必须存在且调用方有读取权限，否则整个
  请求返回错误；不存在返回 `NOT_FOUND`，不可读返回 `PERMISSION_DENIED`。校验通过后只返回这些 Channel
  中调用方可读的消息。
- `only_my_recipient=true` 时应用 recipient 过滤。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从 seq 0 开始扫描；是否返回 seq 0 取决于过滤条件 |
| `> max_seq` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `1..max_seq` | 从该 seq 开始扫描可见消息 |

响应语义：

- messages 最多包含 limit 条经过 ACL、channels 和 recipient 过滤的消息。显式 channels 的完整权限校验在
  扫描前完成，任一 Channel 校验失败都不会返回部分结果。
- 服务端可以因扫描预算或响应大小预算提前结束一页，因此 messages 可能少于 limit，甚至在大量
  不可见消息时为空；调用方必须使用返回的 `next_seq` 继续，不能把短页当作尾部。
- 单条可返回消息可以突破页面软大小预算，但仍受 gRPC 传输硬上限约束；服务端不会因软预算在同一
  游标上反复返回空页。
- `next_seq` 是下一次继续扫描的建议起点。
- `last_seq` 是本次响应观察到的最大已提交消息序号。
- `next_seq <= last_seq` 时可以继续 Fetch；`next_seq > last_seq` 表示到达本次观察到的尾部。
- ACL 或过滤不会让 next_seq 回退。
- `from_seq=0` 是包含起点的消息位置，不是空结果哨兵。系统初始化消息和其他消息一样接受 Channel、
  recipient 及其他过滤条件。

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

按全局 seq 顺序推送当前调用方可见的消息。

- token 与 principal 匹配。
- Subscribe 不进入非流式读写的全局线性化顺序，与并发 RPC 之间没有线性化点。每批使用覆盖批次开始前
  已完成提交的一致读取视图；这些提交最晚在下一次成功的 100 ms 视图刷新后可见。
- `channels` 为空时不按 Channel ID 过滤；非空时，列表中的每个 Channel 都必须存在且调用方有读取权限，
  否则整个 stream 返回错误；不存在返回 `NOT_FOUND`，不可读返回 `PERMISSION_DENIED`。校验通过后只推送
  所列 Channel 中调用方可读的消息。
- `only_my_recipient=true` 时应用 recipient 过滤。
- 校验通过后，被 Channel ACL、channels 或 recipient 过滤的记录不产生响应，但服务端扫描会消费这些记录并
  继续向后处理。订阅运行期间如果显式 Channel 的读取权限失效，后续批次返回 `PERMISSION_DENIED` 并结束
  stream。
- 未指定 channels 时，权限变化只使后续批次过滤已经不可读的 private Channel，不结束整个订阅。
  protected Channel 移除成员不改变读取权限。删除 token 后，下一批返回 `UNAUTHENTICATED` 并结束 stream。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从 seq 0 开始扫描；是否推送 seq 0 取决于过滤条件 |
| `1..max_seq` | 从该 seq 开始推送可见消息 |
| `max_seq + 1` | 接受订阅并等待该序号成为已提交消息 |
| `> max_seq + 1` | 返回 `OUT_OF_RANGE` |

`SubscribeResponse.message` 表示正常消息，包括通过过滤条件的系统初始化消息。起点超过当前尾部一个
位置时表示等待下一条消息；超过一个位置才直接
返回错误；订阅响应只包含消息，不包含游标字段。客户端取消或断开后
应记录最后已处理 seq，并从 `seq+1` 重新订阅或 Fetch。该序号恰好是当前 `max_seq+1` 时，Subscribe
保持打开并等待它提交；如果仍超过当前尾部一个以上的位置则返回 `OUT_OF_RANGE`。调用方也可以从最后
已处理 seq 包含式重订阅并过滤重放。扫描到当前尾部后，Subscribe 保持打开并等待后续提交的消息。

认证、显式 Channel 校验和起点 seq 校验成功后，服务端会在等待或推送任何 OpenEvent 消息之前发送 gRPC 初始响应 metadata。
即使起点消息或当前全部记录都被过滤，这也表示订阅已被接受。该 metadata 是 gRPC 控制响应，不是
`SubscribeResponse`，也不是 OpenEvent 消息。

有效且已经建立的 Subscribe stream 没有服务端固定存活时长。业务 gRPC 端口在传输层 30 秒没有活动后发送
一次 HTTP/2 PING；10 秒内收不到确认就关闭连接。服务端只接受间隔不少于 20 秒的客户端 PING。Python
高层 client 每 30 秒发送一次客户端 PING，并且不为 Subscribe 设置 deadline；使用其他 gRPC client 或
生成式 stub 的调用方必须为自己的 channel 配置与服务端兼容的传输层保活策略。

服务端开始正常或致命关闭时，会主动结束已经建立的 Subscribe stream；客户端应按其他 stream 中断
处理，记录最后已处理的 seq，并按上述恢复规则继续订阅或 Fetch。

## 4. ObjectStorageService

ObjectStorageService 运行在业务端口。对象写入使用普通 `principal + token` 认证；metadata 和 data
读取只使用 ObjectKey capability。

### 4.1 WriteObject

```protobuf
rpc WriteObject(WriteObjectRequest) returns (WriteObjectResponse);
```

- `name` 和 `type` 必填，各自的 UTF-8 编码最多 255 bytes。
- `description` 可以为空，UTF-8 编码最多 4096 bytes。
- `data` 必须为 `1..4,194,304` bytes。4 MiB 是固定限制，不受消息 payload 配置影响。
- 服务端把这些字符串作为不透明 metadata 保存，不解释对象内容，也不根据 metadata 生成文件路径。
- 成功返回非 0、全局递增的 `object_id` 和不透明 `object_token`。ID 可以跳号且不复用；对象不可变。
- 对象 token 由 CSPRNG 生成 32 bytes，并编码为 43 个无 padding Base64URL 字符。调用方必须把其
  表示视为不透明值。
- 只有对象 data 和 metadata 都持久提交后才返回成功。处理开始后的取消、deadline、连接中断或
  部分服务端错误可能造成结果不确定；客户端取消后，服务端仍会继续提交或清理。
- API 没有幂等键或按创建者查询接口。调用方需要自行决定如何处理结果不确定的对象写入，避免产生不需要的
  重复不可变对象。

metadata 或 data 大小非法返回 `INVALID_ARGUMENT`；principal/token 无效返回 `UNAUTHENTICATED`；
对象文件存储空间、quota 或 inode 耗尽返回 `RESOURCE_EXHAUSTED`；RocksDB 写入失败以及其他存储可用性故障
返回 `UNAVAILABLE`。RocksDB 写入失败仍会触发服务端退出。
致命存储故障按 2.1 节处理。提交阶段的存储故障可能使对象结果不确定并导致服务端退出；重启后只有
已提交对象可见，不会公开部分写入的对象。

### 4.2 GetObjectMetadata

```protobuf
rpc GetObjectMetadata(GetObjectMetadataRequest) returns (GetObjectMetadataResponse);
```

返回已提交对象的 `name`、`type`、`description` 和总 `nbytes`。成功不代表对象 data 已通过完整性检查。

### 4.3 ReadObject

```protobuf
rpc ReadObject(ReadObjectRequest) returns (ReadObjectResponse);
```

`nbytes` 必须大于 0。服务端校验 committed 对象的文件类型和总大小后，只按以下规则读取请求范围：

| 条件 | 结果 |
|------|------|
| `offset < object size` | 返回 `min(nbytes, object size - offset)` bytes |
| `offset == object size` | 成功返回空 `data` |
| `offset > object size` | `OUT_OF_RANGE` |
| `nbytes == 0` | `INVALID_ARGUMENT` |

`nbytes` 可以是任意正 uint64，超出剩余内容的请求只返回剩余内容。

对 GetObjectMetadata 和 ReadObject，`object_id=0` 或对象 token 为空返回 `INVALID_ARGUMENT`；对象
不存在/未提交和所有 token 不匹配统一返回 `NOT_FOUND`。其他非空 token 作为不透明字符串处理，
不会仅按语法拒绝。若 committed data 缺失、不是普通文件或大小错误，ReadObject 返回
`DATA_LOSS`，并按 2.1 节退出。此接口不检测同大小的外部内容变更。

## 5. ChannelService

### 5.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- `name` 必填，UTF-8 编码最多 255 bytes；`protocol` 可以为空，非空时最多 255 bytes；
  `description` 可以为空，最多 4096 UTF-8 bytes。
- 请求参数 `members` 最多 1024 项，每个 principal 必须大于 0。参数检查后对成员去重，并自动加入创建者，
  创建者只保留一份。name 不要求全局唯一。
- `channel_id=0` 为系统 Channel，不能通过此接口创建。
- visibility 必须是 public、protected 或 private；未设置时默认为 public。
- 成功响应返回创建后的 ChannelInfo。

### 5.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- 读取权限见 2.5。
- Channel 不存在返回 `NOT_FOUND`。
- 无读取权限返回 `PERMISSION_DENIED`。

### 5.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- 当前不支持分页。
- 先按读取权限过滤，再应用 filter。
- `CHANNEL_FILTER_ALL` 返回全部可见 Channel。
- `CHANNEL_FILTER_JOINED` 返回调用方已加入的可见 Channel。
- `CHANNEL_FILTER_OWNED` 返回调用方创建的可见 Channel。
- 非法 filter 返回 `INVALID_ARGUMENT`。

### 5.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

- 请求必须通过 `principal + token` 认证，操作者 principal 必须大于 0，目标成员 principal 也必须大于 0。
- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 目标已是成员时返回 `ALREADY_EXISTS`。

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- 请求必须通过 `principal + token` 认证，操作者 principal 必须大于 0，目标成员 principal 也必须大于 0。
- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 不允许移除创建者本人。
- 移除非成员目标是幂等成功。protected/private Channel 移除成员成功后，该成员的后续写入返回
  `PERMISSION_DENIED`；public Channel 的写权限不受成员列表影响。

## 6. AdminService

AdminService 定义在 [`proto/admin.proto`](../proto/admin.proto)，只运行在独立管理端口，不携带
业务 principal/token。部署必须通过网络隔离或外部授权保护管理端口。

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 6.1 AddToken 和 DeleteToken

- `AddToken` 的 `target_principal` 必须大于 0；否则返回 `INVALID_ARGUMENT`。每次成功调用都会创建一个新的 token binding；同一
  principal 可以拥有多个 token，接口没有幂等键，也不会按 principal 去重。
- `DeleteToken` 的 `target_token` 不能为空；为空返回 `INVALID_ARGUMENT`。不存在的 token 也按成功处理，删除操作幂等。token 本身按
  不透明字符串匹配，不接受通配或前缀删除。

### 6.2 ListMessages

```protobuf
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

- 不应用 Channel ACL 或 recipient 过滤。
- from_seq 是包含起点；0 表示从 seq 0 开始扫描，返回页按本节的 limit 和响应大小预算规则确定。
- limit 必填且范围为 `1..1000`。
- 消息按 seq 升序返回；服务端可以因响应大小预算在达到 limit 前结束一页。
- 单条消息可以突破页面软大小预算，但仍受 gRPC 传输硬上限约束；因大小预算延后的消息不会被游标
  跳过。
- next_seq 是下一页建议起点。
- last_seq 是查询开始时的最大已提交消息序号。
- ListMessages 起点超过尾部时返回空页和尾部下一位置。
  Subscribe 起点等于 `max_seq+1` 时等待新消息，只有超过 `max_seq+1` 才返回 `OUT_OF_RANGE`。
  `next_seq > last_seq` 表示到达本页观察到的尾部。
- 每页是一致视图，但并发追加可使后续页的 last_seq 增大。

## 7. 兼容性建议

- 两份 proto 共同组成公开协议契约。
- 保留系统 Channel 及其 `system.v1` payload 定义见
  [`SYSTEM_PROTOCOL_cn.md`](SYSTEM_PROTOCOL_cn.md)。
- 调用方按 gRPC status code 分支，不依赖错误文本。
- 消费者应记录最后已处理 seq；Subscribe 重连遵循 3.7 节的恢复规则。
- 业务 principal 必须为正整数；`principal=0` 保留给服务端系统行为。
- ObjectKey 是凭据。必须保持 repeated 字段顺序、禁止记录 token，也不能假设 capability 附加到
  一条可读消息后仍保持私密。
- payload 的业务 schema 放在 Channel protocol 对应的业务协议中。
