# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.4.2
> Date: 2026-07-31

This document defines the public behavior of the business protocol
[`proto/openevent.proto`](../proto/openevent.proto) and management protocol
[`proto/admin.proto`](../proto/admin.proto). It does not define server storage,
recovery, deployment topology, or internal execution mechanisms.

## 1. Basic Conventions

- All APIs use `gRPC + Protocol Buffers`.
- Event, Channel, and object-write requests carry `principal + token`.
  Object reads use `object_id + object_token`; `AdminService` carries neither.
- Clients use `seq` as the global message position and do not maintain a separate
  message-storage offset.
- Committed `seq` values increase monotonically and are not reused. Clients must
  not depend on adjacency.
- Each `EventMessage` contains `ts_ms`, the Unix millisecond timestamp when the
  server received the publish request.
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
| `ALREADY_EXISTS` | Resource already exists, such as an existing member |
| `INVALID_ARGUMENT` | Missing/invalid fields or invalid recipients |
| `RESOURCE_EXHAUSTED` | payload exceeds its limit or object storage has insufficient space/quota |
| `OUT_OF_RANGE` | ReadObject offset is greater than the object size |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | The call was cancelled; a write result may be uncertain |
| `DEADLINE_EXCEEDED` | The call exceeded its deadline; a write result may be uncertain |
| `UNAVAILABLE` | Service or dependency is unavailable |
| `DATA_LOSS` | Committed object metadata or data is corrupt; the server exits rather than serve partial state |
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

### 2.4 Channel ACL

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

### 2.5 Recipients

- recipients is the targeted principal list; an empty list means no targets.
- Every recipient must be a Channel member when publishing, otherwise the server
  returns `INVALID_ARGUMENT`.
- With `only_my_recipient=true`, only messages targeting the current principal
  are returned or pushed; messages with empty recipients do not match.

### 2.6 Object Capabilities and Message References

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

### 2.7 Read/Write Linearization and Commit Ordering

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
- Subscribe does not provide this read-after-write or cross-RPC serialization
  guarantee. Use GetStatus and Fetch when a stable watermark is required.

### 2.8 Handling an Uncertain Publish Result

When Publish or PublishAutoSeq does not return a successful response, the client
cannot always conclude that the message was not committed. A broken connection,
client cancellation, `DEADLINE_EXCEEDED`, and some `UNAVAILABLE` failures may
occur after the server started or completed persistence.

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

Do not immediately retry a publish with an uncertain result. Instead:

1. Before publishing, record the current `max_seq` and retain the request's
   channel ID, principal, recipients, and payload. The payload's application
   protocol should carry a unique event ID.
2. After the failure, call GetStatus and record `reconcile_max_seq`. Because that
   read is ordered after the earlier publish, this watermark must include the
   publish if it committed.
3. Fetch from the pre-publish `max_seq + 1`, following `next_seq` until the scan
   passes `reconcile_max_seq`. Subscribe may help find a matching message quickly,
   but it cannot by itself prove absence because filtered seq values produce no
   progress response.
4. For Publish, first inspect the explicitly requested seq. For PublishAutoSeq,
   inspect the scanned range using the unique event ID or complete request data.
5. Treat a matching message as success and use its seq. Only after confirming
   that no match exists may the caller decide to issue a new publish request.

If an application can concurrently publish identical content, payload and request
fields cannot reliably identify one attempt; a unique application event ID is
required.

Reconciliation must use an identity that is still authenticated and authorized
to read the target Channel, and its channel/recipient filters must include the
candidate message. If the token was revoked or Channel read access was removed,
business Fetch cannot prove the outcome; use another authorized identity or a
protected administrative query.

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

- `max_seq`: highest committed message seq; 0 when there are no messages.
- `min_seq`: 1 when messages exist under full retention, otherwise 0.

Clients may use `max_seq+1` as the next Publish candidate. A concurrent publish
can consume it first, causing `ABORTED`.

### 3.2 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

Publishes with a client-specified CAS seq. Success requires valid authentication,
an allowed payload size, `seq == max_seq+1`, an existing writable Channel, valid
recipients, and valid object capabilities.

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

### 3.3 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

Uses the same validation as Publish, but the server allocates seq. The response
contains the committed message seq.

### 3.4 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

- token must match principal.
- limit must be in `1..1000`.
- Empty channels means no Channel filter; otherwise normal ACL still applies.
- `only_my_recipient=true` enables recipient filtering.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `> max_seq` | Empty result with `next_seq=max_seq+1`, `last_seq=max_seq` |
| `1..max_seq` | Scan visible messages from this seq |

The response contains at most limit visible messages. A server may end a page
early because of scan or response-size budgets, so a short page, or an empty page
after scanning invisible messages, does not imply end of stream. A single
returnable message may exceed the page's soft size budget but remains subject to
the gRPC transport limit; the soft budget never causes an empty-page loop at the
same cursor. `next_seq` is the suggested continuation point and `last_seq` is the
highest committed seq observed by this response. Continue while
`next_seq <= last_seq`; a larger next_seq means the observed tail was reached.
Filtering never moves next_seq backward.

### 3.5 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

Pushes messages visible to the caller in global seq order. It has no Channel
filter; `only_my_recipient` is supported. Subscribe is outside the non-streaming
read/write linearization order and provides no read-after-write guarantee relative
to other RPCs.

| `from_seq` | Behavior |
|------------|----------|
| `0` | Wait for messages after current `max_seq+1` |
| `> max_seq` | Return `next_seq=max_seq+1` and close |
| `1..max_seq` | Push visible messages from this seq |

`SubscribeResponse.message` carries normal messages. `next_seq` is only used for
the `from_seq > max_seq` boundary. After disconnect, resume from the last
processed `seq + 1` using Subscribe or Fetch.

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

`nbytes` must be positive. The server validates the complete committed object and
then returns a range according to these rules:

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
is missing, not a regular file, has the wrong size, or fails its integrity check;
the server then shuts down with a nonzero exit rather than continue serving.

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

Read permission follows section 2.4. Missing Channel returns `NOT_FOUND`; denied
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
