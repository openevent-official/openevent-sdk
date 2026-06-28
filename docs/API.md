# OpenEvent API Contract (gRPC)

[中文版](API_cn.md)

> Version: 0.3.0
> Date: 2026-04-30

This document describes the API behavior exposed by
[`proto/openevent.proto`](../proto/openevent.proto) and shared by all dependent projects. It is
intended for clients, server implementations, sync components, test tools, and
language SDKs. It does not describe server storage, recovery, deployment
topology, or internal execution mechanisms.

## 1. Basic Conventions

- All APIs are exposed through `gRPC + Protocol Buffers`.
- Business requests carry `principal + token`, except for `AdminService`.
- Clients only depend on `seq` as the message position. Any internal offset is
  not part of the public protocol.
- Each committed log entry is returned as an `EventMessage`. It contains `ts_ms`,
  the Unix millisecond timestamp when the server received the publish request.
- `payload` is an opaque byte array. OpenEvent does not parse it or validate any
  business schema.
- OpenEvent retains all committed history messages and channel metadata.
- Channels continue to exist after creation; published messages are read by
  global `seq`.

### 1.1 Services

| Service | Responsibility |
|---------|----------------|
| `EventService` | Status query, message publishing, batch fetch, streaming subscription |
| `ChannelService` | Channel creation, query, listing, and member management |
| `AdminService` | Token create, delete, and list operations |

## 2. Common Semantics

### 2.1 gRPC Status Codes

Callers should handle errors by gRPC status code:

| Status | Meaning |
|--------|---------|
| `UNAUTHENTICATED` | token is missing, invalid, or not bound to `principal` |
| `PERMISSION_DENIED` | ACL check failed, or caller is not allowed to perform the operation |
| `NOT_FOUND` | Channel does not exist |
| `ALREADY_EXISTS` | Resource already exists, such as adding an existing member |
| `INVALID_ARGUMENT` | Missing or invalid fields, or `recipients` does not satisfy member constraints |
| `RESOURCE_EXHAUSTED` | `payload` exceeds the deployment limit |
| `ABORTED` | `Publish` request has `req.seq != max_seq + 1` |
| `UNAVAILABLE` | Service or dependency is unavailable |
| `INTERNAL` | Unexpected error |

### 2.2 Payload

- `Publish` and `PublishAutoSeq` requests both contain `payload`.
- `payload` may be empty.
- The payload size limit is deployment-specific. The reference implementation
  defaults to 16 MiB.
- If the payload exceeds the limit, the server returns `RESOURCE_EXHAUSTED` and
  does not create a new message.

### 2.3 Channel ACL

| Visibility | Read Permission | Write Permission |
|------------|-----------------|------------------|
| `VISIBILITY_PUBLIC` | All authenticated callers | All authenticated callers |
| `VISIBILITY_PROTECTED` | All authenticated callers | Channel members |
| `VISIBILITY_PRIVATE` | Channel members | Channel members |

System channel `channel_id=0`:

- Can be queried by all authenticated callers.
- Appears in `CHANNEL_FILTER_ALL` list results.
- Does not appear in `CHANNEL_FILTER_JOINED` or `CHANNEL_FILTER_OWNED` results.
- Returns fixed `ChannelInfo`: `channel_id=0`,
  `visibility=VISIBILITY_PROTECTED`, unset `creator`, and `members=[]`.
- Does not allow publishing or member changes.

### 2.4 Recipients

- `recipients` is the targeted recipient principal list for a message.
- `recipients=[]` means no targeted recipients.
- When publishing, every principal in `recipients` must be a member of the
  channel.
- If any recipient is not a channel member, publish returns `INVALID_ARGUMENT`
  and creates no message.
- When fetching or subscribing with `only_my_recipient=true`, only messages whose
  `recipients` include the current `principal` are returned. Messages with
  `recipients=[]` do not match this filter.

## 3. EventService

### 3.1 GetStatus

```protobuf
rpc GetStatus(GetStatusRequest) returns (GetStatusResponse);
```

Returns the current global message range:

- `max_seq`: current maximum published message seq; `0` when there are no
  messages.
- `min_seq`: current minimum available seq; under full-retention semantics, this
  is `1` when messages exist and `0` otherwise.

Clients can calculate the next expected `Publish` seq as `max_seq + 1`.

### 3.2 Publish

```protobuf
rpc Publish(PublishRequest) returns (PublishResponse);
```

Publishes with a client-specified `seq` for CAS semantics.

Success conditions:

- token matches `principal`.
- `payload` does not exceed the size limit.
- `seq == current max_seq + 1`.
- Channel exists and the current `principal` can write to it.
- `recipients` satisfies the member constraints in 2.4.

Error semantics:

| Condition | Status |
|-----------|--------|
| `seq != max_seq + 1` | `ABORTED` |
| `channel_id=0` | `PERMISSION_DENIED` |
| Channel does not exist | `NOT_FOUND` |
| No write permission | `PERMISSION_DENIED` |
| recipient is not a channel member | `INVALID_ARGUMENT` |

Successful calls return an empty response. The message can then be read or
subscribed by authorized callers. The server writes `ts_ms` into the message.

### 3.3 PublishAutoSeq

```protobuf
rpc PublishAutoSeq(PublishAutoSeqRequest) returns (PublishAutoSeqResponse);
```

The server assigns the global seq for this publish.

Success conditions are the same as `Publish`, except the caller does not provide
`seq`. The success response contains the assigned global `seq`. The server writes
`ts_ms` into the message.

### 3.4 Fetch

```protobuf
rpc Fetch(FetchRequest) returns (FetchResponse);
```

Fetches messages visible to the current caller by global `seq`.

Request rules:

- token matches `principal`.
- `limit` must be in `1..1000`, otherwise `INVALID_ARGUMENT` is returned.
- `channels` is an optional channel ID filter. When it is empty, Fetch scans all
  channels visible to the caller. When it is non-empty, only messages from those
  channel IDs are returned, after normal ACL filtering.
- With `only_my_recipient=true`, only messages whose `recipients` include the
  current `principal` are returned.

Start semantics:

| `from_seq` | Behavior |
|------------|----------|
| `0` | Return empty result, `next_seq=max_seq+1`, and `last_seq=max_seq` |
| `> max_seq` | Return empty result, `next_seq=max_seq+1`, and `last_seq=max_seq` |
| `1..max_seq` | Scan from this seq and return visible messages |

Response semantics:

- `messages` contains at most `limit` visible `EventMessage` values after ACL,
  `channels`, and recipient filtering.
- Every message contains its publish-time `ts_ms`.
- `next_seq` is the suggested start point for the next fetch.
- `last_seq` is the highest committed global seq observed by this Fetch response.
- If `next_seq <= last_seq`, callers may continue scanning from `next_seq`.
  If `next_seq > last_seq`, the response reached the committed tail visible to
  this Fetch operation.
- ACL, channel, or recipient filtering never moves `next_seq` backward.

### 3.5 Subscribe

```protobuf
rpc Subscribe(SubscribeRequest) returns (stream SubscribeResponse);
```

Subscribes to messages visible to the current caller in the global stream.

Request rules:

- token matches `principal`.
- No `channel_id` is accepted; subscription scope is always the global stream.
- With `only_my_recipient=true`, only messages whose `recipients` include the
  current `principal` are pushed.

Start semantics:

| `from_seq` | Behavior |
|------------|----------|
| `0` | Wait for new messages after current `max_seq+1` |
| `> max_seq` | Return one response containing `next_seq=max_seq+1` and close the stream |
| `1..max_seq` | Push visible messages from this seq |

`SubscribeResponse` uses `oneof result`:

- `message`: a visible `EventMessage`.
- `next_seq`: only used when `from_seq > max_seq`, indicating a usable next
  start point.

Each pushed `message` contains its publish-time `ts_ms`.

When the client cancels or disconnects, the stream ends. To continue consuming,
record the processed seq and start a new subscription from the next expected
seq.

## 4. ChannelService

### 4.1 CreateChannel

```protobuf
rpc CreateChannel(CreateChannelRequest) returns (CreateChannelResponse);
```

Creates a channel.

- `visibility` must be `VISIBILITY_PUBLIC`, `VISIBILITY_PROTECTED`, or
  `VISIBILITY_PRIVATE`.
- Under proto3 defaults, an unset `visibility` is equivalent to
  `VISIBILITY_PUBLIC`.
- The creator is automatically added as a member.
- Request `members` are deduplicated; the creator appears only once.
- The success response returns the created `ChannelInfo`.

### 4.2 GetChannel

```protobuf
rpc GetChannel(GetChannelRequest) returns (GetChannelResponse);
```

Queries a channel by `channel_id`.

- Read permission follows 2.3.
- Missing channel returns `NOT_FOUND`.
- No read permission returns `PERMISSION_DENIED`.

### 4.3 ListChannels

```protobuf
rpc ListChannels(ListChannelsRequest) returns (ListChannelsResponse);
```

Lists channels visible to the current caller.

- Pagination is not supported.
- The server first filters by read permission.
- The server then applies `filter`:
  - `CHANNEL_FILTER_ALL`: all visible channels.
  - `CHANNEL_FILTER_JOINED`: visible channels where caller is a member.
  - `CHANNEL_FILTER_OWNED`: visible channels created by caller.
- Under proto3 defaults, an unset `filter` is equivalent to
  `CHANNEL_FILTER_ALL`.
- Invalid `filter` returns `INVALID_ARGUMENT`.

### 4.4 AddMember

```protobuf
rpc AddMember(AddMemberRequest) returns (AddMemberResponse);
```

Adds a channel member.

- Caller must be the channel creator.
- `channel_id=0` returns `PERMISSION_DENIED`.
- Missing channel returns `NOT_FOUND`.
- Existing member returns `ALREADY_EXISTS`.

### 4.5 RemoveMember

```protobuf
rpc RemoveMember(RemoveMemberRequest) returns (RemoveMemberResponse);
```

Removes a channel member.

- Caller must be the channel creator.
- `channel_id=0` returns `PERMISSION_DENIED`.
- Missing channel returns `NOT_FOUND`.
- Removing the creator is not allowed.

## 5. AdminService

`AdminService` manages token bindings. It is usually exposed on a separate admin
port and does not carry business `principal/token`.

Operations:

- `AddToken(target_principal)`: generate a token and bind it to a principal.
- `DeleteToken(token)`: delete a token binding.
- `ListTokens()`: list token bindings.

Deployments must protect the admin port with network isolation or an external
authorization layer.

## 6. Compatibility Guidance

- Treat `payload` as opaque bytes at the OpenEvent layer.
- Put business schema rules in the protocol used by the channel, not in
  OpenEvent.
- Use new channel protocol names, such as `im.v2`, for breaking payload changes.
- Consumers should record processed `seq` and resume from the next expected seq
  after restarts or disconnects.
