# OpenEvent SDK Usage Guide

[中文版](USAGE_cn.md)

This document explains how community users can install and use `openevent-sdk`
in Python projects. Protocol fields and boundary semantics are defined by
[the API contract](API.md).

## Install

Build and install from this repository:

```bash
make install
```

See [Build and Test in the README](../README.md#build-and-test) for Python environment selection
and installation arguments.

## Connect to the Service

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` is the gRPC service address. `OpenEventClient` and `AdminClient` create
and manage plaintext gRPC connections with a 64 MiB limit per sent or received
message. Use the generated gRPC stubs for TLS or custom connection options.

Both clients accept `timeout_ms`, defaulting to 30000 milliseconds (30 seconds),
for non-streaming RPCs. Construction and reassignment require a finite number
greater than zero, excluding booleans; invalid values raise `ValueError`.
`timeout_ms` controls how long an ordinary RPC waits for its result, not the
subscription lifetime or connection keepalive.
Subscriptions continue until cancelled, ended by the server, or disconnected.

`OpenEventClient` enables gRPC keepalive for subscriptions; `AdminClient` uses
gRPC's default keepalive settings. A connection failure ends the subscription
stream and invokes registered completion callbacks; see the
[Subscribe API](API.md#37-subscribe).

Call `client.close()` after use, or use a context manager. `close()` is idempotent:
it closes the connection and cancels pending RPCs.

```python
with OpenEventClient("127.0.0.1:9527") as scoped_client:
    status = scoped_client.get_status(principal, token)
```

### RPC Return Values

| Method | Return value |
| --- | --- |
| `get_uuid()` | A Python `int` containing one allocated UUID |
| `get_seq_by_uuid(uuid)` | A Python `int` containing the committed message seq for that UUID |
| `subscribe(...)` | A gRPC-compatible stream iterator yielding `SubscribeResponse` values |
| Other business and admin RPC wrapper methods | The original protobuf response, including RPCs with empty responses |

The SDK returns messages unchanged, without parsing or filtering system messages.
Ordinary Channels also contain system messages: callers first identify messages
with `message.principal == 0` and handle them according to the
[system protocol](SYSTEM_PROTOCOL.md), then parse other messages according to
the Channel's application protocol.

## Create a Channel

```python
from openevent.sdk.proto import openevent_pb2

created = client.create_channel(
    principal=principal,
    token=token,
    name="general",
    visibility=openevent_pb2.VISIBILITY_PUBLIC,
    protocol="text/plain",
    description="Community discussion",
    members=[1002, 1003],
)

channel_id = created.channel.channel_id
```

See [Channel ACL](API.md#25-channel-acl) for `visibility` and member permissions,
and [the system protocol](SYSTEM_PROTOCOL.md) for initialization messages written
when Channels are created, membership-change messages, and the reserved system
Channel. An ordinary Channel's `protocol` still identifies its application-message
protocol.

## Publish Messages

Call `get_uuid()` before each logical publish and pass the UUID explicitly when
publishing. The caller handles retries.

The simplest publish lets the server assign the global `seq`:

```python
payload = "hello OpenEvent".encode("utf-8")
uuid = client.get_uuid()

result = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=payload,
    uuid=uuid,
)

print(result.seq)
```

If the client needs CAS-style publishing, query status first and then call
`publish`:

```python
status = client.get_status(principal, token)
next_seq = status.max_seq + 1
uuid = client.get_uuid()

client.publish(
    principal=principal,
    token=token,
    channel_id=channel_id,
    seq=next_seq,
    payload=b"message with expected seq",
    uuid=uuid,
)
```

`payload` uses bytes. If a publish result is uncertain, retain the original UUID and follow the
[recovery procedure](API.md#29-handling-an-uncertain-publish-result).
Use `client.get_seq_by_uuid(uuid)` to look up the committed message position.

## Write and Reference Objects

The high-level client exposes object storage methods and accepts `object_keys`
when publishing.

```python
from openevent.sdk.proto import openevent_pb2

written = client.write_object(
    principal=principal,
    token=token,
    name="report.json",
    type="application/json",
    description="Agent output",
    data=b'{"result":"ok"}',
)
key = openevent_pb2.ObjectKey(
    object_id=written.object_id,
    object_token=written.object_token,
)

published = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=b"report ready",
    object_keys=[key],
    uuid=client.get_uuid(),
)
```

Use the ObjectKey to authorize metadata and byte-range reads:

```python
metadata = client.get_object_metadata(
    object_id=key.object_id,
    object_token=key.object_token,
)
chunk = client.read_object(
    object_id=key.object_id,
    object_token=key.object_token,
    offset=0,
    nbytes=metadata.nbytes,
)
```

Object limits, reference authorization, and errors are defined by the
[object API](API.md#4-objectstorageservice)
and [ObjectKey rules](API.md#27-object-capabilities-and-message-references).

## Fetch Messages

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=0,
    limit=100,
    channels=[channel_id],
)

for message in response.messages:
    print(message.seq, message.channel_id, message.payload)

next_seq = response.next_seq
last_seq = response.last_seq
```

The example reads only the selected Channel, including its system and application
messages; it does not read Channel 0. Use the response's `next_seq` as the next
`from_seq` to continue;
use `next_seq > last_seq` to determine whether the tail observed by this page was
reached. Full filtering, pagination, and error rules are in
the [Fetch API](API.md#36-fetch).

## Subscribe to Messages

`subscribe` returns a stream-compatible iterator. It provides `cancel()` and
`wait_started(timeout_ms)`, while applying the SDK sequence check described below:

```python
for item in client.subscribe(
    principal,
    token,
    from_seq=0,
    channels=[channel_id],
):
    message = item.message
    print(message.seq, message.payload)
```

`wait_started(timeout_ms)` waits for the acceptance marker defined by the
[Subscribe API](API.md#37-subscribe) in the initial metadata. It returns `None`
on success and remembers that this stream was accepted. Even if the stream has
already ended before the first call, the method succeeds when its initial
metadata contains the marker; subsequent calls also succeed. It does not
guarantee that the stream is still open. Obtain errors after acceptance through
iteration or `code()`.

Without the acceptance marker, the SDK waits for the final gRPC status. A non-`OK`
status propagates the call's original `grpc.RpcError`; `OK` raises `RuntimeError`
because the subscription ended without confirmation. The argument follows the
client's `timeout_ms` validation and applies only to this establishment wait.
A timeout cancels the stream and raises `TimeoutError`, without setting a
deadline for the entire stream.

Subscriptions wait for new messages at the tail. Starting positions, permission
changes, and reconnection are defined in the [Subscribe API](API.md#37-subscribe).

The high-level `OpenEventClient.subscribe` iterator requires strictly increasing
message `seq` values within one stream. The first seq may be 0, and gaps caused by
filtering are valid. A repeated or backward seq violates the stream protocol:
the SDK cancels that stream and raises `RuntimeError` with the previous and
received sequence values.
Each subscription checks its sequence independently. Save the last processed seq
and resubscribe according to the API resume rules.

## Discover Channels Through Messages

Applications can rebuild their Channel lists from historical messages and then
discover new Channels through subscriptions, without periodically calling
`ListChannels`. At startup, use `get_status` to record message watermark `H`, then
read from `from_seq=0` through that watermark using
`fetch(channels=[], only_my_recipient=False)`, handling system events and restoring
application state. Continue receiving currently readable messages with
`subscribe(from_seq=H+1, channels=[], only_my_recipient=False)`.

See the [system protocol](SYSTEM_PROTOCOL.md) for message contents and
[Discovering Channels Through Messages](API.md#38-discovering-channels-through-messages)
for the full reading, handoff, and permission rules. The SDK does not maintain
Channel lists or recovery progress.

## Targeted Messages

Pass `recipients` when publishing. This example uses members 1002 and 1003,
who were added in the Create a Channel example:

```python
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"private note for selected members",
    uuid=client.get_uuid(),
)
```

Set `only_my_recipient=True` when reading to select messages targeted to you.
Membership requirements and filtering semantics are defined in the
[Recipients API](API.md#26-recipients).

## Manage Tokens

`AdminClient` accesses `AdminService`, which runs only on a separate admin port.

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

token = admin.add_token(principal=1001).token

admin.delete_token(principal=1001, token=token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

The example obtains a token for principal 1001, deletes that `(principal, token)`
pair, and reads one administrative message page. Continue message pages with
`next_seq`. Credential pairs, parameters, and deletion are defined in
the [AdminService API](API.md#6-adminservice).
Protect the admin endpoint when deploying.

## Error Handling

The SDK exposes gRPC call errors directly. Callers should catch `grpc.RpcError`
and branch on the status code:

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, b"hello", uuid=client.get_uuid())
except grpc.RpcError as exc:
    if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
        print("invalid token")
    elif exc.code() == grpc.StatusCode.PERMISSION_DENIED:
        print("no permission")
    else:
        raise
```

See [the API contract](API.md) for common status codes and complete semantics.

## Local Development

Commands for proto generation, building, installation, testing, and cleanup are
maintained in [README: Build and Test](../README.md#build-and-test).
