# OpenEvent 系统协议 system.v1

[English version](SYSTEM_PROTOCOL.md)

> 状态：稳定规格
> 适用范围：OpenEvent 系统 Channel 上持久化的初始化消息

本文定义保留系统 Channel 使用的公开 `system.v1` 协议。payload envelope
格式参照其他 OpenEvent 业务协议。OpenEvent API 和 gRPC 状态语义见
[API 契约](API_cn.md)。

## 1. 协议边界

系统 Channel 由服务端拥有且只读。客户端不得向其发布消息、修改它，或在业务请求中使用
`principal=0`。服务端在初始化新数据根目录时恰好创建一条系统初始化消息，该消息永久保留。

系统消息在读取、扫描、分页和订阅时都是普通 `EventMessage`。保留字段值用于识别系统记录，
payload 用于识别协议类型。本协议不新增 protobuf 字段或枚举。

## 2. 系统 Channel

保留的系统 Channel 固定为：

```text
channel_id = 0
name       = "system"
protocol   = "system.v1"
description = ""
visibility = VISIBILITY_PROTECTED
creator    = 未设置
members    = []
```

所有已认证调用方都可以读取该 Channel 及其消息。它出现在
`CHANNEL_FILTER_ALL`，但不出现在 `CHANNEL_FILTER_JOINED` 或
`CHANNEL_FILTER_OWNED`。向该 Channel 发布消息和修改成员都会被拒绝。

## 3. Envelope

初始化消息的 payload 是 UTF-8 JSON object：

```json
{
  "kind": "system.initialization",
  "data": {
    "schema_version": 1
  },
  "timestamps": {
    "event_ms": 1710000000000
  }
}
```

字段规则：

| 字段 | 规则 |
| --- | --- |
| `kind` | string，必填，固定为 `system.initialization` |
| `data` | object，必填 |
| `data.schema_version` | integer，必填，当前为 `1` |
| `timestamps.event_ms` | 非负 JSON integer，必填，Unix epoch 毫秒时间戳 |

payload 不包含 `seq`、`channel_id`、`principal` 或 `uuid`；这些值由外层
`EventMessage` 承载。
`data.schema_version` 表示公开系统 payload schema 的版本，不是服务端私有存储布局版本。

## 4. 初始化消息

唯一一条系统初始化消息的外层字段必须为：

| EventMessage 字段 | 必须值 |
| --- | --- |
| `seq` | `0` |
| `channel_id` | `0` |
| `principal` | `0`（系统 principal，保留值） |
| `recipients` | 空 |
| `payload` | 上述 `system.initialization` JSON envelope |
| `object_keys` | 空 |
| `uuid` | `0` |
| `ts_ms` | 首次初始化提交时的 Unix epoch 毫秒时间戳 |

`ts_ms` 和 `timestamps.event_ms` 表示同一个初始化事件时间，必须相等并由服务端设置。只有该消息持久提交后
初始化才完成。服务端必须在新数据根目录中恰好创建一次、永久保留，并且重启时不得重新创建。

已有数据根目录如果缺少合法的 seq=0 初始化消息或对应 UUID 查询映射，服务端必须拒绝启动。系统消息
缺失、格式错误或冲突属于持久化存储损坏，不属于空消息序列。

## 5. 保留 Principal 与 UUID

`principal=0` 保留给 OpenEvent 系统本身。业务 principal 必须大于 0。客户端不得把 0 用作 token binding
principal、业务 RPC principal、Channel 创建者或成员、recipient、对象创建者或发布消息 principal。

初始化消息使用 `uuid=0` 并参与普通 UUID 查询；`GetSeqByUuid(0)` 返回 `seq=0`。除此之外，UUID 0
不可由客户端分配或用于发布。

## 6. 序列与读取

消息序列始终至少包含系统初始化消息。初始化完成后：

- `min_seq=0`；
- `max_seq=0` 表示当前只有初始化消息；
- 业务消息从 `seq=1` 开始；
- 已提交 seq 永不复用。

所有消息读取器使用包含起点的 seq 游标。`from_seq=0` 从 seq 0 开始扫描。每个读取器再按自己的过滤和分页
规则处理，因此 seq 0 是否出现在响应中取决于具体请求和读取器。

各读取接口的起点、分页及错误规则见 [Fetch](API_cn.md#36-fetch)、[Subscribe](API_cn.md#37-subscribe)
和 [ListMessages](API_cn.md#62-listmessages)。

系统消息没有 recipients，因此 `only_my_recipient=true` 按普通 recipient 规则会将其过滤掉。

## 7. 版本策略

- `system.v1` 是当前系统 payload 协议。
- 向后兼容的增强可以扩展 `data` object。
- 破坏性变更使用新的 protocol 标识，例如 `system.v2`。
