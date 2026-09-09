# OpenEvent System Protocol system.v1

[中文版](SYSTEM_PROTOCOL_cn.md)

> Status: stable specification
> Scope: the initialization message persisted by OpenEvent for the system Channel

This document defines the public `system.v1` protocol for the reserved system
Channel. It follows the same payload-envelope style as the other OpenEvent
business protocols. The OpenEvent API and gRPC status semantics are defined by
the [API contract](API.md).

## 1. Protocol Boundary

The system Channel is a server-owned, read-only Channel. Clients MUST NOT
publish to it, modify it, or use `principal=0` in business requests. The server
creates exactly one system initialization message when it initializes a new
data root. That message is retained permanently.

The system message is an ordinary `EventMessage` for reading, scanning,
pagination, and subscription. Its reserved field values identify the system
record; its payload identifies the protocol kind. No protobuf field or enum is
added for this protocol.

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
`CHANNEL_FILTER_OWNED`. Publishing and member-management operations for this
Channel are denied.

## 3. Envelope

The initialization message payload is a UTF-8 JSON object:

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

Field rules:

| Field | Rule |
| --- | --- |
| `kind` | string, required, exactly `system.initialization` |
| `data` | object, required |
| `data.schema_version` | integer, required, currently `1` |
| `timestamps.event_ms` | non-negative JSON integer, required, Unix epoch milliseconds |

The payload does not contain `seq`, `channel_id`, `principal`, or `uuid`.
Those values are carried by the enclosing `EventMessage`.
`data.schema_version` versions this public system payload schema; it is not the
server's private storage-layout version.

## 4. Initialization Message

The one system initialization message MUST have these enclosing fields:

| EventMessage field | Required value |
| --- | --- |
| `seq` | `0` |
| `channel_id` | `0` |
| `principal` | `0` (reserved system principal) |
| `recipients` | empty |
| `payload` | the `system.initialization` JSON envelope defined above |
| `object_keys` | empty |
| `uuid` | `0` |
| `ts_ms` | Unix epoch milliseconds of the first initialization commit |

`ts_ms` and `timestamps.event_ms` identify the same initialization event time,
MUST be equal, and are set by the server. Initialization completes only after
the message is durably committed. The server MUST create it exactly once for a
new data root, retain it permanently, and never recreate it on restart.

The server MUST reject startup if an existing data root does not contain the
valid seq-0 initialization message and its corresponding UUID lookup mapping. A
missing, malformed, or conflicting system message is persistent storage
corruption, not an empty event log.

## 5. Reserved Principal and UUID

`principal=0` is reserved for the OpenEvent system itself. Business principals
MUST be greater than zero. Clients MUST NOT use zero as a token-binding
principal, business RPC principal, Channel creator or member, recipient, object
creator, or published-message principal.

The initialization message uses `uuid=0` and participates in normal UUID
lookup: `GetSeqByUuid(0)` returns `seq=0`. UUID 0 is otherwise not available for
client allocation or publishing.

## 6. Sequence and Reads

The event sequence always contains at least the system initialization message.
After initialization:

- `min_seq=0`;
- `max_seq=0` means that only the initialization message exists;
- application messages begin at `seq=1`;
- committed sequence values are never reused.

All message readers use inclusive sequence cursors. `from_seq=0` starts scanning
at seq 0. Each reader then applies its own normal filtering and pagination rules,
so whether seq 0 appears in a response depends on that request and reader.

Starting-sequence, pagination, and error rules for each reader are defined in
[Fetch](API.md#36-fetch), [Subscribe](API.md#37-subscribe), and
[ListMessages](API.md#62-listmessages).

The system message has no recipients, so `only_my_recipient=true` filters it
out under the normal recipient rules.

## 7. Versioning

- `system.v1` is the current system payload protocol.
- Backward-compatible additions may extend the `data` object.
- Breaking changes use a new protocol identifier, such as `system.v2`.
