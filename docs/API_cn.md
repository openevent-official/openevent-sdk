# OpenEvent API 契约（gRPC）

[English version](API.md)

> 版本: 0.13.0
> 日期: 2026-10-28

本文档描述业务协议 [`proto/openevent/sdk/proto/openevent.proto`](../proto/openevent/sdk/proto/openevent.proto) 和管理协议
[`proto/openevent/sdk/proto/admin.proto`](../proto/openevent/sdk/proto/admin.proto) 的公开 API 行为。
自行生成客户端时，以 SDK 的 `proto/` 目录为协议搜索根目录。在自己的协议里使用
`openevent/sdk/proto/openevent.proto` 或 `openevent/sdk/proto/admin.proto` 作为导入路径。

## 1. 基础约定

- 服务端通过 gRPC 提供接口，使用 Protocol Buffers 定义消息。
- 服务端初始化完成后，始终保留序号为 0 的全局初始化消息。各频道（Channel）后续的业务消息和系统消息共用从 1 开始的全局序号。
- 每条业务 `EventMessage` 的 `ts_ms` 是服务端收到发布请求时的 Unix 毫秒时间戳。
- 业务消息使用由 `AllocateUuids` 分配的非 0 `uuid`；频道系统消息由服务端从同一分配器取得非 0 UUID。
  只有全局初始化消息使用 `uuid=0`。
- 服务端原样保存业务消息的正文字节（`payload`）。系统消息的识别方法、字段和正文格式见
  [系统协议](SYSTEM_PROTOCOL_cn.md)；读取方先区分系统消息，再按频道的业务协议解析业务消息。
- 业务身份编号（`principal`）必须大于 0。服务端专用的编号是 0。业务请求和 token 管理请求中的身份编号，以及频道创建者、成员、消息接收方和对象创建者的编号，填 0 都会返回 `INVALID_ARGUMENT`。
- OpenEvent 在当前数据历史中永久保留已提交的消息、频道元数据和不可变对象；数据历史的边界见 1.2 节。

### 1.1 服务划分

| 服务 | 职责 |
|---------|------|
| `EventService` | 状态查询、消息发布、批量拉取、流式订阅 |
| `ObjectStorageService` | 不可变对象写入、逐个或批量查询对象元数据、按范围读取对象内容 |
| `ChannelService` | 频道创建、查询、列表、成员管理 |
| `AdminService` | token 管理和全部消息的管理查询 |

### 1.2 数据历史与备份恢复

消息 `seq`、UUID、频道 ID 和对象 ID 的单调递增与不复用保证，以及消息和对象的永久保留、
ObjectKey 的永久有效性，都只在同一段数据历史内有效。复用完整数据目录正常重启或崩溃恢复，不改变这条历史。

恢复旧备份会切换到备份中的数据历史。服务端从备份记录的状态和编号继续运行。
原历史在备份之后的修改不在恢复后的历史中，相应的序号和 ID 值可能再次被分配。备份中恢复的消息、
频道、对象、token 和 ObjectKey 仍按恢复后的状态和本文契约可用；恢复操作不会额外撤销这些凭据。

不能把恢复前历史中的读取进度、订阅游标或未完成的 UUID 重试状态直接接到恢复后的历史上；资源引用
以恢复后的数据历史为准。当前接口不携带数据历史标识，不会自动识别或转换跨历史的游标、重试状态和引用。

## 2. 通用语义

### 2.1 gRPC 状态码

| 状态码 | 含义 |
|--------|------|
| `UNAUTHENTICATED` | 请求中的 `principal + token` 组合无效 |
| `PERMISSION_DENIED` | 频道权限检查失败，或调用方无权执行该操作 |
| `NOT_FOUND` | 频道不存在，或对象读取凭据不存在或无效 |
| `ALREADY_EXISTS` | 资源已存在，例如重复添加成员或 UUID 已被消息使用 |
| `INVALID_ARGUMENT` | 字段缺失、值非法，或接收方不在频道成员中 |
| `RESOURCE_EXHAUSTED` | 消息正文过大、频道成员过多，或对象文件用尽存储空间、磁盘配额或 inode |
| `OUT_OF_RANGE` | ReadObject offset 大于对象大小，或 Subscribe 起点超过 `max_seq + 1` |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | 调用被取消；写入结果可能不确定 |
| `DEADLINE_EXCEEDED` | 调用超时；写入结果可能不确定 |
| `UNAVAILABLE` | 服务或依赖当前不可用 |
| `DATA_LOSS` | 持久化存储损坏；服务端会退出 |
| `INTERNAL` | 未预期错误 |

对象存储仅在写入、同步或清理对象文件时耗尽磁盘空间、配额或 inode 的情况下返回 `RESOURCE_EXHAUSTED`。

致命存储故障使服务端停止接收新调用并以非零状态退出。触发故障的 RPC 尽力返回原始错误：存储损坏为
`DATA_LOSS`，其他致命存储故障为 `UNAVAILABLE`；已经进入执行阶段的普通 RPC 可以完成，活动订阅会结束。

调用方按 gRPC 状态码处理错误。

### 2.2 消息序列

- `EventMessage.seq` 是全局单调递增消息序号。
- `seq=0` 是永久保留的全局初始化消息。`max_seq=0` 表示当前只有该消息。
- 已提交的消息序号不会再分配。
- 业务消息和频道系统消息的 seq 都按 `max_seq + 1` 递增；读取方按服务端返回的游标继续扫描。
  创建频道或实际增减成员也会占用一个序号。此时并发的 `Publish` 若指定了同一序号，就会返回 `ABORTED`。
- `GetStatus`、`Fetch` 和 `Subscribe` 都使用同一消息序列。

### 2.3 Payload

- `Publish` 与 `PublishAutoSeq` 都接受 `payload`，且 payload 可以为空。
- 写入大小上限由部署配置 `max_payload_bytes` 决定，只用于业务发布请求的 payload 校验，不限制服务端
  生成的系统消息。
- 超限返回 `RESOURCE_EXHAUSTED`，不产生新消息。
- Fetch、Subscribe 和管理端 ListMessages 始终返回历史消息的完整 payload，包括下调写入上限后。
  一次返回多少条消息取决于分页限制。

### 2.4 消息 UUID

- UUID 0 保留给全局初始化消息。
- 每个 Publish 和 PublishAutoSeq 请求都必须携带一个由 AllocateUuids 返回的非 0 UUID。
- 频道系统消息与 `AllocateUuids` 共用同一个 UUID 分配器。服务端在提交频道变更时一并分配并使用系统消息的 UUID。系统消息不会使用已分给客户端的号码。
- 频道变更未提交或没有实际改变状态时，不会分配或使用 UUID，也不会占用消息序号。同时发生的其他操作仍可分配号码。
- 全局初始化消息也能按 UUID 查询，`GetSeqByUuid(0)` 返回 `seq=0`；客户端不能分配或发布
  UUID 0。业务发布使用未由服务端分配的 UUID 时返回 `INVALID_ARGUMENT`，UUID 已用于一条已提交消息时返回
  `ALREADY_EXISTS`。
- 认证成功后，若 UUID 已用于一条已提交的消息，直接返回 `ALREADY_EXISTS`，跳过正文、ObjectKey、频道、权限和 `Publish` 序号检查。一个 UUID 只能用于一条消息。
- Python 调用方式见[使用指南](USAGE_cn.md#发布消息)。
- 分配与查询 RPC 分别以 3.2 和 3.3 节为准。发布结果不确定时的完整恢复流程只以 2.9 节为准。

### 2.5 频道读写权限

| 可见性 | 读取权限 | 写入权限 |
|--------|----------|----------|
| `VISIBILITY_PUBLIC` | 所有已认证调用方 | 所有已认证调用方 |
| `VISIBILITY_PROTECTED` | 所有已认证调用方 | 频道成员 |
| `VISIBILITY_PRIVATE` | 频道成员 | 频道成员 |

系统频道（`channel_id=0`）对所有已认证调用方只读。它有哪些固定字段、会出现在哪些列表中，以及初始化消息的内容，见
[`SYSTEM_PROTOCOL_cn.md`](SYSTEM_PROTOCOL_cn.md)。普通频道里的系统消息也遵循该频道当前的读取权限。成员被移出私有频道后，可能读不到自己的移除通知。

### 2.6 Recipients

- `recipients` 列出这条消息指定的接收方身份编号。空列表表示没有指定接收方。
- 发布时指定的每个接收方都必须是该频道的成员，否则返回 `INVALID_ARGUMENT`。
- `Publish` 和 `PublishAutoSeq` 保存新消息时，每个接收方只保存一次，顺序按其在请求中首次出现的位置确定。
  读取消息时原样返回已保存的列表。
- `only_my_recipient=true` 时只返回或推送接收方列表中包含当前调用者的消息。接收方列表为空的消息会被过滤掉。

### 2.7 对象读取凭据与消息引用

- `ObjectKey` 由 `object_id + object_token` 组成，是永久有效的对象读取凭据。只要同时持有这两个值，就可以读取对象的元数据和内容，无需提供身份 token 或拥有频道权限。
- 这个读取凭据可以转交给其他人，请妥善保管 `object_token`。
- Publish 和 PublishAutoSeq 最多接受 1024 个 `object_keys`。服务端不去重，并按请求顺序保存。
- 每个引用都必须包含非 0 对象 ID 和非空 token。格式错误返回 `INVALID_ARGUMENT`；对象不存在、
  尚未提交或 token 不匹配返回 `NOT_FOUND`。
- 任一引用无效都会拒绝整条消息，`max_seq` 不推进。
- Fetch、Subscribe 和管理端 ListMessages 返回完整 ObjectKey；任何有权读取消息的调用方都会取得
  消息里的每一份对象读取凭据。

### 2.8 读写操作的先后顺序

- 业务端和管理端的所有修改，以及所有需要读取服务端状态的非流式请求，共用一个全局顺序。轮到写请求时提交变更，轮到读请求时取得当时的快照。`Subscribe` 不参与这个排序，读取规则见 3.7 节。
- 发布前，服务端按最新已提交状态检查 `principal + token` 和频道写权限。从检查通过到消息提交之间，其他请求不能修改 token 或频道。
- 如果先从受保护或私有频道移除了成员，被移除的成员再用尚未使用的 UUID 发布消息，会收到
  `PERMISSION_DENIED`。公开频道的写权限不依赖成员列表，移除成员也不改变其写权限。DeleteToken 先成功时，
  之后用这组凭据发布会返回 `UNAUTHENTICATED`。其他身份下恰好相同的 token 仍然有效。
  如果先发布成功，后来移除成员或删除凭据不会撤销已提交的消息。
- WriteObject 处理期间可以与其他操作交错；只有完整持久提交的对象才对读取和消息引用可见，
  对象写入在全局顺序中的位置由完整提交的时刻决定。
- CreateChannel、实际添加成员和实际移除成员，各自与一条对应的频道系统消息原子提交。频道元数据、系统消息、UUID 的分配与使用、`max_seq` 要么全部提交，要么全部不提交。初始化消息是新频道
  的第一条消息，先于该频道的任何业务消息或成员变更消息。
- 改变状态的写请求在变更同步持久化后返回成功。RemoveMember 移除非成员时，以服务端在全局顺序中
  确认该成员不存在的时刻为准，直接返回成功。
- 相互重叠的修改和非流式读取按服务端全局顺序观察提交前或提交后的完整状态，不能观察部分提交。
- Subscribe 的批次可见性和起点规则见 3.7 节。需要分阶段读取历史再接续订阅时，按 3.8 节用 Fetch
  第一页的 `last_seq` 记下本次要读到的最后一个序号。每次读取的权限仍以该次 RPC 为准。

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
| `ABORTED` | 本次未提交，`Publish` 指定的序号已经不是下一个序号 |
| `ALREADY_EXISTS` | 此 UUID 对应的消息已在此前调用中提交 |
| 其他任何非 `OK` 返回或没有收到 gRPC 状态码 | 结果不确定 |

取消、超时和连接中断按结果不确定处理。

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- 请求必须提供有效的 `principal` 和 `token`；`principal=0` 返回 `INVALID_ARGUMENT`，认证失败返回
  `UNAUTHENTICATED`。
- 响应仅包含 `max_seq`：最大已提交消息 seq；只有全局初始化消息时为 0。

客户端可以把 `max_seq+1` 作为下一次 Publish 的候选 seq。并发业务发布或频道系统消息先占用该值时，
Publish 返回 `ABORTED`。

### 3.2 AllocateUuids

```protobuf
rpc AllocateUuids(AllocateUuidsRequest) returns (AllocateUuidsResponse);
```

请求只有一个字段 `count`，取值为 `1..1024`。服务端按升序返回 `count` 个连续的 UUID。领取号码无需认证，也不会创建消息。
服务端在返回成功前保存下一个待分配的号码。已经分配的 UUID 即使用不上，也不会再次分配。
只有消息提交后才能通过 `GetSeqByUuid` 查到它。频道系统消息也会取号，因此两次调用得到的号段之间可以有间隔。

### 3.3 GetSeqByUuid

```protobuf
rpc GetSeqByUuid(GetSeqByUuidRequest) returns (GetSeqByUuidResponse);
```

此查询只需要 `uuid`，无需提供 `principal` 或 `token`，也不会分配 UUID 或更改消息。查询 UUID 0 返回全局初始化消息的序号 0。
其他 UUID 若已用于提交消息，就返回该消息的序号 `seq`。尚未分配或已分配但未使用的 UUID 均返回 `NOT_FOUND`。

### 3.4 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

客户端指定预期的下一个序号 `seq`。服务端在以下条件都满足时提交消息：

- `principal + token` 组合有效。
- payload 未超过上限。
- `seq == 当前 max_seq + 1`。
- 频道存在且调用方有写权限。
- recipients 满足 2.6。
- object_keys 满足 2.7。

UUID 的校验顺序、遇到重复 UUID 时的处理以及发布结果不确定时的处理见 2.4 和 2.9 节。

| 条件 | 返回 |
|------|------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| 频道不存在 | `NOT_FOUND` |
| 无写权限 | `PERMISSION_DENIED` |
| recipient 非成员 | `INVALID_ARGUMENT` |
| ObjectKey 超过 1024 个、对象 ID 为 0 或对象 token 为空 | `INVALID_ARGUMENT` |
| 对象不存在、尚未提交或对象 token 不匹配 | `NOT_FOUND` |

成功返回空响应。服务端生成 `ts_ms`，消息随后可被有权限的调用方读取或订阅。

### 3.5 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

成功条件与 Publish 相同，但 seq 由服务端分配。UUID 的校验顺序、遇到重复 UUID 时的处理以及发布结果不确定时的处理见 2.4 和 2.9 节。
响应中的 seq 是已提交消息序号。

### 3.6 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

请求规则：

- `principal + token` 组合有效。
- limit 必须在 `1..1000`。
- channels 为空表示不过滤；非空时，列表中的每个频道都必须存在且调用方有读取权限，否则整个
  请求返回错误；不存在返回 `NOT_FOUND`，不可读返回 `PERMISSION_DENIED`。校验通过后只返回这些频道
  中调用方可读的消息。
- `only_my_recipient=true` 时只返回指定当前调用者为接收方的消息。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从 seq 0 开始扫描；是否返回 seq 0 取决于过滤条件 |
| `> max_seq` | 返回空结果，`next_seq=max_seq+1`，`last_seq=max_seq` |
| `1..max_seq` | 从该 seq 开始扫描可见消息 |

返回结果：

- 每页的 `messages` 最多有 `limit` 条消息，按 `seq` 严格递增排列。结果只包含符合频道读取权限、频道列表和接收方条件的消息，过滤后序号可以不连续。
- 达到扫描条数或返回字节数限制时，服务端会提前结束本页，因此返回的消息可能少于 `limit` 条。如果扫到的消息全被过滤，本页可能为空。继续读取时使用返回的 `next_seq`。
- 单条消息超过每页的大小目标时会单独返回，仍须符合 gRPC 传输上限。调用方不会因为这条大消息而在同一位置反复读到空页。
- `next_seq` 是下一次继续扫描的建议起点。
- `last_seq` 是本次响应观察到的最大已提交消息序号。
- `next_seq <= last_seq` 时可以继续 Fetch；`next_seq > last_seq` 表示到达本次观察到的尾部。
- 权限检查和过滤都不会使 `next_seq` 回退。

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

按全局 seq 顺序推送当前调用方可见的消息。

- `principal + token` 组合有效。
- `Subscribe` 不与同时发生的其他 RPC 排在同一个读写顺序里。订阅每批读取一份一致的快照，其中包含本批开始前已经提交的变更。快照每 100 毫秒刷新一次，这些变更最晚在下次成功刷新后可见。
- `channels` 为空时不按频道 ID 过滤；非空时，列表中的每个频道都必须存在且调用方有读取权限，
  否则整个订阅返回错误；不存在返回 `NOT_FOUND`，不可读返回 `PERMISSION_DENIED`。校验通过后只推送
  所列频道中调用方可读的消息。
- `only_my_recipient=true` 时只返回指定当前调用者为接收方的消息。
- 通过校验后，服务端跳过因频道权限、频道列表或接收方条件被过滤的消息，继续向后扫描。如果调用方失去对指定频道的读取权限，后续批次返回 `PERMISSION_DENIED`，订阅随之结束。
- 未指定 channels 时，后续批次自动包含新创建且可读的频道，以及新获得读取权限的频道，无需重建
  订阅；已经跳过的旧消息不会补发。权限变化只使后续批次过滤已经不可读的私有频道，
  不结束整个订阅。被移出受保护频道的成员仍有读取权限。删除订阅使用的 `principal + token` 组合后，
  下一批返回 `UNAUTHENTICATED` 并结束订阅。

| `from_seq` | 行为 |
|------------|------|
| `0` | 从 seq 0 开始扫描；是否推送 seq 0 取决于过滤条件 |
| `1..max_seq` | 从该 seq 开始推送可见消息 |
| `max_seq + 1` | 接受订阅并等待该序号成为已提交消息 |
| `> max_seq + 1` | 返回 `OUT_OF_RANGE` |

`SubscribeResponse.message` 包含消息。扫描到尾部后，订阅保持打开并等待新消息。
取消或断开后，可以从最后已处理的序号加一开始重新订阅或调用 `Fetch`。也可以从最后已处理的序号开始重新订阅，由调用方跳过重复收到的消息。

服务端确认身份、指定频道的读取权限和起始序号后，会在等待或推送第一条消息前发送 gRPC 初始响应元数据。
只有其中包含 `openevent-subscription: accepted`，才表示服务端已接受订阅。
即使当前全部消息都被过滤，服务端也会发送该标记。元数据为空、只有其他键，或该键的值不同时，都不代表订阅已被接受；
服务端在接受前拒绝订阅时不得发送该标记。

订阅会一直保持打开，直到中断。业务 gRPC 端口在传输层 30 秒没有活动后发送
一次 HTTP/2 PING；10 秒内收不到确认就关闭连接。服务端只接受间隔不少于 20 秒的客户端 PING。Python
SDK 客户端每 30 秒发送一次 PING，10 秒内收不到确认就关闭连接。`Subscribe` 本身没有截止时间。使用其他 gRPC 客户端或根据协议生成的客户端时，须为自己的 gRPC 连接配置与服务端兼容的保活参数。

上述时间用于检测连接是否中断。客户端单独决定每次 RPC 的截止时间。Python SDK 客户端
的 `timeout_ms` 只用于非流式 RPC，不改变服务端的保活时间。连接失效时，双方各自检测并释放本端连接；
网络中断可能阻止客户端的取消通知到达服务端。
只要连接正常并能回应 PING，没有业务消息也不会使订阅超时。

服务端开始正常关闭或因致命故障退出时，会主动结束已有订阅。客户端按同样的断线恢复规则，记录最后处理的序号，继续订阅或调用 `Fetch`。

### 3.8 通过消息发现频道

要从历史消息开始持续发现频道，直接用 `from_seq=0`、`channels=[]`、`only_my_recipient=false`
建立一次 Subscribe。服务端按序推送可见的历史消息，到达尾部后继续等待新消息，无需先调用 Fetch 或 ListChannels。

调用方根据 [频道系统消息](SYSTEM_PROTOCOL_cn.md) 中的协议和成员信息，决定哪些可读频道
属于自己的业务列表。非成员也可以读取公开和受保护的频道。如果只收到成员加入事件而缺少
频道属性，可用该 `channel_id` 调用一次 `GetChannel`。

如果需要明确知道启动时已有的消息何时处理完，可以先用 `Fetch` 分页读取历史，再用 `Subscribe` 继续订阅。

1. 从 `from_seq=0` 开始 Fetch，设置 `channels=[]`、`only_my_recipient=false`。将第一页返回的
   `last_seq` 记为本次历史读取的最后一个序号 `H`，即使该页为空也使用这个值。
2. 从第一页起，每页只处理 `seq<=H` 的消息。`next_seq<=H` 时从 `next_seq` 继续 Fetch，短页或空页
   同样按此判断。后续页的 `last_seq` 可能增大，整个历史读取过程都使用最初的 `H`。
3. 当 `next_seq>H` 时，启动时的历史已处理完。从 `from_seq=H+1` 建立 Subscribe，沿用相同的过滤条件，
   继续读取恢复期间和之后提交的消息。

两种方式都按 3.7 节恢复中断的订阅。每页 Fetch 和每批 Subscribe 都按当时的读取权限过滤。
新增读取权限不会自动补发游标之前的历史，需要时可对相应频道另行 Fetch。系统消息也受
接收方过滤影响，因此不能用 `only_my_recipient=true` 发现频道。被移出私有频道的通知遵循 2.5 节。

## 4. ObjectStorageService

ObjectStorageService 运行在业务端口。对象写入使用普通 `principal + token` 认证；读取对象元数据和内容只需要对象读取凭据 ObjectKey。

### 4.1 WriteObject

```protobuf
rpc WriteObject(WriteObjectRequest) returns (WriteObjectResponse);
```

- `name` 和 `type` 必填，各自的 UTF-8 编码最多 255 字节。
- `description` 可以为空，UTF-8 编码最多 4096 字节。
- `data` 必须为 `1..4,194,304` 字节。4 MiB 是固定限制，不随消息正文的大小上限变化。
- 服务端原样保存这些描述字段和对象内容。
- 成功返回非 0 的 `object_id` 和不透明的 `object_token`。对象 ID 按服务端预留的先后顺序全局递增，
  可以跳号且不复用。并发上传时，较大 ID 的对象可能先提交，其调用也可能先返回。对象不可变。
- 对象内容和元数据都写入持久存储后才返回成功。中断后结果不确定时，重试可能创建另一个对象。

当 `principal=0`、描述字段无效或 `data` 大小超出范围时，返回 `INVALID_ARGUMENT`。
非零身份编号与 token 的组合无效时，返回 `UNAUTHENTICATED`。
写对象文件时若磁盘空间、配额或 inode 用尽，返回 `RESOURCE_EXHAUSTED`。其他导致存储不可用的故障返回 `UNAVAILABLE`。
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
| `offset < object size` | 返回 `min(nbytes, object size - offset)` 字节 |
| `offset == object size` | 成功返回空 `data` |
| `offset > object size` | `OUT_OF_RANGE` |
| `nbytes == 0` | `INVALID_ARGUMENT` |

`nbytes` 可以是 `uint64` 范围内的任意正整数，超出剩余内容的请求只返回剩余内容。

对 GetObjectMetadata 和 ReadObject，`object_id=0` 或对象 token 为空返回 `INVALID_ARGUMENT`；对象
不存在、尚未提交或 token 不匹配时，均返回 `NOT_FOUND`。token 按原字符串匹配，调用方无需解析其格式。
若已提交对象的数据文件缺失、类型不是普通文件或大小与记录不符，ReadObject 返回 `DATA_LOSS`，并按 2.1 节退出。

### 4.4 BatchGetObjectMetadata

```protobuf
rpc BatchGetObjectMetadata(BatchGetObjectMetadataRequest) returns (BatchGetObjectMetadataResponse);
```

一次请求读取多个已提交对象的元数据，不读取对象内容。调用方只需要提供 `object_keys` 中的对象读取凭据，无需提供 `principal + token`。

- `object_keys` 必须包含 `1..1024` 项。服务端先检查整个列表的数量，以及每一项的 `object_id`
  非 0、`object_token` 非空；任一条件不满足，返回 `INVALID_ARGUMENT`。
- 通过参数检查后，服务端在轮到这次查询时取得数据库快照，用同一份快照读取整批对象的元数据，并按输入顺序逐项检查读取凭据。对象不存在、未提交或 token 不匹配时，该项的错误均为 `NOT_FOUND`。
- 成功时 `metadata` 与请求的数量、顺序严格对应；每项为独立的 `ObjectMetadata` 消息类型，字段与 4.2 节查询单个对象时相同。重复 ObjectKey 逐项返回，同一 `object_id` 的不同 token 也分别校验。
- 任一项失败，整个 RPC 只返回一个错误，没有部分结果。各项错误中，存储损坏（`DATA_LOSS`）优先，
  其次是其他致命存储故障（`UNAVAILABLE`），最后是 `NOT_FOUND`。
  两类存储故障都会按 2.1 节触发服务端退出。

每批最多查询 1024 项，以限制单次查询的工作量和响应大小。每个对象的元数据仍按原有大小限制保存。

## 5. ChannelService

普通频道去重后的成员总数最多为 1024，包含创建者。CreateChannel 在去重并加入创建者后、
AddMember 在新增成员后，若总数会超过此上限，则返回 `RESOURCE_EXHAUSTED`。这种拒绝不创建或修改
频道，不产生系统消息，不分配或使用 UUID，也不推进 `max_seq`。重复添加已有成员的结果以 5.4 节为准。

### 5.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- `name` 必填，UTF-8 编码最多 255 字节；`protocol` 可以为空，非空时最多 255 字节；
  `description` 可以为空，最多 4096 个 UTF-8 字节。
- 请求参数 `members` 最多 1024 项，每个成员的身份编号必须大于 0。参数检查后对成员去重，并自动加入创建者，
  创建者只保留一份；成员总数不得超过[第 5 节](#5-channelservice)规定的上限。name 不要求全局唯一。
- `channel_id=0` 为系统频道，不能通过此接口创建。
- visibility 必须是 public、protected 或 private；未设置时默认为 public。
- 成功时在新频道内写入一条初始化系统消息，包含创建后的频道属性和完整初始成员列表；
  初始成员（含创建者）不再逐个产生加入事件。消息格式见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  与频道状态一起提交的规则见 2.8 节。
- 成功响应返回创建后的 ChannelInfo。
- 请求超时或成功响应丢失时，频道可能已经创建。用相同参数重试，如果两次都提交，
  当前数据历史中会永久保留两个同名频道，各有自己的 ID 和初始化消息。

### 5.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- 读取权限见 2.5。
- 频道不存在返回 `NOT_FOUND`。
- 无读取权限返回 `PERMISSION_DENIED`。

### 5.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

- 请求必须通过 `principal + token` 认证，且 `principal` 必须大于 0。
- 当前不支持分页。
- 先按读取权限过滤，再按请求的筛选条件过滤。
- `CHANNEL_FILTER_ALL` 返回全部可见频道。
- `CHANNEL_FILTER_JOINED` 返回调用方已加入的可见频道。
- `CHANNEL_FILTER_OWNED` 返回调用方创建的可见频道。
- 无效的筛选条件返回 `INVALID_ARGUMENT`。

### 5.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

- 请求必须通过 `principal + token` 认证，操作者和目标成员的身份编号都必须大于 0。
- 操作者必须是频道创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- 频道不存在返回 `NOT_FOUND`。
- 目标已是成员时返回 `ALREADY_EXISTS`，即使频道已达到成员总数上限也一样，不产生系统消息。
- 新增成员须满足[第 5 节总则](#5-channelservice)的成员总数上限。
- 实际添加成员时，在该频道内写入一条成员加入系统消息。消息格式见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  原子提交规则见 2.8 节。

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- 请求必须通过 `principal + token` 认证，操作者和目标成员的身份编号都必须大于 0。
- 操作者必须是频道创建者。
- `channel_id=0` 返回 `PERMISSION_DENIED`。
- 频道不存在返回 `NOT_FOUND`。
- 不允许移除创建者本人。
- 如果目标本来就不是成员，仍返回成功，不产生系统消息。
- 实际移除成员时，在该频道内写入一条成员移除系统消息。消息格式见[系统协议](SYSTEM_PROTOCOL_cn.md)，
  原子提交规则见 2.8 节。
- 成员被移出受保护或私有频道后，再用尚未使用的 UUID 发布消息会收到 `PERMISSION_DENIED`；
  公开频道的写权限不受成员列表影响。系统消息不绕过读取权限，见 2.5 节。

## 6. AdminService

AdminService 定义在 [`proto/openevent/sdk/proto/admin.proto`](../proto/openevent/sdk/proto/admin.proto)，只运行在独立管理端口，不要求调用者提供
业务认证凭据。AddToken 和 DeleteToken 的 principal/token 字段指定被管理的凭据，不用于认证管理调用者。
部署时必须通过网络隔离或外部授权保护管理端口。

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 6.1 AddToken 和 DeleteToken

- 每组凭据由身份编号和 token 组成。同一串 token 可以分别对不同的身份有效。
- `AddTokenRequest.principal` 必须大于 0，否则返回 `INVALID_ARGUMENT`。身份编号由调用方分配，无需先注册。
  成功时，`AddTokenResponse.token` 会返回一个非空 token，与请求中的身份编号一起使用。同一身份可以拥有多个 token。
- `DeleteTokenRequest.principal` 必须大于 0，`token` 必须非空；否则返回 `INVALID_ARGUMENT`。删除只影响
  指定组合，不存在也成功。token 按原字符串精确匹配，调用方无需解析其格式。

### 6.2 ListMessages

```protobuf
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

- 返回所有频道的消息，不按频道权限或接收方过滤。
- 从 `from_seq` 指定的序号（含）开始读取。填 0 表示从序号 0 开始，每页受 `limit` 和消息总字节数限制。
- limit 必填且范围为 `1..1000`。
- 消息按 seq 升序返回；服务端可以因每页消息字节预算在达到 limit 前结束一页。
- 单条消息超过每页的大小目标时会单独返回，仍须符合 gRPC 传输上限。本页放不下的消息会留到下一页。
- next_seq 是下一页建议起点。
- last_seq 是查询开始时的最大已提交消息序号。
- 起点超过尾部时返回空页和尾部下一位置；`next_seq > last_seq` 表示到达本页观察到的尾部。
- 每页是一致视图，但并发追加可使后续页的 last_seq 增大。
