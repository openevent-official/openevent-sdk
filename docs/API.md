# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.13.0
> Date: 2026-10-28

This document defines the public behavior of the business protocol
[`proto/openevent.proto`](../proto/openevent.proto) and management protocol
[`proto/admin.proto`](../proto/admin.proto).

## 1. Basic Conventions

- All APIs use `gRPC + Protocol Buffers`.
- A valid initialized server always contains the permanent global initialization
  message at `seq=0`. Subsequent application messages and Channel system messages
  share the global message sequence starting at `seq=1`.
- Each application `EventMessage` contains `ts_ms`, the Unix millisecond
  timestamp when the server received the publish request.
- Application messages use nonzero UUIDs allocated by `AllocateUuids`. The server
  obtains nonzero UUIDs for Channel system messages from the same allocator.
  Only the global initialization message uses `uuid=0`.
- Application `payload` is stored as opaque bytes. Identification, fields, and
  payload schemas for all server-generated system messages are defined in the
  [system protocol](SYSTEM_PROTOCOL.md). Readers distinguish system messages
  before parsing application messages according to the Channel's application protocol.
- Business principals must be greater than zero. `principal=0` is reserved for
  server-generated system behavior. Zero in a business or token-management request
  principal, Channel creator/member, recipient, or object-creator position returns
  `INVALID_ARGUMENT`.
- OpenEvent permanently retains committed messages, Channel metadata, and immutable
  objects within the current data history. Section 1.2 defines that history's scope.

### 1.1 Services

| Service | Responsibility |
|---------|----------------|
| `EventService` | Status queries, publishing, batch fetch, and streaming subscription |
| `ObjectStorageService` | Immutable object writes, single/batch metadata queries, and partial data reads |
| `ChannelService` | Channel creation, queries, listing, and member management |
| `AdminService` | Token management and administrative message queries |

### 1.2 Data History and Backup Restoration

Monotonic allocation and non-reuse of message `seq`, UUIDs, Channel IDs, and
object IDs, permanent retention of messages and objects, and permanent validity
of ObjectKeys are all scoped to the current data history. Normal restart or crash
recovery using the complete data directory preserves that history.

Restoring an older backup switches to a different data history rather than
continuing the history that existed before restoration. Restored state and
allocation watermarks come from the backup. Changes made in the previous history
after that backup are absent, and their sequence numbers and IDs may be allocated again.
Messages, Channels, objects, tokens, and ObjectKeys present in the restored backup
remain usable according to the restored state and this contract; restoration
does not additionally revoke those credentials.

Reading progress, subscription cursors, and pending UUID retry state from the
previous history must not be carried straight into the restored history.
Resource references are resolved within the restored data history. Current
interfaces carry no data history identifier and do not automatically recognize
or convert cursors, retry state, or references across histories.

## 2. Common Semantics

### 2.1 gRPC Status Codes

| Status | Meaning |
|--------|---------|
| `UNAUTHENTICATED` | the request's `principal + token` pair is invalid |
| `PERMISSION_DENIED` | ACL check failed or the operation is not allowed |
| `NOT_FOUND` | Channel does not exist, or an object capability is absent or invalid |
| `ALREADY_EXISTS` | Resource already exists, such as an existing member or a UUID already used by a message |
| `INVALID_ARGUMENT` | Missing/invalid fields or invalid recipients |
| `RESOURCE_EXHAUSTED` | payload or total Channel membership exceeds its limit, or object-file storage has insufficient space/quota/inodes |
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
- `seq=0` is the permanent global initialization message. `max_seq=0` means
  that only this message exists.
- Committed values are not reused.
- Application messages and Channel system messages both advance seq from
  `max_seq + 1`. Readers continue with the cursor returned by the server.
  Channel creation and actual membership changes also advance `max_seq` and can
  cause a concurrent Publish seq CAS to return `ABORTED`.
- `GetStatus`, `Fetch`, and `Subscribe` all use this sequence.

### 2.3 Payload

- `Publish` and `PublishAutoSeq` accept payload, which may be empty.
- The deployment's `max_payload_bytes` setting limits only application publish
  request payloads, not server-generated system messages.
- Oversized payload returns `RESOURCE_EXHAUSTED` and creates no message.
- Fetch, Subscribe, and administrative ListMessages return complete historical
  payloads even after the write limit is lowered. Page budgets determine page boundaries.

### 2.4 Message UUIDs

- UUID 0 is reserved for the global initialization message.
- Every Publish and PublishAutoSeq request must carry one nonzero UUID returned by AllocateUuids.
- Channel system messages share the UUID allocator with AllocateUuids. The server
  allocates and consumes their UUIDs in the same commit as the corresponding
  Channel mutation. System messages do not consume UUIDs allocated to clients.
- A Channel mutation that does not commit or does not actually change state
  neither allocates nor consumes a UUID and does not advance the message sequence.
  Other concurrent operations may still advance the watermarks.
- The global initialization message participates in normal UUID lookup, and
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
[`SYSTEM_PROTOCOL.md`](SYSTEM_PROTOCOL.md). System messages in an ordinary
Channel follow that Channel's current read permission, without special delivery
permissions. A caller removed from a private Channel is not guaranteed to read
the removal event.

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
  by that removed member using an unconsumed UUID returns `PERMISSION_DENIED`;
  public Channel writes are not controlled by membership. If DeleteToken commits first, a later publish using
  that deleted pair returns `UNAUTHENTICATED`; the same token string under another
  principal is unaffected. A message committed first remains valid.
- WriteObject may interleave with other operations while processing. Only a fully
  and durably committed object is visible to reads and message references; that
  complete commit is the write's ordering point.
- CreateChannel, an actual member addition, and an actual member removal each
  commit atomically with one corresponding Channel system message. Channel
  metadata, the system message, UUID allocation and consumption, and `max_seq`
  either all commit or none do. The initialization message is the new Channel's
  first message, preceding all application and membership-change messages in it.
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
  only the global initialization message exists.

Clients may use `max_seq+1` as the next Publish candidate. A concurrent application
publish or Channel system message can consume it first, causing `ABORTED`.

### 3.2 AllocateUuids

```protobuf
rpc AllocateUuids(AllocateUuidsRequest) returns (AllocateUuidsResponse);
```

The request contains only `count`, which must be in `1..1024`. The response contains exactly `count` globally
monotonically increasing, contiguous UUIDs in increasing order. Allocation is unauthenticated and does not create a
message. A successful call durably advances the allocation watermark; allocated UUIDs are never reused, including
when no message consumes them. Allocation alone does not make a UUID queryable through GetSeqByUuid.
Channel system messages advance the same allocation watermark, so UUIDs from consecutive calls need not be adjacent.

### 3.3 GetSeqByUuid

```protobuf
rpc GetSeqByUuid(GetSeqByUuidRequest) returns (GetSeqByUuidResponse);
```

The request contains only `uuid`; it carries no `principal` or `token`. This unauthenticated read does not change
allocation or message state. UUID 0 returns `seq=0` for the global initialization message. For other UUIDs, the
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

With no channels specified, later batches automatically include newly created
readable Channels and Channels for which the caller gains read permission,
without rebuilding the subscription. Previously filtered and consumed messages
are not replayed. A permission change makes later batches filter out private
Channels that are no longer readable; it does not end the whole stream.
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
waits for or pushes an OpenEvent message. The fixed metadata entry
`openevent-subscription: accepted` is the sole confirmation that the subscription
has been accepted. The server sends this marker even when all current messages
are filtered out. Empty metadata, unrelated entries, or any other value for this
key do not confirm acceptance. The server must not send the marker when rejecting
a subscription before acceptance.

A Subscribe stream stays open until interrupted. The public gRPC endpoint sends
an HTTP/2 PING after 30 seconds without transport activity and closes a connection
whose PING acknowledgement does not
arrive within 10 seconds. It accepts client PINGs no more frequent than every 20
seconds. The Python high-level client sends client PINGs every 30 seconds, closes
its connection if no acknowledgement arrives within 10 seconds, and does not set
a deadline for Subscribe; a caller using another gRPC client or a
generated stub must configure a compatible transport keepalive policy on its own
channel.

These intervals detect broken connections; they are not RPC execution deadlines.
The client chooses each RPC's deadline. The Python high-level client's
`timeout_ms` applies only to non-streaming RPCs and does not change the server's
keepalive intervals. Each peer detects connection failure and releases its own
connection; a network interruption can prevent client cancellation from reaching
the server.
An otherwise healthy connection that acknowledges PINGs does not time out a
subscription merely because there are no application messages.

When the server begins normal or fatal shutdown, it actively ends every
established Subscribe stream. Treat this like any other stream interruption:
record the last processed `seq` and use the resume rule above with Subscribe or
Fetch.

### 3.8 Discovering Channels Through Messages

Callers can discover Channels from [Channel system messages](SYSTEM_PROTOCOL.md)
while restoring message history, without an initial or periodic ListChannels
call. Initialization and ongoing subscription use the same message handling:

1. Call GetStatus and record the fixed watermark `H=max_seq`.
2. Fetch from `from_seq=0` with `channels=[]` and `only_my_recipient=false`.
   Follow `next_seq` and process only messages with `seq<=H`. History reading is
   complete only when `next_seq>H`, never merely because a page is short or empty.
3. Handle system messages according to the system protocol. The caller decides
   which readable Channels belong in its application list using protocol and
   membership information. Read permission does not imply membership. If only
   a member-added event was received and Channel attributes are missing, call
   GetChannel once with that `channel_id`; no history replay or list scan is needed.
4. After restoring history, start Subscribe with `from_seq=H+1`, `channels=[]`,
   and `only_my_recipient=false`. Messages committed during recovery are read
   from this point. Resume after interruptions according to section 3.7.

This fixes the message watermark, not ACL across RPCs. Gaining read permission
does not replay history before the cursor; callers needing that history must
Fetch the relevant Channel separately. System messages follow current read
permissions in section 2.5 and recipient filtering. Therefore discovery cannot
use `only_my_recipient=true`, and this procedure does not guarantee notification
of removal from a private Channel.

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

### 4.4 BatchGetObjectMetadata

```protobuf
rpc BatchGetObjectMetadata(BatchGetObjectMetadataRequest) returns (BatchGetObjectMetadataResponse);
```

Read metadata for multiple committed objects in one request without reading object
data. Authorization uses only the ObjectKey capabilities in `object_keys`; no
ordinary `principal + token` is required.

- `object_keys` must contain `1..1024` items. The server first checks the size of
  the entire list and that every item has a nonzero `object_id` and a nonempty
  `object_token`. Any violation returns `INVALID_ARGUMENT`.
- After input validation, the entire batch uses one database snapshot at one
  linearization point to read committed metadata and check each capability. A
  missing or uncommitted object or a token mismatch returns `NOT_FOUND`, as with
  a single-object query.
- On success, `metadata` has exactly the same length and order as the request;
  each item is an independent `ObjectMetadata` message with the same fields as
  the single-object response in section 4.2. Duplicate ObjectKeys are preserved.
  Different tokens for the same `object_id` are each checked.
- If any item fails, the entire RPC fails without partial results. After input
  validation, objects are queried in input order and the first error encountered
  is returned. Storage failures still follow section 2.1.

The fixed batch limit bounds the work and response size of one query. It does not
change the metadata limits for individual objects.

## 5. ChannelService

An ordinary Channel may contain at most 1024 distinct members, including its
creator. CreateChannel returns `RESOURCE_EXHAUSTED` if deduplication and inclusion
of the creator would exceed this total; AddMember returns the same status if a
new member would exceed it. Such a rejection neither creates nor changes a
Channel, creates no system message, allocates or consumes no UUID, and does not
advance `max_seq`. Repeated additions of existing members follow section 5.4.

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
  added automatically, appearing only once. The result must satisfy the total
  membership limit in [section 5's general rules](#5-channelservice). Names need not be globally unique.
- `channel_id=0` is the system Channel and cannot be created through this RPC.
- visibility must be public, protected, or private; unset means public.
- Success writes one initialization system message in the new Channel,
  containing the created Channel's attributes and complete initial membership.
  Initial members, including the creator, do not each produce a separate join
  event. See the [system protocol](SYSTEM_PROTOCOL.md) for the message schema
  and section 2.8 for atomic commitment with Channel state.
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
missing Channel returns `NOT_FOUND`, and an existing member returns `ALREADY_EXISTS`
without a system message, even when the Channel has reached its total membership
limit. New members must satisfy the total membership limit in
[section 5's general rules](#5-channelservice). An actual
addition writes one member-added system message in that Channel. See the
[system protocol](SYSTEM_PROTOCOL.md) for the schema and section 2.8 for atomic commitment.

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- The request must authenticate with `principal + token`; both the operator and
  target principal must be greater than zero.

Only the creator may remove members. `channel_id=0` returns `PERMISSION_DENIED`,
and a missing Channel returns `NOT_FOUND`. The creator cannot be removed. Removing a
non-member is idempotent success without a system message. An actual removal
writes one member-removed system message in that Channel. See the
[system protocol](SYSTEM_PROTOCOL.md) for the schema and section 2.8 for atomic
commitment. After a successful removal from a protected or private Channel, later
publishes by the removed principal using an unconsumed UUID return `PERMISSION_DENIED`.
Removing a member from a public Channel does not change that principal's write permission. System
messages do not bypass read permissions; see section 2.5.

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
