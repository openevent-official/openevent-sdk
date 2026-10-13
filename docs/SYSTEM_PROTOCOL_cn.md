# OpenEvent 系统协议 system.v1

[English version](SYSTEM_PROTOCOL.md)

> 状态：设计规格
> 适用范围：OpenEvent 持久化的全局初始化、Channel 初始化和成员变更消息

本文是 `system.v1` 系统消息 payload schema 的唯一权威文档。RPC 行为和 gRPC 状态语义见
[API 契约](API_cn.md)。

## 1. 协议边界

所有系统消息均由 OpenEvent 服务端生成，外层 `EventMessage.principal=0`。客户端不得在业务请求中
使用 `principal=0`，也不能通过普通发布接口创建系统消息。

系统消息与业务消息使用同一种 `EventMessage`。普通 Channel 的 `protocol` 仍表示业务协议；读取方
先按 `principal=0` 识别系统消息，再按 Channel 的业务协议解析其他消息。SDK 原样返回系统消息，
不解析、隐藏或过滤它们。

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
`CHANNEL_FILTER_OWNED`。客户端向该 Channel 发布消息和修改成员都会被拒绝。

Channel 0 只保存第 4 节的全局初始化消息，不另写一条 Channel 初始化消息。

## 3. Envelope

所有系统消息的 payload 都是 UTF-8 JSON object，公共字段如下：

| 字段 | 规则 |
| --- | --- |
| `kind` | string，必填，取第 4–6 节定义的事件类型 |
| `data` | object，必填，各事件的字段见第 4–6 节 |
| `data.schema_version` | integer，必填，固定为 `1` |

公共外层字段如下：

| EventMessage 字段 | 规则 |
| --- | --- |
| `seq` | 全局初始化为 `0`；其他系统消息使用普通的全局递增消息序号 |
| `channel_id` | 事件所属 Channel；全局初始化属于 Channel 0 |
| `principal` | 固定为 `0` |
| `recipients` | 空 |
| `payload` | 本文定义的 JSON envelope |
| `object_keys` | 空 |
| `uuid` | 全局初始化为 `0`；其他系统消息由服务端从同一 UUID 分配器领取非零 UUID |
| `ts_ms` | 服务端在提交事件时取得的 Unix epoch 毫秒时间戳 |

payload 不重复保存外层的序号、Channel ID、发送者、UUID 或时间戳。

## 4. 全局初始化

`system.initialization` 的 `data` 只包含 `schema_version`：

```json
{
  "kind": "system.initialization",
  "data": {
    "schema_version": 1
  }
}
```

这条消息固定使用 `seq=0`、`channel_id=0` 和 `uuid=0`。服务端必须在新数据根目录中恰好创建一次，
只有持久提交后初始化才完成。该消息永久保留，重启时不得重新创建。

## 5. Channel 初始化

每个普通 Channel 创建时写入唯一一条 `system.channel_initialized`。它是该 Channel 内的第一条消息，
记录创建时的完整 Channel 状态：

```json
{
  "kind": "system.channel_initialized",
  "data": {
    "schema_version": 1,
    "name": "conversation",
    "protocol": "chat.v1",
    "description": "",
    "visibility": "private",
    "creator": 100,
    "members": [100, 200]
  }
}
```

`data` 的字段均必填：

| 字段 | 规则 |
| --- | --- |
| `schema_version` | 固定为 `1` |
| `name` | Channel 名称，string |
| `protocol` | Channel 业务协议，string |
| `description` | Channel 描述，string，允许为空 |
| `visibility` | string，取 `public`、`protected` 或 `private` |
| `creator` | 创建者 principal，正整数 |
| `members` | 创建时的成员 principal 列表；元素为正整数，去重且包含 creator，顺序无语义 |

字符串长度和成员数量限制见 [ChannelService](API_cn.md#5-channelservice)。初始成员只记录在这一条
消息中，不为每个初始成员另写 `system.member_added`。Channel 创建与初始化消息的提交规则也由
CreateChannel 定义。

## 6. 成员变更

成员加入使用 `system.member_added`，成员移除使用 `system.member_removed`。两种消息的 `data`
都只包含 `schema_version` 和 `member`；`member` 是受影响成员的 principal，必须为正整数。例如：

```json
{
  "kind": "system.member_added",
  "data": {
    "schema_version": 1,
    "member": 300
  }
}
```

这两种事件只记录成员变化，不重复完整的 Channel 属性或成员列表。实际成员变化、无变化操作及
状态与消息的原子提交规则见 [ChannelService](API_cn.md#5-channelservice)。

## 7. 序列与读取

系统消息和业务消息共用全局消息序列。只有全局初始化使用 `seq=0`；Channel 初始化、成员变更和
业务消息都占用后续序号，详见[消息序列](API_cn.md#22-消息序列)。

非零系统消息 UUID 与客户端领取的 UUID 共用分配空间，系统消息参与 `GetSeqByUuid` 查询。
全局初始化的 `GetSeqByUuid(0)` 返回 `seq=0`，详见[消息 UUID](API_cn.md#24-消息-uuid)。

系统消息沿用所在 Channel 的当前 ACL 和普通 recipient 过滤规则。它们的 `recipients` 为空，
所以 `only_my_recipient=true` 会过滤掉所有系统消息。成员被移出私有 Channel 后，不保证还能收到
该 Channel 中的移除消息；系统消息不享有读取权限例外。

历史恢复后接订阅的 Channel 发现流程见[通过消息发现 Channel](API_cn.md#38-通过消息发现-channel)。
各读取接口的起点、分页及错误规则见 [Fetch](API_cn.md#36-fetch)、[Subscribe](API_cn.md#37-subscribe)
和 [ListMessages](API_cn.md#62-listmessages)。
