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

For instructions on choosing a Python environment and a package source, see
[Build and Test in the README](../README.md#build-and-test).

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
for non-streaming RPCs. On construction or reassignment, the value must be a
number greater than zero and at most 2,147,483,647 milliseconds (about 24.9 days).
Booleans and other invalid values raise `ValueError`.
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

### Use generated stubs through a TLS proxy

When creating a gRPC channel, set both the send and receive message limits to 64 MiB.
The default receive limit is 4 MiB. A 4 MiB object exceeds it once protobuf adds its fields.
This example reads a whole object using an existing object key, `key`. Replace the example
address with the address of your deployment's TLS proxy.

```python
import grpc
from openevent.sdk.proto import openevent_pb2, openevent_pb2_grpc

max_message_bytes = 64 * 1024 * 1024
options = (
    ("grpc.max_send_message_length", max_message_bytes),
    ("grpc.max_receive_message_length", max_message_bytes),
)
with grpc.secure_channel(
    "events.example.com:443", grpc.ssl_channel_credentials(), options=options
) as channel:
    objects = openevent_pb2_grpc.ObjectStorageServiceStub(channel)
    response = objects.ReadObject(
        openevent_pb2.ReadObjectRequest(
            object_id=key.object_id,
            object_token=key.object_token,
            offset=0,
            nbytes=4 * 1024 * 1024,
        ),
        timeout=30,
    )
```

For `Subscribe` on a custom channel, also configure keepalive to match the server.
See the [Subscribe API](API.md#37-subscribe).

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

For CAS-style publishing, the client can query status first and then call
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

`payload` uses bytes. If you do not receive a publish response, retry the same message
with the original `uuid`. If the retry returns `ALREADY_EXISTS`, call
`client.get_seq_by_uuid(uuid)` to get the committed message's sequence number.
See [Handling an Uncertain Publish Result](API.md#29-handling-an-uncertain-publish-result).

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

uuid = client.get_uuid()
published = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=b"report ready",
    object_keys=[key],
    uuid=uuid,
)
```

Use the ObjectKey to read the entire object in one call. The maximum object size is
4 MiB. Requesting that many bytes from the start returns the object's actual contents.

```python
data = client.read_object(
    object_id=key.object_id,
    object_token=key.object_token,
    offset=0,
    nbytes=4 * 1024 * 1024,
).data
```

Call `get_object_metadata` when you need the name, type, description, or a separate size lookup.
Object limits, reference authorization, and errors are defined by the
[object API](API.md#4-objectstorageservice)
and [ObjectKey rules](API.md#27-object-capabilities-and-message-references).

`batch_get_object_metadata(object_keys: Iterable[ObjectKey])` queries an existing
list of object references in one call. For example, `message` is an already read
message containing object references:

```python
keys = message.object_keys
batch = client.batch_get_object_metadata(keys)
for key, metadata in zip(keys, batch.metadata):
    print(key.object_id, metadata.name, metadata.nbytes)
```

The method makes one RPC, uses `timeout_ms`, and returns the original
`BatchGetObjectMetadataResponse`. Its `metadata` entries are `ObjectMetadata`
messages and correspond to `keys` by position, including duplicates. If any entry
fails, the whole call raises `grpc.RpcError` without returning partial results.
The SDK does not split batches,
deduplicate, cache, or retry automatically. See the
[batch metadata API](API.md#44-batchgetobjectmetadata) for count limits, parameters,
and errors.

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

The subscription iterator keeps its client alive. A function can create a client and return
just the iterator, and the caller can continue receiving messages. Calling `client.close()`
or leaving the client's `with` block cancels all active subscriptions on that client.

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

To discover Channels continuously from the start of history, call
`client.subscribe(principal, token, from_seq=0, channels=[], only_my_recipient=False)`.
Handle system messages in a single loop. The subscription delivers visible
historical messages and waits for new messages at the tail.

If the application needs to know when it has processed the messages that existed
at startup, it can read pages with `fetch` up to the `last_seq` reported on the
first page, then subscribe from the following sequence number.

The application maintains its Channel list and recovery progress.
See the [system protocol](SYSTEM_PROTOCOL.md) for message contents and
[Discovering Channels Through Messages](API.md#38-discovering-channels-through-messages)
for pagination, handoff, and permission rules.

## Targeted Messages

You can pass `recipients` when publishing. This example uses members 1002 and 1003,
who were added in the Create a Channel example:

```python
uuid = client.get_uuid()
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"meeting reminder",
    uuid=uuid,
)
```

The Channel's read permissions determine who can read the reminder. This Channel
is public, so every authenticated user can read it. With `only_my_recipient=True`,
only users 1002 and 1003 see it in their filtered results.
See [Channel ACL](API.md#25-channel-acl) and [Recipients](API.md#26-recipients).

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

uuid = client.get_uuid()
try:
    client.publish_auto_seq(principal, token, channel_id, b"hello", uuid=uuid)
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
