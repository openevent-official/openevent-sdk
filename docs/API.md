# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.13.0
> Date: 2026-10-28

This document defines the public behavior of the business protocol
[`proto/openevent/sdk/proto/openevent.proto`](../proto/openevent/sdk/proto/openevent.proto) and management protocol
[`proto/openevent/sdk/proto/admin.proto`](../proto/openevent/sdk/proto/admin.proto).
When generating bindings, pass the SDK's `proto/` directory as the protobuf include root.
In your own schemas, import `openevent/sdk/proto/openevent.proto` or `openevent/sdk/proto/admin.proto`.

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
| `AdminService` | Token management and administrative queries over all messages |

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
| `INVALID_ARGUMENT` | Missing or invalid fields, or recipients that do not meet Channel membership requirements |
| `RESOURCE_EXHAUSTED` | payload or total Channel membership exceeds its limit, or object-file storage has insufficient space/quota/inodes |
| `OUT_OF_RANGE` | ReadObject offset is greater than the object size, or Subscribe starts beyond `max_seq + 1` |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | The call was cancelled; a write result may be uncertain |
| `DEADLINE_EXCEEDED` | The call exceeded its deadline; a write result may be uncertain |
| `UNAVAILABLE` | Service or dependency is unavailable |
| `DATA_LOSS` | Persistent storage is corrupt; the server exits |
| `INTERNAL` | Unexpected error |

Object storage returns `RESOURCE_EXHAUSTED` only when object-file writes, syncs, or cleanup
exhaust space, quota, or inodes.

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
- Each application message and Channel system message takes the next sequence
  number, `max_seq + 1`. Readers continue with the cursor returned by the server.
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
- `Publish` and `PublishAutoSeq` store each recipient once, in order of first
  appearance in the request. Reads return the list as stored.
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
  server-side global order. State changes commit in that order, while non-streaming reads
  acquire a consistent snapshot at their position. Subscribe is excluded.
- When publishing, the server checks the `principal + token` pair and Channel write permission against the latest
  committed state preceding the message commit. No token or Channel mutation can
  intervene between those checks and the commit.
- If RemoveMember commits first on a protected or private Channel, a later publish
  by that removed member using an unconsumed UUID returns `PERMISSION_DENIED`;
  public Channel writes are not controlled by membership. If DeleteToken commits first, a later publish using
  that deleted pair returns `UNAUTHENTICATED`; the same token string under another
  principal is unaffected. A message published successfully before the change remains committed.
- WriteObject may interleave with other operations while processing. Only a fully
  and durably committed object is visible to reads and message references; that
  complete commit is the write's ordering point.
- CreateChannel, an actual member addition, and an actual member removal each
  commit atomically with one corresponding Channel system message. Channel
  metadata, the system message, UUID allocation and consumption, and `max_seq`
  either all commit or none do. The initialization message is the new Channel's
  first message, preceding all application and membership-change messages in it.
- A write that changes state returns success after the change is synchronously
  persisted. When the target is a non-member, RemoveMember's position in the global
  order is the point where it confirms the member is absent. It then returns success.
- Overlapping mutations and non-streaming reads observe the complete state before
  or after a commit according to the server global order, never a partial commit.
- Subscribe batch visibility and starting-sequence rules are defined in section
  3.7. When reading history in a separate phase before subscribing, use the first
  Fetch page's `last_seq` as the fixed watermark as described in section 3.8.
  Permissions are evaluated separately on each RPC.

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
allocation or message state. UUID 0 returns `seq=0` for the global initialization message. For other UUIDs,
the server returns `NOT_FOUND` unless a committed message has consumed the UUID. This covers both unallocated
UUIDs and allocated but unused UUIDs. On success, `seq` is the sequence number of the committed message that
consumed the UUID.

### 3.4 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

The client supplies `seq` for a compare-and-set publish. All of the following conditions must hold.

- The `principal + token` pair is valid.
- The payload is within the size limit.
- The requested `seq` equals the current `max_seq + 1`.
- The Channel exists and the caller has write permission.
- The recipients meet the requirements in section 2.6.
- The `object_keys` meet the requirements in section 2.7.

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

Success returns an empty response. The server sets `ts_ms`, and authorized callers
can then read the message or receive it through a subscription.

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

The request follows these rules.

- The `principal + token` pair must be valid.
- limit must be in `1..1000`.
- Empty channels means no Channel filter. When non-empty, every listed Channel
  must exist and be readable by the caller; otherwise the whole request returns
  an error: `NOT_FOUND` for a missing Channel and `PERMISSION_DENIED` for an
  unreadable Channel. After validation, Fetch returns only messages the caller
  can read from the listed Channels.
- `only_my_recipient=true` enables recipient filtering.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Scan from seq 0; seq 0 is returned only when the filters allow it |
| `> max_seq` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `1..max_seq` | Scan visible messages from this seq |

The response follows these rules.

- `messages` contains at most `limit` messages matching the ACL, Channel, and recipient filters.
  Within each page, messages are ordered by strictly increasing `seq`. Filtering can leave gaps in the sequence numbers.
- The scan budget or the per-page message byte budget can end a page before `limit`.
  A page may even be empty after scanning invisible messages. Use the returned `next_seq`
  to continue after a short or empty page.
- A single matching message may exceed the soft page budget and is still subject to the
  gRPC transport limit. The soft budget never causes an empty-page loop at the same cursor.
- `next_seq` is the suggested starting point for the next scan.
- `last_seq` is the highest committed sequence number observed by this response.
- Continue while `next_seq <= last_seq`. When `next_seq > last_seq`, the scan has reached
  the tail observed by this response.
- Neither ACL nor filtering moves `next_seq` backward.

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

Pushes messages visible to the caller in global `seq` order.

- The `principal + token` pair must be valid.
- Subscribe is outside the global order of non-streaming reads and writes and has no
  linearization point relative to concurrent RPCs. Each batch uses a consistent view
  that includes commits completed before the batch began. Those commits become visible
  by the next successful 100 ms view refresh.
- An empty `channels` list disables filtering by Channel ID. Otherwise, every listed
  Channel must exist and be readable by the caller. The whole stream returns `NOT_FOUND`
  for a missing Channel or `PERMISSION_DENIED` for an unreadable one. After validation,
  the server pushes only readable messages from the listed Channels.
- `only_my_recipient=true` enables recipient filtering.
- After validation, the server scans past records excluded by ACL, Channel, or recipient
  filters without sending a response for them. If an explicitly listed Channel becomes
  unreadable, a later batch returns `PERMISSION_DENIED` and ends the stream.
- With no Channels specified, later batches automatically include newly created readable
  Channels and Channels for which the caller gains read permission. The caller can keep
  the same subscription. Previously filtered and consumed messages are not replayed.
  Later batches filter out private Channels that become unreadable and the stream continues.
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

A Subscribe stream stays open until interrupted. The business gRPC endpoint sends
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

To discover Channels continuously from the start of history, open one Subscribe
stream with `from_seq=0`, `channels=[]`, and `only_my_recipient=false`. The server
delivers visible historical messages in order and waits for new messages at the
tail. No initial Fetch or ListChannels call is needed.

Use the protocol and membership information in [Channel system messages](SYSTEM_PROTOCOL.md)
to decide which readable Channels belong in the application's list. Nonmembers can also
read public and protected Channels. If a member-added event arrives and Channel attributes
are missing, call GetChannel once with that `channel_id`.

If the caller needs to know when it has processed the messages that existed at
startup, it can page through Fetch before subscribing.

1. Fetch from `from_seq=0` with `channels=[]` and `only_my_recipient=false`.
   Record the first page's `last_seq` as the fixed watermark `H`, even if the page is empty.
2. Starting with the first page, process only messages with `seq<=H`. While
   `next_seq<=H`, fetch the next page from `next_seq`, including after a short or empty
   page. Later pages may report a higher `last_seq`. Keep the original `H` throughout.
3. Once `next_seq>H`, the startup history is complete. Subscribe from `from_seq=H+1`
   with the same filters to receive messages committed during and after recovery.

For either approach, resume interrupted subscriptions as described in section 3.7.
Each Fetch page and Subscribe batch uses the read permissions in effect for that page or batch.
Gaining read permission does not replay history before the cursor. Fetch the relevant
Channel separately if that history is needed. Recipient filtering also applies to system
messages, so `only_my_recipient=true` cannot be used for discovery. Notification of removal
from a private Channel follows section 2.5.

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
- Success returns a nonzero `object_id` and an opaque `object_token`. Object IDs
  increase globally in the order the server reserves them. Gaps are possible and IDs
  are never reused. With concurrent uploads, the object with the higher ID may commit
  first, and its call may also return first. Objects are immutable.
- Success is returned only after object data and metadata are durably committed.
  If the result is uncertain after interruption, retrying may create another object.

The server returns `INVALID_ARGUMENT` if `principal=0`, the metadata is invalid, or the data size
is outside the allowed range. An invalid combination of a nonzero principal and token returns
`UNAUTHENTICATED`. Object-file storage space, quota, or inode exhaustion returns
`RESOURCE_EXHAUSTED`. Other storage availability failures return `UNAVAILABLE`.
Fatal storage failures follow section 2.1.

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
  linearization point to read committed metadata and check each capability in
  input order. A missing or uncommitted object or a token mismatch gives that
  item a `NOT_FOUND` error.
- On success, `metadata` has exactly the same length and order as the request;
  each item is an independent `ObjectMetadata` message with the same fields as
  the single-object response in section 4.2. Each occurrence of a duplicate
  ObjectKey gets its own result. Different tokens for the same `object_id` are each checked.
- If any item fails, the entire RPC returns one error and no partial results.
  Storage corruption (`DATA_LOSS`) takes precedence over other fatal storage
  failures (`UNAVAILABLE`), which in turn take precedence over `NOT_FOUND`.
  Both kinds of storage failure trigger server shutdown as described in section 2.1.

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
- If the request times out or the success response is lost, the Channel may already exist.
  If you retry with the same arguments and both requests commit, the current data history
  permanently retains two Channels with the same name, each with its own ID and initialization message.

### 5.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

- The request must authenticate with `principal + token`, and `principal` must
  be greater than zero.
- Read permission follows section 2.5.
- A missing Channel returns `NOT_FOUND`.
- Denied read access returns `PERMISSION_DENIED`.

### 5.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

- The request must authenticate with `principal + token`, and `principal` must
  be greater than zero.
- Pagination is not currently supported.
- The server filters by read permission before applying the requested filter.
- `CHANNEL_FILTER_ALL` returns all visible Channels.
- `CHANNEL_FILTER_JOINED` returns the visible Channels the caller has joined.
- `CHANNEL_FILTER_OWNED` returns the visible Channels the caller created.
- An invalid filter returns `INVALID_ARGUMENT`.

### 5.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

- The request must authenticate with `principal + token`; both the operator and
  target principal must be greater than zero.
- Only the Channel creator may add members.
- `channel_id=0` returns `PERMISSION_DENIED`.
- A missing Channel returns `NOT_FOUND`.
- An existing member returns `ALREADY_EXISTS` without a system message, even when
  the Channel has reached its total membership limit.
- Adding a member must satisfy the total membership limit in [section 5](#5-channelservice).
- An actual addition writes one member-added system message in that Channel. See the
  [system protocol](SYSTEM_PROTOCOL.md) for the schema and section 2.8 for atomic commitment.

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

- The request must authenticate with `principal + token`; both the operator and
  target principal must be greater than zero.
- Only the Channel creator may remove members.
- `channel_id=0` returns `PERMISSION_DENIED`.
- A missing Channel returns `NOT_FOUND`.
- The creator cannot be removed.
- Removing a non-member is idempotent and succeeds without a system message.
- An actual removal writes one member-removed system message in that Channel. See the
  [system protocol](SYSTEM_PROTOCOL.md) for the schema and section 2.8 for atomic commitment.
- After a successful removal from a protected or private Channel, later publishes by the
  removed principal using an unconsumed UUID return `PERMISSION_DENIED`. Removing a member
  from a public Channel does not change that principal's write permission. System messages
  follow the read permissions in section 2.5.

## 6. AdminService

AdminService is defined in [`proto/openevent/sdk/proto/admin.proto`](../proto/openevent/sdk/proto/admin.proto), runs only
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
  this section's limit and per-page message byte budget rules.
- limit is required in `1..1000`.
- Messages are returned in ascending seq order. The per-page message byte budget
  may end a page before `limit`.
- One message may exceed the soft page budget and is still subject to the gRPC
  transport limit. The cursor does not skip a message deferred by the budget.
- next_seq is the suggested continuation point.
- last_seq is the highest committed seq observed when the page starts.
- A cursor beyond the tail returns an empty page and the tail's next position.
  `next_seq > last_seq` means the page has reached its observed tail.
- Each page is a consistent view. Concurrent appends may increase `last_seq` on later pages.
