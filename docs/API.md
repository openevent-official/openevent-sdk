# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.7.0
> Date: 2026-08-21

This document defines the public behavior of the business protocol
[`proto/openevent.proto`](../proto/openevent.proto) and management protocol
[`proto/admin.proto`](../proto/admin.proto). It does not define server storage,
recovery, deployment topology, or internal execution mechanisms.

## 1. Basic Conventions

- All APIs use `gRPC + Protocol Buffers`.
- Clients use `seq` as the global message position and do not maintain a separate
  message-storage offset.
- Committed `seq` values increase monotonically and are not reused. Clients must
  not depend on adjacency.
- Each `EventMessage` contains `ts_ms`, the Unix millisecond timestamp when the
  server received the publish request.
- Each `EventMessage` contains a nonzero `uuid` allocated by `AllocateUuids`.
- `payload` is opaque bytes; OpenEvent does not parse or validate its business schema.
- OpenEvent retains all committed messages, Channel metadata, and immutable objects.

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
| `UNAUTHENTICATED` | token is missing, invalid, or not bound to `principal` |
| `PERMISSION_DENIED` | ACL check failed or the operation is not allowed |
| `NOT_FOUND` | Channel does not exist, or an object capability is absent or invalid |
| `ALREADY_EXISTS` | Resource already exists, such as an existing member or a UUID already used by a message |
| `INVALID_ARGUMENT` | Missing/invalid fields or invalid recipients |
| `RESOURCE_EXHAUSTED` | payload exceeds its limit or object storage has insufficient space/quota |
| `OUT_OF_RANGE` | ReadObject offset is greater than the object size |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | The call was cancelled; a write result may be uncertain |
| `DEADLINE_EXCEEDED` | The call exceeded its deadline; a write result may be uncertain |
| `UNAVAILABLE` | Service or dependency is unavailable |
| `DATA_LOSS` | Persistent storage is corrupt; the server exits rather than serve partial state |
| `INTERNAL` | Unexpected error |

### 2.2 Message Sequence

- `EventMessage.seq` is a globally monotonically increasing message sequence; 0
  is reserved.
- Committed values are not reused.
- The public contract does not guarantee adjacent values. Readers must accept
  gaps and continue with server-provided cursors.
- `GetStatus`, `Fetch`, and `Subscribe` all use this sequence.

### 2.3 Payload

- `Publish` and `PublishAutoSeq` accept payload, which may be empty.
- The size limit is deployment-specific; the reference default is 16 MiB.
- Oversized payload returns `RESOURCE_EXHAUSTED` and creates no message.

### 2.4 Message UUIDs

- UUID 0 is reserved.
- Every Publish and PublishAutoSeq request must carry one nonzero UUID returned by AllocateUuids.
- UUID 0 or a UUID not allocated by the server returns `INVALID_ARGUMENT`. A UUID already consumed by a committed
  message returns `ALREADY_EXISTS`. Neither result creates a message or advances `max_seq`.
- After authentication, a consumed UUID returns `ALREADY_EXISTS` before payload, ObjectKey, Channel, ACL, or
  Publish seq-CAS validation. Duplicate handling does not compare any of those request fields; a UUID can consume
  at most one message.
- The Python high-level client exposes one UUID at a time through `get_uuid()`; its pool behavior is described in
  the usage guide. The generated gRPC stub exposes AllocateUuids as part of the protocol.
- The allocation and lookup RPCs are defined in sections 3.2 and 3.3. The complete recovery procedure for an
  uncertain publish result is defined only in section 2.9.

### 2.5 Channel ACL

| Visibility | Read Permission | Write Permission |
|------------|-----------------|------------------|
| `VISIBILITY_PUBLIC` | All authenticated callers | All authenticated callers |
| `VISIBILITY_PROTECTED` | All authenticated callers | Channel members |
| `VISIBILITY_PRIVATE` | Channel members | Channel members |

System channel `channel_id=0`:

- Can be queried by every authenticated caller.
- Appears in `CHANNEL_FILTER_ALL`, not JOINED or OWNED results.
- Returns fixed ChannelInfo with protected visibility, no creator, and no members.
- Does not allow publishing or member changes.

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
- Object capabilities can be transferred. They cannot currently be revoked,
  rotated, deleted, or restricted after creation. Treat `object_token` as a secret.
- Publish and PublishAutoSeq accept at most 1024 `object_keys`. The server does
  not deduplicate the list and stores it in request order.
- Every reference must have a nonzero object ID and a nonempty token. Malformed
  references return `INVALID_ARGUMENT`; a missing object, uncommitted object, or
  token mismatch returns `NOT_FOUND`.
- If any reference is invalid, the complete message is rejected and `max_seq`
  does not advance. Reference validation does not read object data.
- Fetch, Subscribe, and administrative ListMessages return complete ObjectKeys.
  Any caller allowed to read a message therefore receives every capability in it.

### 2.8 Read/Write Linearization and Commit Ordering

- All mutations and non-streaming stateful business/administrative reads have one
  server-side global order. Writes commit in that order, while non-streaming reads
  acquire a consistent snapshot at their position. Subscribe is excluded.
- Publish evaluates token validity and Channel write permission against the latest
  committed state preceding the message commit.
- If RemoveMember or DeleteToken commits first, a later publish returns
  `PERMISSION_DENIED` or `UNAUTHENTICATED`. A message committed first remains valid.
- WriteObject performs file I/O between two ordered metadata commits; other
  operations may be ordered between those phases. Only committed objects are
  visible, and its final metadata commit is the public write point.
- A write returns success only after it has been synchronously and durably
  persisted. Success means the mutation satisfies the server durability contract.
- A non-streaming read issued after a write RPC returned success must observe the
  complete result of that write. Overlapping mutations and non-streaming reads
  observe the complete state before or after a commit according to the server
  global order, never a partial commit.
- Subscribe has no ordering point relative to concurrent RPCs, but a batch waits
  for the shared snapshot to cover commits completed before that batch began.
  This visibility may take until the next successful 100 ms snapshot refresh and
  does not provide a stable watermark. Use GetStatus and Fetch for fixed-watermark
  reads.

### 2.9 Handling an Uncertain Publish Result

When Publish or PublishAutoSeq does not return a successful response, the client
cannot always conclude that the message was not committed. A broken connection,
client cancellation, `DEADLINE_EXCEEDED`, and some `UNAVAILABLE` failures may
occur after the server started or completed persistence. Retain the UUID supplied
to that call. Retry the same Publish or PublishAutoSeq with that UUID. If the retry returns `ALREADY_EXISTS`, call
`GetSeqByUuid` with the UUID to obtain the original committed seq; do not scan messages or UUIDs to reconcile this
result. Retrying with a new UUID is a new publish attempt.

For an OpenEvent server conforming to this contract, classify the terminal
publish status as follows. A non-commit guarantee means that this request did not
create an `EventMessage` or advance `max_seq` on behalf of this request; it does
not prove that an earlier attempt for the same application event was not
committed, and does not mean that the cause is recoverable.

| Status | Commit guarantee |
|--------|------------------|
| `OK` | Synchronously persisted and committed |
| `UNAUTHENTICATED` | Guaranteed not committed |
| `PERMISSION_DENIED` | Guaranteed not committed |
| `NOT_FOUND` | Guaranteed not committed |
| `INVALID_ARGUMENT` | Guaranteed not committed |
| `RESOURCE_EXHAUSTED` | Guaranteed not committed |
| `ABORTED` | Guaranteed not committed; only for Publish seq-CAS conflicts and not expected from PublishAutoSeq |
| `ALREADY_EXISTS` | Guaranteed not committed; the UUID was already used by an earlier committed message |
| Any other non-`OK` status, or no gRPC status | Uncertain outcome |

Therefore `CANCELLED`, `DEADLINE_EXCEEDED`, `UNKNOWN`, `UNAVAILABLE`,
`INTERNAL`, connection loss, and proxy resets must be treated as uncertain. Even
if one `UNAVAILABLE` error appears to occur before connection establishment, a
caller must not infer non-commit from error text. Only statuses explicitly marked
"Guaranteed not committed" may enter direct cause-remediation and retry logic.
A guaranteed non-commit caused by a permanent parameter, permission, or payload
error still must not be retried blindly.

The table applies only to terminal statuses produced by a conforming OpenEvent
server. If a proxy, load balancer, or client can synthesize the same status after
forwarding the request, and the deployment cannot prove that it happened before
forwarding, treat the outcome as uncertain.

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- `max_seq`: highest committed message seq; 0 when there are no messages.
- `min_seq`: 1 when messages exist under full retention, otherwise 0.

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
allocation or message state. UUID 0 returns `INVALID_ARGUMENT`. The server returns `NOT_FOUND` unless the UUID has
already been consumed by a committed message, including when the UUID was never allocated or was allocated but has
not been used. On success, `seq` is the committed message sequence that consumed the UUID.

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

- token must match principal.
- limit must be in `1..1000`.
- Empty channels means no Channel filter. When non-empty, every listed Channel
  must exist and be readable by the caller; otherwise the whole request returns
  an error: `NOT_FOUND` for a missing Channel and `PERMISSION_DENIED` for an
  unreadable Channel. After validation, normal ACL still applies to the selected
  Channels.
- `only_my_recipient=true` enables recipient filtering.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `> max_seq` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `1..max_seq` | Scan visible messages from this seq |

The response contains at most limit visible messages. Explicit channel permissions
are validated before scanning, so one failed Channel never produces a partial
response. A server may end a page
early because of scan or response-size budgets, so a short page, or an empty page
after scanning invisible messages, does not imply end of stream. A single
returnable message may exceed the page's soft size budget but remains subject to
the gRPC transport limit; the soft budget never causes an empty-page loop at the
same cursor. `next_seq` is the suggested continuation point and `last_seq` is the
highest committed seq observed by this response. Continue while
`next_seq <= last_seq`; a larger next_seq means the observed tail was reached.
Filtering never moves next_seq backward.
If a scanned message refers to a Channel that is not present, the call returns
`INTERNAL`; this does not mark persistent storage corrupt or stop the server.

### 3.7 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

Pushes messages visible to the caller in global seq order. Empty `channels`
disables Channel filtering. When non-empty, every listed Channel must exist and
be readable by the caller; otherwise the stream returns an error: `NOT_FOUND` for
a missing Channel and `PERMISSION_DENIED` for an unreadable Channel. After
validation, only readable messages from the listed Channel IDs are pushed.
Normal Channel ACL and `only_my_recipient` filtering still apply. Filtered
records do not produce responses but are consumed by the server's scan. If an
explicitly listed Channel becomes unreadable while the stream is running, a
later batch returns `PERMISSION_DENIED` and ends the stream. Subscribe is outside
the non-streaming read/write linearization
order and has no linearization point relative to concurrent RPCs. Subscription
reads use a server-wide snapshot refreshed every 100 ms after successful commits;
a batch waits until that snapshot covers commits completed before the batch began.
If a scanned message refers to a Channel that is not present, the stream returns
`INTERNAL` and ends; this does not mark persistent storage corrupt or stop the
server.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Wait for messages after current `max_seq+1` |
| `> max_seq` | Return `next_seq=max_seq+1` and close |
| `1..max_seq` | Push visible messages from this seq |

`SubscribeResponse.message` carries normal messages. `next_seq` is only used for
the `from_seq > max_seq` boundary. After disconnect, resume from the last
processed `seq + 1` using Subscribe or Fetch.

After authentication and explicit Channel validation succeed, the server sends
gRPC initial response metadata before it waits for or pushes an OpenEvent
message. This confirms that the subscription was accepted even when its starting
seq or every current record is filtered out. The metadata is a gRPC control
response, not a `SubscribeResponse` or an OpenEvent message.

The OpenEvent server has no fixed lifetime for a valid, established Subscribe
stream. Its public gRPC endpoint sends an HTTP/2 PING after 30 seconds without
transport activity and closes a connection whose PING acknowledgement does not
arrive within 10 seconds. It accepts client PINGs no more frequent than every 20
seconds. The Python high-level client sends client PINGs every 30 seconds and
does not set a deadline for Subscribe; a caller using another gRPC client or a
custom channel must configure a compatible transport keepalive policy.

When the server begins normal or fatal shutdown, it actively ends every
established Subscribe stream. Treat this like any other stream interruption:
record the last processed `seq` and resume from `seq + 1` using Subscribe or
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
- The server stores these strings as opaque metadata and does not interpret the
  object contents or derive a file path from metadata.
- Success returns a nonzero globally increasing `object_id` and an opaque
  `object_token`. IDs can contain gaps and are never reused. Objects are immutable.
- The object token is a 32-byte CSPRNG value encoded as 43 unpadded Base64URL
  characters. Callers must treat its representation as opaque.
- Success is returned only after object data and metadata are durably committed.
  A cancellation, deadline, connection loss, or some server error after processing
  starts can leave the result uncertain. The server continues committing or
  cleaning up after client cancellation.
- The API has no idempotency key or query-by-creator operation. If the success
  response is lost, the token cannot be recovered; do not blindly retry when
  duplicate immutable objects are unacceptable.

Invalid metadata or data size returns `INVALID_ARGUMENT`. Invalid principal/token
returns `UNAUTHENTICATED`. Storage space, quota, or inode exhaustion returns
`RESOURCE_EXHAUSTED`; other storage availability failures return `UNAVAILABLE`.
Any RocksDB operation failure causes the server to exit after returning its error;
bytes that violate the persisted record format are `DATA_LOSS` and also cause
exit. If the final metadata commit reports an error, the server exits because it
cannot safely determine whether the object became committed. Recovery keeps a
committed object or removes its remaining preparing state.

### 4.2 GetObjectMetadata

```protobuf
rpc GetObjectMetadata(GetObjectMetadataRequest) returns (GetObjectMetadataResponse);
```

Returns the committed object's `name`, `type`, `description`, and total `nbytes`.
It does not read or verify the object data file.

### 4.3 ReadObject

```protobuf
rpc ReadObject(ReadObjectRequest) returns (ReadObjectResponse);
```

`nbytes` must be positive. The server checks the committed object's file type and
total size, then reads only the requested range according to these rules:

| Condition | Result |
|-----------|--------|
| `offset < object size` | Return `min(nbytes, object size - offset)` bytes |
| `offset == object size` | Success with empty `data` |
| `offset > object size` | `OUT_OF_RANGE` |
| `nbytes == 0` | `INVALID_ARGUMENT` |

`nbytes` may be any positive uint64; ranges cannot overflow because the server
subtracts offset from the object size before choosing the response length.

For GetObjectMetadata and ReadObject, `object_id=0` or an empty object token is
`INVALID_ARGUMENT`. A missing/uncommitted object and every token mismatch return
the same `NOT_FOUND` result. A nonempty token is otherwise treated as opaque and
is not rejected based on syntax. ReadObject returns `DATA_LOSS` if committed data
is missing, not a regular file, or has the wrong size; the server then shuts down
with a nonzero exit rather than continue serving. The server does not maintain a
content digest, so same-size external changes are not detected.

## 5. ChannelService

### 5.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- visibility must be public, protected, or private; unset means public.
- The creator is automatically added to members.
- Request members are deduplicated.
- Success returns the created ChannelInfo.

### 5.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

Read permission follows section 2.5. Missing Channel returns `NOT_FOUND`; denied
access returns `PERMISSION_DENIED`.

### 5.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

Pagination is not currently supported. The server filters by read permission and
then applies ALL, JOINED, or OWNED. Invalid filter returns `INVALID_ARGUMENT`.

### 5.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

Only the creator may add members. System Channel changes are denied, missing
Channel returns `NOT_FOUND`, and an existing member returns `ALREADY_EXISTS`.

### 5.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

Only the creator may remove members. The creator cannot be removed. Removing a
non-member is idempotent success.

## 6. AdminService

AdminService is defined in [`proto/admin.proto`](../proto/admin.proto), runs only
on the admin endpoint, and carries no business principal/token. Deployments must
protect it with network isolation or external authorization.

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListTokens(ListTokensRequest) returns (ListTokensResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 6.1 ListTokens

- page_token is an opaque exclusive cursor; empty means the first page.
- limit is required in `1..1000`.
- Bindings are ordered by unsigned persisted token-key bytes.
- Empty next_page_token means traversal is complete.
- Invalid/unsupported cursors return `INVALID_ARGUMENT`.
- Each page is consistent, but token mutations mean pages are not a strong
  cross-RPC snapshot.
- Bindings and page tokens are sensitive and must not be logged or traced.

### 6.2 ListMessages

```protobuf
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

- Does not apply Channel ACL or recipient filtering.
- from_seq is inclusive; 0 starts from 1.
- limit is required in `1..1000`.
- Messages are returned in ascending seq order.
- A response-size budget may end a page before limit. One message may exceed the
  soft page budget but remains subject to the gRPC transport limit, and a message
  deferred by the budget is not skipped by next_seq.
- next_seq is the suggested continuation point.
- last_seq is the highest committed seq observed when the page starts.
- If a scanned message refers to a Channel that is not present, the call returns
  `INTERNAL`; this does not mark persistent storage corrupt or stop the server.
- A cursor beyond the tail returns an empty page and the tail's next position.
- Concurrent appends may increase last_seq on later pages.

## 7. Compatibility Guidance

- Both proto files together form the public protocol contract.
- Branch on gRPC status codes, not error text.
- Consumers must accept seq gaps, record the last processed seq, and resume from
  `seq + 1`.
- ObjectKey values are credentials. Preserve repeated-field order, do not log
  tokens, and do not assume an object capability remains private after attaching
  it to a readable message.
- Put payload schema rules in the business protocol named by Channel protocol.
