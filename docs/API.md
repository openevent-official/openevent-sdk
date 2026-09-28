# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.11.0
> Date: 2026-09-28

This document defines the public behavior of the business protocol
[`proto/openevent.proto`](../proto/openevent.proto) and management protocol
[`proto/admin.proto`](../proto/admin.proto).

## 1. Basic Conventions

- All APIs use `gRPC + Protocol Buffers`.
- A valid initialized server always contains the permanent system initialization
  message at `seq=0`; application messages start at `seq=1`.
- Each application `EventMessage` contains `ts_ms`, the Unix millisecond
  timestamp when the server received the publish request. For seq 0 it is the
  initialization event time defined by `system.v1`.
- Each application `EventMessage` contains a nonzero `uuid` allocated by
  `AllocateUuids`. The system initialization message is the sole exception and
  uses `uuid=0` and `principal=0`.
- Application `payload` is stored as opaque bytes. The server-generated seq-0 system payload follows the
  separate `system.v1` protocol.
- Business principals must be greater than zero. `principal=0` is reserved for
  server-generated system behavior. Zero in a business or token-management request
  principal, Channel creator/member, recipient, or object-creator position returns
  `INVALID_ARGUMENT`.
- OpenEvent permanently retains committed messages, Channel metadata, and immutable objects.

### 1.1 Services

| Service | Responsibility |
|---------|----------------|
| `EventService` | Status queries, publishing, batch fetch, and streaming subscription |
| `ObjectStorageService` | Immutable object writes, metadata queries, and partial data reads |
| `ChannelService` | Channel creation, queries, listing, and member management |
| `AdminService` | Token management and administrative message queries |

## 2. Common Semantics

### 2.1 gRPC Status Codes

| Status | Meaning |
|--------|---------|
| `UNAUTHENTICATED` | the request's `principal + token` pair is invalid |
| `PERMISSION_DENIED` | ACL check failed or the operation is not allowed |
| `NOT_FOUND` | Channel does not exist, or an object capability is absent or invalid |
| `ALREADY_EXISTS` | Resource already exists, such as an existing member or a UUID already used by a message |
| `INVALID_ARGUMENT` | Missing/invalid fields or invalid recipients |
| `RESOURCE_EXHAUSTED` | payload exceeds its limit or object-file storage has insufficient space/quota/inodes |
| `OUT_OF_RANGE` | ReadObject offset is greater than the object size, or Subscribe starts beyond `max_seq + 1` |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | The call was cancelled; a write result may be uncertain |
| `DEADLINE_EXCEEDED` | The call exceeded its deadline; a write result may be uncertain |
| `UNAVAILABLE` | Service or dependency is unavailable |
| `DATA_LOSS` | Persistent storage is corrupt; the server exits |
| `INTERNAL` | Unexpected error |

The object-storage part of `RESOURCE_EXHAUSTED` covers only space, quota, or inode
exhaustion during object-file writes, syncs, or cleanup. Metadata storage write
failures return `UNAVAILABLE` and trigger server shutdown.

A fatal storage failure stops new calls and causes a nonzero process exit. The
triggering RPC makes a best effort to return its original error: `DATA_LOSS` for
corruption or `UNAVAILABLE` for other fatal storage failures. Ordinary RPCs that
have begun execution may finish; active subscriptions are ended.

Handle errors by gRPC status code.

### 2.2 Message Sequence

- `EventMessage.seq` is a globally monotonically increasing message sequence.
- `seq=0` is the permanent system initialization message. `max_seq=0` means
  that only this message exists.
- Committed values are not reused.
- Application message seq values advance from `max_seq + 1`. Readers continue
  with the cursor returned by the server.
- `GetStatus`, `Fetch`, and `Subscribe` all use this sequence.

### 2.3 Payload

- `Publish` and `PublishAutoSeq` accept payload, which may be empty.
- The deployment's `max_payload_bytes` setting limits only publish request payloads.
- Oversized payload returns `RESOURCE_EXHAUSTED` and creates no message.
- Fetch, Subscribe, and administrative ListMessages return complete historical
  payloads even after the write limit is lowered. Page budgets determine page boundaries.

### 2.4 Message UUIDs

- UUID 0 is reserved for the system initialization message.
- Every Publish and PublishAutoSeq request must carry one nonzero UUID returned by AllocateUuids.
- The initialization message participates in normal UUID lookup, and
  `GetSeqByUuid(0)` returns `seq=0`. Clients cannot allocate or publish with
  UUID 0. For client publishes, a UUID not allocated by the server returns
  `INVALID_ARGUMENT`; a UUID already consumed by a committed message returns
  `ALREADY_EXISTS`.
- After authentication, a consumed UUID returns `ALREADY_EXISTS` before payload, ObjectKey, Channel, ACL, or
  Publish seq-CAS validation. Duplicate handling does not compare any of those request fields; a UUID can consume
  at most one message.
- For Python usage, see the [usage guide](USAGE.md#publish-messages).
- The allocation and lookup RPCs are defined in sections 3.2 and 3.3. The complete recovery procedure for an
  uncertain publish result is defined only in section 2.9.

### 2.5 Channel ACL

| Visibility | Read Permission | Write Permission |
|------------|-----------------|------------------|
| `VISIBILITY_PUBLIC` | All authenticated callers | All authenticated callers |
| `VISIBILITY_PROTECTED` | All authenticated callers | Channel members |
| `VISIBILITY_PRIVATE` | Channel members | Channel members |

System Channel `channel_id=0` is read-only for all authenticated callers. Its
fixed fields, list membership, and initialization message are defined in
[`SYSTEM_PROTOCOL.md`](SYSTEM_PROTOCOL.md).

### 2.6 Recipients

- recipients is the targeted principal list; an empty list means no targets.
- Every recipient must be a Channel member when publishing, otherwise the server
  returns `INVALID_ARGUMENT`.
- With `only_my_recipient=true`, only messages targeting the current principal
  are returned or pushed; messages with empty recipients do not match.

### 2.7 Object Capabilities and Message References

- `ObjectKey` contains `object_id + object_token` and is a permanent bearer
  capability. Anyone holding both values can read the object's metadata and data
  without a principal token or Channel permission.
- Object capabilities can be transferred and remain valid permanently. Keep `object_token` secret.
- Publish and PublishAutoSeq accept at most 1024 `object_keys`. The server does
  not deduplicate the list and stores it in request order.
- Every reference must have a nonzero object ID and a nonempty token. Malformed
  references return `INVALID_ARGUMENT`; a missing object, uncommitted object, or
  token mismatch returns `NOT_FOUND`.
- If any reference is invalid, the complete message is rejected and `max_seq`
  does not advance.
- Fetch, Subscribe, and administrative ListMessages return complete ObjectKeys.
  Any caller allowed to read a message therefore receives every capability in it.

### 2.8 Read/Write Linearization and Commit Ordering

- All mutations and non-streaming stateful business/administrative reads have one
  server-side global order. Writes commit in that order, while non-streaming reads
  acquire a consistent snapshot at their position. Subscribe is excluded.
- Publish evaluates the `principal + token` pair and Channel write permission against the latest
  committed state preceding the message commit. No token or Channel mutation can
  intervene between those checks and the commit.
- If RemoveMember commits first on a protected or private Channel, a later publish
  by that removed member returns `PERMISSION_DENIED`; public Channel writes are not
  controlled by membership. If DeleteToken commits first, a later publish using
  that deleted pair returns `UNAUTHENTICATED`; the same token string under another
  principal is unaffected. A message committed first remains valid.
- WriteObject may interleave with other operations while processing. Only a fully
  and durably committed object is visible to reads and message references; that
  complete commit is the write's ordering point.
- A write returns success only after it has been synchronously and durably
  persisted. Success means the mutation satisfies the server durability contract.
- A non-streaming read issued after a write RPC returned success must observe the
  complete result of that write. Overlapping mutations and non-streaming reads
  observe the complete state before or after a commit according to the server
  global order, never a partial commit.
- Subscribe batch visibility and starting-sequence rules are defined in section
  3.7. For a fixed message watermark, record GetStatus.max_seq, then use Fetch and
  process only messages through that watermark. This does not freeze permissions
  across RPCs.

### 2.9 Handling an Uncertain Publish Result

For an uncertain publish result, retain the original UUID and retry Publish or
PublishAutoSeq with it. If the retry returns `ALREADY_EXISTS`, use `GetSeqByUuid`
to obtain the committed message's seq. A new UUID starts a new publish attempt.

The following table describes the result of this RPC from the server's response:

| Status | Commit result |
|--------|------------------|
| `OK` | Synchronously persisted and committed |
| `UNAUTHENTICATED` | Not committed |
| `PERMISSION_DENIED` | Not committed |
| `NOT_FOUND` | Not committed |
| `INVALID_ARGUMENT` | Not committed |
| `RESOURCE_EXHAUSTED` | Not committed |
| `ABORTED` | Not committed; Publish seq-CAS conflict |
| `ALREADY_EXISTS` | Already committed by an earlier call using this UUID |
| Any other non-`OK` status, or no gRPC status | Uncertain outcome |

Cancellation, timeout, and connection loss are handled as uncertain outcomes.

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- The request must provide a valid `principal` and `token`; `principal=0`
  returns `INVALID_ARGUMENT`, and authentication failures return
  `UNAUTHENTICATED`.
- The response contains only `max_seq`: highest committed message seq; 0 when
  only the system initialization message exists.

Clients may use `max_seq+1` as the next Publish candidate. A concurrent publish
can consume it first, causing `ABORTED`.

### 3.2 AllocateUuids

```protobuf
rpc AllocateUuids(AllocateUuidsRequest) returns (AllocateUuidsResponse);
```

The request contains only `count`, which must be in `1..1024`. The response contains exactly `count` globally
monotonically increasing, contiguous UUIDs in increasing order. Allocation is unauthenticated and does not create a
message. A successful call durably advances the allocation watermark; allocated UUIDs are never reused, including
when no message consumes them. Allocation alone does not make a UUID queryable through GetSeqByUuid.

### 3.3 GetSeqByUuid

```protobuf
rpc GetSeqByUuid(GetSeqByUuidRequest) returns (GetSeqByUuidResponse);
```

The request contains only `uuid`; it carries no `principal` or `token`. This unauthenticated read does not change
allocation or message state. UUID 0 returns `seq=0` for the system initialization message. For other UUIDs, the
server returns `NOT_FOUND` unless the UUID has already been consumed by a committed message, including when the UUID
was never allocated or was allocated but has not been used. On success, `seq` is the committed message sequence that
consumed the UUID.

### 3.4 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

Publishes with a client-specified CAS seq. Success requires valid authentication,
an allowed payload size, `seq == max_seq+1`, an existing writable Channel, valid
recipients, and valid object capabilities.

UUID validation, duplicate precedence, and recovery rules are defined in sections 2.4 and 2.9.

| Condition | Status |
|-----------|--------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Missing Channel | `NOT_FOUND` |
| No write permission | `PERMISSION_DENIED` |
| Non-member recipient | `INVALID_ARGUMENT` |
| More than 1024 ObjectKeys, object ID 0, or empty object token | `INVALID_ARGUMENT` |
| Missing/uncommitted object or object token mismatch | `NOT_FOUND` |

Success returns an empty response. The server sets `ts_ms` before the message is
made available to authorized readers.

### 3.5 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

Uses the same validation as Publish, except the server allocates seq. UUID validation, duplicate precedence, and
recovery rules are defined in sections 2.4 and 2.9. The response contains the committed message seq.

### 3.6 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

- The `principal + token` pair must be valid.
- limit must be in `1..1000`.
- Empty channels means no Channel filter. When non-empty, every listed Channel
  must exist and be readable by the caller; otherwise the whole request returns
  an error: `NOT_FOUND` for a missing Channel and `PERMISSION_DENIED` for an
  unreadable Channel. After validation, normal ACL still applies to the selected
  Channels.
- `only_my_recipient=true` enables recipient filtering.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Scan from seq 0; seq 0 is returned only when the filters allow it |
| `> max_seq` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `1..max_seq` | Scan visible messages from this seq |

The response contains at most limit messages matching ACL, Channel, and recipient
filters. A server may end a page early because of scan or response-size budgets,
so a short page, or an empty page
after scanning invisible messages, does not imply end of stream. A single
returnable message may exceed the page's soft size budget but remains subject to
the gRPC transport limit; the soft budget never causes an empty-page loop at the
same cursor. `next_seq` is the suggested continuation point and `last_seq` is the
highest committed seq observed by this response. Continue while
`next_seq <= last_seq`; a larger next_seq means the observed tail was reached.
Filtering never moves next_seq backward.

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

Pushes messages visible to the caller in global seq order. The `principal + token`
pair must be valid. Empty `channels` disables Channel filtering.
When non-empty, every listed Channel must exist and
be readable by the caller; otherwise the stream returns an error: `NOT_FOUND` for
a missing Channel and `PERMISSION_DENIED` for an unreadable Channel. After
validation, only readable messages from the listed Channel IDs are pushed.
Normal Channel ACL and `only_my_recipient` filtering still apply. Filtered
records do not produce responses but are consumed by the server's scan. If an
explicitly listed Channel becomes unreadable while the stream is running, a
later batch returns `PERMISSION_DENIED` and ends the stream. Subscribe is outside
the non-streaming read/write linearization order and has no linearization point
relative to concurrent RPCs. Each batch
uses a consistent read view covering commits completed before that batch began;
those commits become visible by the next successful 100 ms view refresh.

With no channels specified, a permission change makes later batches filter out
private Channels that are no longer readable; it does not end the whole stream.
Removing a member from a protected Channel does not change read permission.
Deleting the subscription's `principal + token` pair makes the next batch return
`UNAUTHENTICATED` and end the stream.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Scan from seq 0; seq 0 is pushed only when the filters allow it |
| `1..max_seq` | Push visible messages from this seq |
| `max_seq + 1` | Accept the subscription and wait for that sequence to be committed |
| `> max_seq + 1` | `OUT_OF_RANGE` |

`SubscribeResponse.message` contains a message. The subscription stays open at
the tail and waits for new messages. After cancellation or disconnect, resume
Subscribe or Fetch from the last processed `seq + 1`; alternatively, subscribe
inclusively from the last processed seq and filter the replay.

After authentication, explicit Channel validation, and starting-sequence
validation succeed, the server sends gRPC initial response metadata before it
waits for or pushes an OpenEvent message. This metadata confirms acceptance
even when all current messages are filtered out.

A Subscribe stream stays open until interrupted. The public gRPC endpoint sends
an HTTP/2 PING after 30 seconds without transport activity and closes a connection
whose PING acknowledgement does not
arrive within 10 seconds. It accepts client PINGs no more frequent than every 20
seconds. The Python high-level client sends client PINGs every 30 seconds and
does not set a deadline for Subscribe; a caller using another gRPC client or a
generated stub must configure a compatible transport keepalive policy on its own
channel.

When the server begins normal or fatal shutdown, it actively ends every
established Subscribe stream. Treat this like any other stream interruption:
record the last processed `seq` and use the resume rule above with Subscribe or
Fetch.

## 4. ObjectStorageService

ObjectStorageService runs on the business endpoint. Object writes use normal
`principal + token` authentication. Metadata and data reads use only the
ObjectKey capability.

### 4.1 WriteObject

```protobuf
rpc WriteObject(WriteObjectRequest) returns (WriteObjectResponse);
```

- `name` and `type` are required and each must encode to at most 255 UTF-8 bytes.
- `description` may be empty and must encode to at most 4096 UTF-8 bytes.
- `data` must contain `1..4,194,304` bytes. The 4 MiB limit is fixed and is not
  affected by the message payload configuration.
- The server stores these strings as opaque metadata and stores object data as supplied.
- Success returns a nonzero globally increasing `object_id` and an opaque
  `object_token`. IDs can contain gaps and are never reused. Objects are immutable.
- Success is returned only after object data and metadata are durably committed.
  If the result is uncertain after interruption, retrying may create another object.

Invalid metadata or data size returns `INVALID_ARGUMENT`. Invalid principal/token
returns `UNAUTHENTICATED`. Object-file storage space, quota, or inode exhaustion
returns `RESOURCE_EXHAUSTED`; other storage availability failures return
`UNAVAILABLE`. Fatal storage failures follow section 2.1.

### 4.2 GetObjectMetadata

```protobuf
rpc GetObjectMetadata(GetObjectMetadataRequest) returns (GetObjectMetadataResponse);
```

Returns the committed object's `name`, `type`, `description`, and total `nbytes`.

### 4.3 ReadObject

```protobuf
rpc ReadObject(ReadObjectRequest) returns (ReadObjectResponse);
```

`nbytes` must be positive. The requested range follows these rules:

| Condition | Result |
|-----------|--------|
| `offset < object size` | Return `min(nbytes, object size - offset)` bytes |
| `offset == object size` | Success with empty `data` |
| `offset > object size` | `OUT_OF_RANGE` |
| `nbytes == 0` | `INVALID_ARGUMENT` |

`nbytes` may be any positive uint64; requests larger than the remaining content
return only that remaining content.

For GetObjectMetadata and ReadObject, `object_id=0` or an empty object token is
`INVALID_ARGUMENT`. A missing/uncommitted object and every token mismatch return
the same `NOT_FOUND` result. Tokens are treated as opaque strings.
ReadObject returns `DATA_LOSS` if committed data is missing, not a regular file,
or has the wrong size, and the server exits as
described in section 2.1.

## 5. ChannelService

### 5.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- The request must authenticate with `principal + token`, and `principal` must
  be greater than zero.
- `name` is required and must be at most 255 UTF-8 bytes; `protocol` may be
  empty and is at most 255 bytes when present; `description` may be empty and
  is at most 4096 UTF-8 bytes.
- The request parameter `members` contains at most 1024 entries, each greater than
  zero. After parameter validation, members are deduplicated and the creator is
  added automatically, appearing only once. Names need not be globally unique.
- `channel_id=0` is the system Channel and cannot be created through this RPC.
- visibility must be public, protected, or private; unset means public.
- Success returns the created ChannelInfo.

### 5.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

- The request must authenticate with `principal + token`, and `principal` must
  be greater than zero.

Read permission follows section 2.5. Missing Channel returns `NOT_FOUND`; denied
access returns `PERMISSION_DENIED`.

### 5.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

- The request must authenticate with `principal + token`, and `principal` must
  be greater than zero.

Pagination is not currently supported. The server filters by read permission and
then applies ALL, JOINED, or OWNED. Invalid filter returns `INVALID_ARGUMENT`.

### 5.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

- The request must authenticate with `principal + token`; both the operator and
  target principal must be greater than zero.

Only the creator may add members. `channel_id=0` returns `PERMISSION_DENIED`, a
missing Channel returns `NOT_FOUND`, and an existing member returns `ALREADY_EXISTS`.

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- The request must authenticate with `principal + token`; both the operator and
  target principal must be greater than zero.

Only the creator may remove members. `channel_id=0` returns `PERMISSION_DENIED`,
and a missing Channel returns `NOT_FOUND`. The creator cannot be removed. Removing a
non-member is idempotent success. After a successful removal from a protected or
private Channel, later writes by the removed principal return `PERMISSION_DENIED`.
Removing a member from a public Channel does not change that principal's write
permission.

## 6. AdminService

AdminService is defined in [`proto/admin.proto`](../proto/admin.proto), runs only
on the admin endpoint, and requires no business credentials from its caller.
The principal/token fields of AddToken and DeleteToken identify the managed
credential, not the admin caller. Deployments must protect the endpoint with
network isolation or external authorization.

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 6.1 AddToken and DeleteToken

- A credential is identified by its `(principal, token)` pair. The same token string
  may be independently valid under different principals.
- `AddTokenRequest.principal` must be greater than zero; otherwise it returns
  `INVALID_ARGUMENT`. Principals are assigned by the caller and do not need to be
  created or registered first. Success activates the pair of this principal and
  the returned nonempty token, provided in `AddTokenResponse.token`.
  One principal may have multiple tokens.
- `DeleteTokenRequest.principal` must be greater than zero, and `token` must be
  non-empty; otherwise it returns `INVALID_ARGUMENT`. Deletion affects only that
  exact pair and succeeds even when the pair is absent. Tokens are matched exactly
  as opaque strings.

### 6.2 ListMessages

```protobuf
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

- Does not apply Channel ACL or recipient filtering.
- from_seq is inclusive; 0 starts scanning at seq 0. The returned page follows
  this section's limit and response-size budget rules.
- limit is required in `1..1000`.
- Messages are returned in ascending seq order.
- A response-size budget may end a page before limit. One message may exceed the
  soft page budget but remains subject to the gRPC transport limit, and a message
  deferred by the budget is not skipped by next_seq.
- next_seq is the suggested continuation point.
- last_seq is the highest committed seq observed when the page starts.
- A cursor beyond the tail returns an empty page and the tail's next position.
  `next_seq > last_seq` means the page has reached its observed tail.
- Concurrent appends may increase last_seq on later pages.

## 7. Compatibility Guidance

- Version 0.11.0 is incompatible with older protocols and data directories.
  Update the server and SDK together and use a new data directory.
- Put payload schema rules in the business protocol named by Channel protocol.
