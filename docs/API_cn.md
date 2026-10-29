# OpenEvent API 契约（gRPC）

[English version](API.md)

> 版本: 0.13.0
> 日期: 2026-10-28

本文档描述业务协议 [`proto/openevent.proto`](../proto/openevent.proto) 和管理协议
[`proto/admin.proto`](../proto/admin.proto) 的公开 API 行为。

## 1. 基础约定

- 所有接口通过 `gRPC + Protocol Buffers` 提供。
- 有效初始化的服务端始终包含永久保留的全局初始化消息 `seq=0`；此后的业务消息和 Channel 系统消息
  共用从 `seq=1` 开始的全局消息序列。
- 每条业务 `EventMessage` 的 `ts_ms` 是服务端收到发布请求时的 Unix 毫秒时间戳。
- 业务消息使用由 `AllocateUuids` 分配的非 0 `uuid`；Channel 系统消息由服务端从同一分配器取得非 0 UUID。
  只有全局初始化消息使用 `uuid=0`。
- 业务 `payload` 作为不透明 bytes 保存。所有服务端系统消息的识别、字段和 payload schema 统一见
  [系统协议](SYSTEM_PROTOCOL_cn.md)；读取方先区分系统消息，再按 Channel 的业务协议解析业务消息。
- 业务 principal 必须大于 0。`principal=0` 保留给服务端生成的系统行为；在业务及 token 管理请求的 principal、
  Channel creator/member、recipient 或对象创建者位置使用 0 时返回 `INVALID_ARGUMENT`。
- OpenEvent 在当前数据历史中永久保留已提交消息、Channel metadata 和不可变对象；数据历史的边界见 1.2 节。

### 1.1 服务划分

| Service | 职责 |
|---------|------|
| `EventService` | 状态查询、消息发布、批量拉取、流式订阅 |
| `ObjectStorageService` | 不可变对象写入、单个/批量 metadata 查询和部分数据读取 |
| `ChannelService` | Channel 创建、查询、列表、成员管理 |
| `AdminService` | token 管理和全部消息的管理查询 |

### 1.2 数据历史与备份恢复

消息 `seq`、UUID、Channel ID 和 object ID 的单调递增与不复用保证，以及消息和对象的永久保留、
ObjectKey 的永久有效性，都以当前数据历史为作用域。复用完整数据目录正常重启或崩溃恢复，不改变这条历史。

恢复旧备份则视为切换到另一条数据历史，而不是继续恢复前的历史。恢复后的状态和分配水位以备份为准；
原历史在备份之后的修改不在恢复后的历史中，相应的序号和 ID 值可能再次被分配。备份中恢复的消息、
Channel、对象、token 和 ObjectKey 仍按恢复后的状态和本文契约可用；恢复操作不会额外撤销这些凭据。

不能把恢复前历史中的读取进度、订阅游标或未完成的 UUID 重试状态直接接到恢复后的历史上；资源引用
以恢复后的数据历史为准。当前接口不携带数据历史标识，不会自动识别或转换跨历史的游标、重试状态和引用。

## 2. 通用语义

### 2.1 gRPC 状态码

| 状态码 | 语义 |
|--------|------|
| `UNAUTHENTICATED` | 请求中的 `principal + token` 组合无效 |
| `PERMISSION_DENIED` | ACL 检查失败，或调用方无权执行操作 |
| `NOT_FOUND` | Channel 不存在，或对象 capability 不存在/无效 |
| `ALREADY_EXISTS` | 资源已存在，例如重复添加成员或 UUID 已被消息使用 |
| `INVALID_ARGUMENT` | 字段缺失、值非法，或 recipients 不满足成员约束 |
| `RESOURCE_EXHAUSTED` | payload 或 Channel 成员总数超过上限，或对象文件存储空间/quota/inode 不足 |
| `OUT_OF_RANGE` | ReadObject offset 大于对象大小，或 Subscribe 起点超过 `max_seq + 1` |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | 调用被取消；写入结果可能不确定 |
| `DEADLINE_EXCEEDED` | 调用超过 deadline；写入结果可能不确定 |
| `UNAVAILABLE` | 服务或依赖当前不可用 |
| `DATA_LOSS` | 持久化存储损坏；服务端会退出 |
| `INTERNAL` | 未预期错误 |

`RESOURCE_EXHAUSTED` 的对象存储范围只包括对象文件写入、同步和清理过程中的空间、quota 或 inode
耗尽；元数据存储写入失败返回 `UNAVAILABLE`，并触发服务端退出。

致命存储故障使服务端停止接收新调用并以非零状态退出。触发故障的 RPC 尽力返回原始错误：存储损坏为
`DATA_LOSS`，其他致命存储故障为 `UNAVAILABLE`；已经进入执行阶段的普通 RPC 可以完成，活动订阅会结束。

调用方按 gRPC 状态码处理错误。

### 2.2 消息序列

- `EventMessage.seq` 是全局单调递增消息序号。
- `seq=0` 是永久保留的全局初始化消息。`max_seq=0` 表示当前只有该消息。
- 已提交 seq 不复用。
- 业务消息和 Channel 系统消息的 seq 都按 `max_seq + 1` 递增；读取方按服务端返回的游标继续扫描。
  创建 Channel 或实际修改成员也会推进 `max_seq`，并可能使并发 Publish 的 seq CAS 返回 `ABORTED`。
- `GetStatus`、`Fetch` 和 `Subscribe` 都使用同一消息序列。

### 2.3 Payload

- `Publish` 与 `PublishAutoSeq` 都接受 `payload`，且 payload 可以为空。
- 写入大小上限由部署配置 `max_payload_bytes` 决定，只用于业务发布请求的 payload 校验，不限制服务端
  生成的系统消息。
- 超限返回 `RESOURCE_EXHAUSTED`，不产生新消息。
- Fetch、Subscribe 和管理端 ListMessages 始终返回历史消息的完整 payload，包括下调写入上限后。
  分页预算决定返回的页边界。

### 2.4 消息 UUID

- UUID 0 保留给全局初始化消息。
- 每个 Publish 和 PublishAutoSeq 请求都必须携带一个由 AllocateUuids 返回的非 0 UUID。
- Channel 系统消息与 AllocateUuids 共用 UUID 分配器；服务端内部取号并在对应 Channel 修改的同一次
  提交中消费该 UUID。客户端分配的 UUID 不会被系统消息占用。
- 未提交或未实际改变状态的 Channel 修改操作不分配或消费 UUID，也不推进消息序号；其他并发操作仍可推进水位。
- 全局初始化消息参与普通 UUID 查询，`GetSeqByUuid(0)` 返回 `seq=0`；客户端不能分配或发布
  UUID 0。业务发布使用未由服务端分配的 UUID 时返回 `INVALID_ARGUMENT`，已被 committed 消息消费时返回
  `ALREADY_EXISTS`。
- 认证成功后，已被消费的 UUID 必须先于 payload、ObjectKey、Channel、ACL 和 Publish seq-CAS 校验返回
  `ALREADY_EXISTS`。重复处理不比较任何这些请求字段；一个 UUID 最多消费一条消息。
- Python 调用方式见[使用指南](USAGE_cn.md#发布消息)。
- 分配与查询 RPC 分别以 3.2 和 3.3 节为准。发布结果不确定时的完整恢复流程只以 2.9 节为准。

### 2.5 Channel ACL

| 可见性 | 读取权限 | 写入权限 |
|--------|----------|----------|
| `VISIBILITY_PUBLIC` | 所有已认证调用方 | 所有已认证调用方 |
| `VISIBILITY_PROTECTED` | 所有已认证调用方 | Channel 成员 |
| `VISIBILITY_PRIVATE` | Channel 成员 | Channel 成员 |

系统 Channel `channel_id=0` 对所有已认证调用方只读；固定字段、列表归属和初始化消息定义见
[`SYSTEM_PROTOCOL_cn.md`](SYSTEM_PROTOCOL_cn.md)。普通 Channel 中的系统消息沿用该 Channel 的当前读取
权限，不提供特殊投递权限；被移出 private Channel 的调用方不保证能读到移除事件。

### 2.6 Recipients

- `recipients` 是消息的定向接收 principal 列表；空列表表示无定向接收方。
- 发布时每个 recipient 都必须是对应 Channel 成员，否则返回 `INVALID_ARGUMENT`。
- `only_my_recipient=true` 时只返回或推送 recipients 包含当前 principal 的消息；空 recipients
  不匹配该过滤条件。

### 2.7 对象 Capability 与消息引用

- `ObjectKey` 由 `object_id + object_token` 组成，是永久 bearer capability。任何同时持有两者的
  调用方都能读取对象 metadata 和 data，不需要 principal token 或 Channel 权限。
- 对象 capability 可以转交且永久有效。请妥善保管 `object_token`。
- Publish 和 PublishAutoSeq 最多接受 1024 个 `object_keys`。服务端不去重，并按请求顺序保存。
- 每个引用都必须包含非 0 object ID 和非空 token。格式错误返回 `INVALID_ARGUMENT`；对象不存在、
  尚未提交或 token 不匹配返回 `NOT_FOUND`。
- 任一引用无效都会拒绝整条消息，`max_seq` 不推进。
- Fetch、Subscribe 和管理端 ListMessages 返回完整 ObjectKey；任何有权读取消息的调用方都会取得
  消息中的全部 capability。

### 2.8 读写线性化与提交顺序

- 所有修改和非流式有状态业务/管理读 RPC 具有一个服务端全局顺序。写请求在该顺序中提交，
  非流式读请求在自己的顺序位置取得一致 snapshot。Subscribe 不属于该全局顺序。
- 发布时 `principal + token` 组合和 Channel 写权限按该发布提交前最新的 committed 状态判断；权限校验与消息提交
  之间不会插入另一个 token 或 Channel 修改。
- protected/private Channel 的 RemoveMember 先成功时，被移除成员排在其后使用未消费 UUID 的发布返回
  `PERMISSION_DENIED`；public Channel 不依赖成员列表，移除成员不改变其写权限。DeleteToken 先成功时，
  使用被删除组合的后续发布返回 `UNAUTHENTICATED`；其他 principal 下相同 token 字符串不受影响。
  发布先成功时消息保持已提交。
- WriteObject 处理期间可以与其他操作交错；只有完整持久提交的对象才对读取和消息引用可见，
  对象的完整提交是该写入的顺序点。
- CreateChannel、实际添加成员和实际移除成员，各自与一条对应的 Channel 系统消息原子提交。Channel
  metadata、系统消息、UUID 分配与消费、`max_seq` 要么全部提交，要么全部不提交。初始化消息是新 Channel
  的第一条消息，先于该 Channel 的任何业务消息或成员变更消息。
- 写请求只在服务端完成同步持久化后返回成功。成功响应表示修改已经提交并满足服务端持久性承诺。
- 如果调用方在写 RPC 成功返回后才发起非流式读 RPC，该读必须观察到该写的完整结果。相互重叠
  的修改和非流式读取按服务端全局顺序观察提交前或提交后的完整状态，不能观察部分提交。
- Subscribe 的批次可见性和起点规则见 3.7 节。需要固定消息水位时，用 GetStatus 记录 `max_seq`，再用
  Fetch 读取并只处理不超过该水位的消息；这不固定跨 RPC 的权限状态。

### 2.9 发布结果不确定时的处理

发布结果不确定时，保留原 UUID，用同一 UUID 重试 Publish 或 PublishAutoSeq；返回 `ALREADY_EXISTS`
时，通过 `GetSeqByUuid` 取得已提交消息的 seq。使用新 UUID 会被视为新的发布尝试。

根据服务端响应，按下表判断本次调用的结果：

| 返回 | 提交结果 |
|------|----------|
| `OK` | 已同步持久化并提交 |
| `UNAUTHENTICATED` | 本次未提交 |
| `PERMISSION_DENIED` | 本次未提交 |
| `NOT_FOUND` | 本次未提交 |
| `INVALID_ARGUMENT` | 本次未提交 |
| `RESOURCE_EXHAUSTED` | 本次未提交 |
| `ABORTED` | 本次未提交；Publish 的 seq CAS 冲突 |
| `ALREADY_EXISTS` | 此 UUID 对应的消息已在此前调用中提交 |
| 其他任何非 `OK` 返回或没有 gRPC status | 结果不确定 |

取消、超时和连接中断按结果不确定处理。

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- 请求必须提供有效的 `principal` 和 `token`；`principal=0` 返回 `INVALID_ARGUMENT`，认证失败返回
  `UNAUTHENTICATED`。
- 响应仅包含 `max_seq`：最大已提交消息 seq；只有全局初始化消息时为 0。

客户端可以把 `max_seq+1` 作为下一次 Publish 的候选 seq。并发业务发布或 Channel 系统消息先占用该值时，
Publish 返回 `ABORTED`。

### 3.2 AllocateUuids

```protobuf
rpc AllocateUuids(AllocateUuidsRequest) returns (AllocateUuidsResponse);
```

请求只包含必须位于 `1..1024` 的 `count`。响应恰好包含 count 个全局单调递增且连续的 UUID，按递增顺序
返回。分配不认证，也不创建消息。成功调用会持久化推进分配水位；即使没有消息消费，已分配 UUID 也不会重复使用。仅分配不会使 UUID
可被 GetSeqByUuid 查到。Channel 系统消息也会推进同一分配水位，因此相邻两次调用之间的 UUID 可以不连续。

### 3.3 GetSeqByUuid

```protobuf
rpc GetSeqByUuid(GetSeqByUuidRequest) returns (GetSeqByUuidResponse);
```

请求只包含 `uuid`，不携带 `principal` 或 `token`。该不认证读取不改变分配或消息状态。UUID 为 0 时，全局初始化消息返回
`seq=0`。其他 UUID 除非已被 committed 消息消费，否则返回 `NOT_FOUND`；未分配 UUID 和已分配但未使用 UUID 均属于此情况。
成功时 `seq` 是消费该 UUID 的 committed 消息序号。

### 3.4 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

客户端指定 seq 做 CAS 发布。成功条件：

- `principal + token` 组合有效。
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

- `principal + token` 组合有效。
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

- messages 最多包含 limit 条经过 ACL、channels 和 recipient 过滤的消息。
- 服务端可以因扫描预算或响应大小预算提前结束一页，因此 messages 可能少于 limit，甚至在大量
  不可见消息时为空；调用方必须使用返回的 `next_seq` 继续，不能把短页当作尾部。
- 单条可返回消息可以突破页面软大小预算，但仍受 gRPC 传输硬上限约束；服务端不会因软预算在同一
  游标上反复返回空页。
- `next_seq` 是下一次继续扫描的建议起点。
- `last_seq` 是本次响应观察到的最大已提交消息序号。
- `next_seq <= last_seq` 时可以继续 Fetch；`next_seq > last_seq` 表示到达本次观察到的尾部。
- ACL 或过滤不会让 next_seq 回退。

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

按全局 seq 顺序推送当前调用方可见的消息。

- `principal + token` 组合有效。
- Subscribe 不进入非流式读写的全局线性化顺序，与并发 RPC 之间没有线性化点。每批使用覆盖批次开始前
  已完成提交的一致读取视图；这些提交最晚在下一次成功的 100 ms 视图刷新后可见。
- `channels` 为空时不按 Channel ID 过滤；非空时，列表中的每个 Channel 都必须存在且调用方有读取权限，
  否则整个 stream 返回错误；不存在返回 `NOT_FOUND`，不可读返回 `PERMISSION_DENIED`。校验通过后只推送
  所列 Channel 中调用方可读的消息。
- `only_my_recipient=true` 时应用 recipient 过滤。
- 校验通过后，被 Channel ACL、channels 或 recipient 过滤的记录不产生响应，但服务端扫描会消费这些记录并
  继续向后处理。订阅运行期间如果显式 Channel 的读取权限失效，后续批次返回 `PERMISSION_DENIED` 并结束
  stream。
- 未指定 channels 时，后续批次自动包含新创建且可读的 Channel，以及新获得读取权限的 Channel，无需重建
  订阅；已经被过滤并消费的旧消息不会因此回放。权限变化只使后续批次过滤已经不可读的 private Channel，
  不结束整个订阅。protected Channel 移除成员不改变读取权限。删除订阅使用的 `principal + token` 组合后，
  下一批返回 `UNAUTHENTICATED` 并结束 stream。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从 seq 0 开始扫描；是否推送 seq 0 取决于过滤条件 |
| `1..max_seq` | 从该 seq 开始推送可见消息 |
| `max_seq + 1` | 接受订阅并等待该序号成为已提交消息 |
| `> max_seq + 1` | 返回 `OUT_OF_RANGE` |

`SubscribeResponse.message` 包含消息。扫描到尾部后，订阅保持打开并等待新消息。
取消或断开后，从最后已处理的 `seq+1` 重新订阅或 Fetch；也可以从最后已处理 seq 包含式重订阅，
由调用方过滤重放。

认证、显式 Channel 校验和起点 seq 校验成功后，服务端会在等待或推送任何 OpenEvent 消息之前发送 gRPC
初始响应 metadata，其中固定键值 `openevent-subscription: accepted` 是订阅已被接受的唯一确认标记。
即使当前全部消息都被过滤，服务端也会发送该标记。空 metadata、其他键值或该键的其他值都不表示接受；
服务端在接受前拒绝订阅时不得发送该标记。

Subscribe stream 持续保持打开，直到发生中断。业务 gRPC 端口在传输层 30 秒没有活动后发送
一次 HTTP/2 PING；10 秒内收不到确认就关闭连接。服务端只接受间隔不少于 20 秒的客户端 PING。Python
高层 client 每 30 秒发送一次客户端 PING，10 秒内收不到确认就关闭本端连接，并且不为 Subscribe 设置
deadline；使用其他 gRPC client 或生成式 stub 的调用方必须为自己的 channel 配置与服务端兼容的传输层保活策略。

上述时间用于检测失联连接，不是 RPC 的执行时限。客户端决定单次 RPC 的 deadline；Python 高层 client
的 `timeout_ms` 只用于非流式 RPC，不改变服务端的保活时间。连接失效时，双方各自检测并释放本端连接；
网络中断可能阻止客户端的取消通知到达服务端。
只要连接正常并能回应 PING，没有业务消息也不会使订阅超时。

服务端开始正常或致命关闭时，会主动结束已经建立的 Subscribe stream；客户端应按其他 stream 中断
处理，记录最后已处理的 seq，并按上述恢复规则继续订阅或 Fetch。

### 3.8 通过消息发现 Channel

调用方可以在恢复消息历史的同时，根据 [Channel 系统消息](SYSTEM_PROTOCOL_cn.md) 发现 Channel，
无需先调用或定时调用 ListChannels。初始化和持续订阅使用同一套消息处理逻辑：

1. 调用 GetStatus，记录固定水位 `H=max_seq`。
2. 从 `from_seq=0` 开始 Fetch，设置 `channels=[]`、`only_my_recipient=false`。按 `next_seq` 翻页，
   只处理 `seq<=H` 的消息；直到 `next_seq>H` 才完成历史读取，不能因短页或空页提前结束。
3. 遇到系统消息时按系统协议处理 Channel 信息；是否把可读 Channel 纳入自己的业务列表，由调用方根据
   协议和成员信息决定。可读不等于已加入。如果只收到成员加入事件而缺少 Channel 属性，可按该
   `channel_id` 调用一次 GetChannel，无需为此重读历史或扫描列表。
4. 完成历史恢复后，以 `from_seq=H+1`、`channels=[]`、`only_my_recipient=false` 建立 Subscribe。
   恢复期间新提交的消息从这里继续读取；断线恢复遵循 3.7 节。

该流程固定消息水位，不固定跨 RPC 的 ACL。新增读取权限不会自动补发游标之前的历史；需要这些历史时，
调用方对相应 Channel 另行 Fetch。系统消息遵循 2.5 节的当前读取权限，也受 recipient 过滤影响，
因此不能用 `only_my_recipient=true` 发现 Channel，也不能靠此流程保证收到被移出 private Channel 的通知。

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
- 服务端把这些字符串作为不透明 metadata 保存，原样保存对象 data。
- 成功返回非 0、全局递增的 `object_id` 和不透明 `object_token`。ID 可以跳号且不复用；对象不可变。
- 对象 data 和 metadata 都持久提交后才返回成功。中断后结果不确定时，重试可能创建另一个对象。

metadata 或 data 大小非法返回 `INVALID_ARGUMENT`；principal/token 无效返回 `UNAUTHENTICATED`；
对象文件存储空间、quota 或 inode 耗尽返回 `RESOURCE_EXHAUSTED`；其他存储可用性故障返回 `UNAVAILABLE`。
致命存储故障按 2.1 节处理。

### 4.2 GetObjectMetadata

```protobuf
rpc GetObjectMetadata(GetObjectMetadataRequest) returns (GetObjectMetadataResponse);
```

返回已提交对象的 `name`、`type`、`description` 和总 `nbytes`。

### 4.3 ReadObject

```protobuf
rpc ReadObject(ReadObjectRequest) returns (ReadObjectResponse);
```

`nbytes` 必须大于 0，读取范围规则如下：

| 条件 | 结果 |
|------|------|
| `offset < object size` | 返回 `min(nbytes, object size - offset)` bytes |
| `offset == object size` | 成功返回空 `data` |
| `offset > object size` | `OUT_OF_RANGE` |
| `nbytes == 0` | `INVALID_ARGUMENT` |

`nbytes` 可以是任意正 uint64，超出剩余内容的请求只返回剩余内容。

对 GetObjectMetadata 和 ReadObject，`object_id=0` 或对象 token 为空返回 `INVALID_ARGUMENT`；对象
不存在/未提交和所有 token 不匹配统一返回 `NOT_FOUND`。token 作为不透明字符串处理。
若 committed data 缺失、不是普通文件或大小错误，ReadObject 返回 `DATA_LOSS`，并按 2.1 节退出。

### 4.4 BatchGetObjectMetadata

```protobuf
rpc BatchGetObjectMetadata(BatchGetObjectMetadataRequest) returns (BatchGetObjectMetadataResponse);
```

一次请求读取多个已提交对象的 metadata，不读取对象 data。请求只使用 `object_keys` 中的
ObjectKey capability，不要求普通 `principal + token`。

- `object_keys` 必须包含 `1..1024` 项。服务端先检查整个列表的数量，以及每一项的 `object_id`
  非 0、`object_token` 非空；任一条件不满足，返回 `INVALID_ARGUMENT`。
- 通过参数检查后，整批查询使用同一个线性化位置的数据库 snapshot，逐项读取已提交 metadata 并校验
  capability。对象不存在、未提交或 token 不匹配均返回 `NOT_FOUND`，与单个查询相同。
- 成功时 `metadata` 与请求的数量、顺序严格对应；每项为独立的 `ObjectMetadata` 消息类型，字段与
  4.2 节的单查响应相同。重复 ObjectKey 不去重，同一 `object_id` 的不同 token 也分别校验。
- 任一项失败，整个 RPC 失败，不返回部分结果。参数检查通过后的对象查询按输入顺序执行，返回遇到的
  第一个错误；存储故障仍按 2.1 节处理。

固定批量上限约束单次查询的工作量和响应大小；它不改变单个对象的 metadata 限制。

## 5. ChannelService

普通 Channel 去重后的成员总数最多为 1024，包含创建者。CreateChannel 在去重并加入创建者后、
AddMember 在新增成员后，若总数会超过此上限，则返回 `RESOURCE_EXHAUSTED`。这种拒绝不创建或修改
Channel，不产生系统消息，不分配或消费 UUID，也不推进 `max_seq`。重复添加已有成员的结果以 5.4 节为准。

### 5.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- `name` 必填，UTF-8 编码最多 255 bytes；`protocol` 可以为空，非空时最多 255 bytes；
  `description` 可以为空，最多 4096 UTF-8 bytes。
- 请求参数 `members` 最多 1024 项，每个 principal 必须大于 0。参数检查后对成员去重，并自动加入创建者，
  创建者只保留一份；结果须满足[第 5 节总则](#5-channelservice)的成员总数上限。name 不要求全局唯一。
- `channel_id=0` 为系统 Channel，不能通过此接口创建。
- visibility 必须是 public、protected 或 private；未设置时默认为 public。
- 成功时在新 Channel 内写入一条初始化系统消息，包含创建后的 Channel 属性和完整初始成员列表；
  初始成员（含创建者）不再逐个产生加入事件。消息 schema 见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  与 Channel 状态一起提交的规则见 2.8 节。
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
- 目标已是成员时返回 `ALREADY_EXISTS`，即使 Channel 已达到成员总数上限也一样，不产生系统消息。
- 新增成员须满足[第 5 节总则](#5-channelservice)的成员总数上限。
- 实际添加成员时，在该 Channel 内写入一条成员加入系统消息；schema 见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  原子提交规则见 2.8 节。

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- 请求必须通过 `principal + token` 认证，操作者 principal 必须大于 0，目标成员 principal 也必须大于 0。
- 操作者必须是 Channel 创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- Channel 不存在返回 `NOT_FOUND`。
- 不允许移除创建者本人。
- 移除非成员目标是幂等成功，不产生系统消息。
- 实际移除成员时，在该 Channel 内写入一条成员移除系统消息；schema 见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  原子提交规则见 2.8 节。
- protected/private Channel 移除成员成功后，该成员后续使用未消费 UUID 的发布返回 `PERMISSION_DENIED`；
  public Channel 的写权限不受成员列表影响。系统消息不绕过读取权限，见 2.5 节。

## 6. AdminService

AdminService 定义在 [`proto/admin.proto`](../proto/admin.proto)，只运行在独立管理端口，不要求调用者提供
业务认证凭据。AddToken 和 DeleteToken 的 principal/token 字段指定被管理的凭据，不用于认证管理调用者。
部署必须通过网络隔离或外部授权保护管理端口。

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 6.1 AddToken 和 DeleteToken

- 凭据由 `(principal, token)` 组合确定，同一 token 字符串可在不同 principal 下独立有效。
- `AddTokenRequest.principal` 必须大于 0；否则返回 `INVALID_ARGUMENT`。principal 由调用方分配，无需先创建或注册。
  成功使该 principal 与返回 token 的组合生效，并通过 `AddTokenResponse.token` 返回非空 token。
  同一 principal 可以拥有多个 token。
- `DeleteTokenRequest.principal` 必须大于 0，`token` 必须非空；否则返回 `INVALID_ARGUMENT`。删除只影响
  指定组合，不存在也成功。token 按不透明字符串精确匹配。

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
- 起点超过尾部时返回空页和尾部下一位置；`next_seq > last_seq` 表示到达本页观察到的尾部。
- 每页是一致视图，但并发追加可使后续页的 last_seq 增大。
