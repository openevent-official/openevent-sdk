# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.4.1
> Date: 2026-07-17

This document defines the public behavior of the business protocol
[`proto/openevent.proto`](../proto/openevent.proto) and management protocol
[`proto/admin.proto`](../proto/admin.proto). It does not define server storage,
recovery, deployment topology, or internal execution mechanisms.

## 1. Basic Conventions

- All APIs use `gRPC + Protocol Buffers`.
- Business requests carry `principal + token`; `AdminService` does not.
- Clients use `seq` as the global message position and do not maintain a separate
  message-storage offset.
- Committed `seq` values increase monotonically and are not reused. Clients must
  not depend on adjacency.
- Each `EventMessage` contains `ts_ms`, the Unix millisecond timestamp when the
  server received the publish request.
- `payload` is opaque bytes; OpenEvent does not parse or validate its business schema.
- OpenEvent retains all committed messages and Channel metadata.

### 1.1 Services

| Service | Responsibility |
|---------|----------------|
| `EventService` | Status queries, publishing, batch fetch, and streaming subscription |
| `ChannelService` | Channel creation, queries, listing, and member management |
| `AdminService` | Token management and administrative message queries |

## 2. Common Semantics

### 2.1 gRPC Status Codes

| Status | Meaning |
|--------|---------|
| `UNAUTHENTICATED` | token is missing, invalid, or not bound to `principal` |
| `PERMISSION_DENIED` | ACL check failed or the operation is not allowed |
| `NOT_FOUND` | Channel does not exist |
| `ALREADY_EXISTS` | Resource already exists, such as an existing member |
| `INVALID_ARGUMENT` | Missing/invalid fields or invalid recipients |
| `RESOURCE_EXHAUSTED` | payload exceeds the deployment limit |
| `ABORTED` | `Publish.req.seq != max_seq + 1` |
| `CANCELLED` | The call was cancelled; a publish result may be uncertain |
| `DEADLINE_EXCEEDED` | The call exceeded its deadline; a publish result may be uncertain |
| `UNAVAILABLE` | Service or dependency is unavailable |
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

### 2.6 Read/Write Linearization and Commit Ordering

- All mutations and non-streaming stateful business/administrative reads have one
  server-side global order. Writes commit in that order, while non-streaming reads
  acquire a consistent snapshot at their position. Subscribe is excluded.
- Publish evaluates token validity and Channel write permission against the latest
  committed state preceding the message commit.
- If RemoveMember or DeleteToken commits first, a later publish returns
  `PERMISSION_DENIED` or `UNAUTHENTICATED`. A message committed first remains valid.
- A write returns success only after it has been synchronously and durably
  persisted. Success means the mutation satisfies the server durability contract.
- A non-streaming read issued after a write RPC returned success must observe the
  complete result of that write. Overlapping mutations and non-streaming reads
  observe the complete state before or after a commit according to the server
  global order, never a partial commit.
- Subscribe does not provide this read-after-write or cross-RPC serialization
  guarantee. Use GetStatus and Fetch when a stable watermark is required.

### 2.7 Handling an Uncertain Publish Result

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
an allowed payload size, `seq == max_seq+1`, an existing writable Channel, and
valid recipients.

| Condition | Status |
|-----------|--------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Missing Channel | `NOT_FOUND` |
| No write permission | `PERMISSION_DENIED` |
| Non-member recipient | `INVALID_ARGUMENT` |

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

## 4. ChannelService

### 4.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

- visibility must be public, protected, or private; unset means public.
- The creator is automatically added to members.
- Request members are deduplicated.
- Success returns the created ChannelInfo.

### 4.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

Read permission follows section 2.4. Missing Channel returns `NOT_FOUND`; denied
access returns `PERMISSION_DENIED`.

### 4.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

Pagination is not currently supported. The server filters by read permission and
then applies ALL, JOINED, or OWNED. Invalid filter returns `INVALID_ARGUMENT`.

### 4.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

Only the creator may add members. System Channel changes are denied, missing
Channel returns `NOT_FOUND`, and an existing member returns `ALREADY_EXISTS`.

### 4.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

Only the creator may remove members. The creator cannot be removed. Removing a
non-member is idempotent success.

## 5. AdminService

AdminService is defined in [`proto/admin.proto`](../proto/admin.proto), runs only
on the admin endpoint, and carries no business principal/token. Deployments must
protect it with network isolation or external authorization.

```protobuf
rpc AddToken(AddTokenRequest) returns (AddTokenResponse);
rpc DeleteToken(DeleteTokenRequest) returns (DeleteTokenResponse);
rpc ListTokens(ListTokensRequest) returns (ListTokensResponse);
rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
```

### 5.1 ListTokens

- page_token is an opaque exclusive cursor; empty means the first page.
- limit is required in `1..1000`.
- Bindings are ordered by unsigned persisted token-key bytes.
- Empty next_page_token means traversal is complete.
- Invalid/unsupported cursors return `INVALID_ARGUMENT`.
- Each page is consistent, but token mutations mean pages are not a strong
  cross-RPC snapshot.
- Bindings and page tokens are sensitive and must not be logged or traced.

### 5.2 ListMessages

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

## 6. Compatibility Guidance

- Both proto files together form the public protocol contract.
- Branch on gRPC status codes, not error text.
- Consumers must accept seq gaps, record the last processed seq, and resume from
  `seq + 1`.
- Put payload schema rules in the business protocol named by Channel protocol.
