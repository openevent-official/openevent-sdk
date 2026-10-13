# OpenEvent System Protocol system.v1

[中文版](SYSTEM_PROTOCOL_cn.md)

> Status: design specification
> Scope: global initialization, Channel initialization, and membership-change messages persisted by OpenEvent

This document is the sole authority for the `system.v1` system-message payload
schema. RPC behavior and gRPC status semantics are defined by the
[API contract](API.md).

## 1. Protocol Boundary

OpenEvent generates all system messages on the server, with
`EventMessage.principal=0`. Clients MUST NOT use `principal=0` in business
requests or create system messages through ordinary publishing RPCs.

System and application messages use the same `EventMessage` type. An ordinary
Channel's `protocol` still identifies its application protocol. Readers first
identify system messages by `principal=0`, then parse other messages using the
Channel's application protocol. The SDK returns system messages unchanged,
without parsing, hiding, or filtering them.

## 2. System Channel

The reserved system Channel has the following fixed shape:

```text
channel_id = 0
name       = "system"
protocol   = "system.v1"
description = ""
visibility = VISIBILITY_PROTECTED
creator    = unset
members    = []
```

All authenticated callers can read this Channel and its messages. It appears
in `CHANNEL_FILTER_ALL`, but not in `CHANNEL_FILTER_JOINED` or
`CHANNEL_FILTER_OWNED`. Client publishing and member-management operations for
this Channel are denied.

Channel 0 contains only the global initialization message in section 4. It does
not receive a separate Channel initialization message.

## 3. Envelope

Every system-message payload is a UTF-8 JSON object with these common fields:

| Field | Rule |
| --- | --- |
| `kind` | string, required; an event type defined in sections 4–6 |
| `data` | object, required; event-specific fields are defined in sections 4–6 |
| `data.schema_version` | integer, required, exactly `1` |

Common enclosing fields:

| EventMessage field | Rule |
| --- | --- |
| `seq` | `0` for global initialization; other system messages use ordinary globally increasing message sequences |
| `channel_id` | The Channel to which the event belongs; global initialization belongs to Channel 0 |
| `principal` | Exactly `0` |
| `recipients` | empty |
| `payload` | The JSON envelope defined in this document |
| `object_keys` | empty |
| `uuid` | `0` for global initialization; the server obtains a nonzero UUID from the shared UUID allocator for every other system message |
| `ts_ms` | Unix epoch milliseconds obtained by the server when committing the event |

The payload does not duplicate the enclosing sequence, Channel ID, sender,
UUID, or timestamp.

## 4. Global Initialization

The `data` object for `system.initialization` contains only `schema_version`:

```json
{
  "kind": "system.initialization",
  "data": {
    "schema_version": 1
  }
}
```

This message always uses `seq=0`, `channel_id=0`, and `uuid=0`. The server MUST
create it exactly once in a new data root. Initialization completes only after
this message is durably committed. The message is retained permanently and MUST
NOT be recreated on restart.

## 5. Channel Initialization

Creating an ordinary Channel writes exactly one `system.channel_initialized`
message. It is the first message in that Channel and records its complete state
at creation:

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

All fields in `data` are required:

| Field | Rule |
| --- | --- |
| `schema_version` | Exactly `1` |
| `name` | Channel name, string |
| `protocol` | Channel application protocol, string |
| `description` | Channel description, string; may be empty |
| `visibility` | string; `public`, `protected`, or `private` |
| `creator` | Creator principal, positive integer |
| `members` | Initial member principals; positive integers, deduplicated, including the creator; order has no meaning |

String-length and member-count limits are defined by
[ChannelService](API.md#5-channelservice). Initial members are recorded only in
this message, without a separate `system.member_added` for each initial member.
CreateChannel also defines how the Channel and its initialization message are
committed.

## 6. Membership Changes

Member additions use `system.member_added`; removals use `system.member_removed`.
For both messages, `data` contains only `schema_version` and `member`. The
`member` field is the affected member's principal and MUST be a positive integer.
For example:

```json
{
  "kind": "system.member_added",
  "data": {
    "schema_version": 1,
    "member": 300
  }
}
```

These events record only the membership change, without repeating complete
Channel properties or member lists. Actual changes, operations that leave
membership unchanged, and atomic commits of state and messages are defined by
[ChannelService](API.md#5-channelservice).

## 7. Sequence and Reads

System and application messages share the global message sequence. Only global
initialization uses `seq=0`; Channel initialization, membership changes, and
application messages occupy subsequent sequence values. See
[message sequence](API.md#22-message-sequence).

Nonzero system-message UUIDs share the allocation space used for client UUIDs,
and system messages participate in `GetSeqByUuid` lookup. For global
initialization, `GetSeqByUuid(0)` returns `seq=0`. See
[message UUIDs](API.md#24-message-uuids).

System messages follow their Channel's current ACL and ordinary recipient
filtering rules. Their `recipients` are empty, so `only_my_recipient=true`
filters out all system messages. A principal removed from a private Channel is
not guaranteed to receive its removal message. System messages have no read
permission exception.

For Channel discovery by reading history and then subscribing, see
[Discovering Channels Through Messages](API.md#38-discovering-channels-through-messages).
Starting-sequence, pagination, and error rules for each reader are defined in
[Fetch](API.md#36-fetch), [Subscribe](API.md#37-subscribe), and
[ListMessages](API.md#62-listmessages).
