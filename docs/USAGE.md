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

For local development, generate protobuf code and use the source tree directly:

```bash
make init
export PYTHONPATH="$PWD/src"
```

## Connect to the Service

```python
from openevent.sdk import OpenEventClient

client = OpenEventClient("127.0.0.1:9527")

principal = 1001
token = "user-token"
```

`target` is the gRPC service address. `OpenEventClient` and `AdminClient` create
`grpc.insecure_channel` instances with fixed transport options and do not accept
a caller-provided channel. Use the generated gRPC stubs directly when TLS or
other custom gRPC connection options are required.

`OpenEventClient` and `AdminClient` accept a constructor `timeout_ms` with a
30000-millisecond (30-second) default. It must be a finite number greater than
zero; `None`, boolean, zero, negative, NaN, and infinite values raise `ValueError`,
both at construction and on reassignment. The SDK converts this value to seconds
for every non-streaming gRPC call. `OpenEventClient.subscribe` deliberately has no RPC
deadline: an idle, healthy subscription remains open until the caller cancels it,
the server ends it, or the gRPC transport fails.

The channel created by `OpenEventClient` uses the documented transport
keepalive policy for Subscribe. This is a transport liveness check, not an
OpenEvent message, so it never appears in a subscription. A broken connection
therefore finishes the subscription (and invokes a done callback, if registered)
without waiting for a per-RPC deadline. The authoritative PING intervals and
compatibility requirements are in the [Subscribe API contract](API.md#37-subscribe).

Call `client.close()` when the client is no longer needed, or use it as a
context manager. `close()` is idempotent and closes the gRPC channel created by
the client, cancelling pending RPCs and discarding unused UUIDs. Closing the
connection does not wait for a UUID allocation request to finish first. The
destructor is only a fallback for an omitted explicit close because Python does
not guarantee when it runs.

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

Visibility:

- `VISIBILITY_PUBLIC`: all authenticated users can read and write.
- `VISIBILITY_PROTECTED`: all authenticated users can read; members can write.
- `VISIBILITY_PRIVATE`: members can read and write.

The reserved system Channel has `channel_id=0`, name `system`, and protocol
`system.v1`. It is server-owned and read-only. Every initialized server contains
its permanent initialization message at `seq=0`; the payload schema is defined
by [the system protocol](SYSTEM_PROTOCOL.md). Business principals must be greater
than zero.

## Publish Messages

Call `get_uuid()` before each logical publish. Each client has its own in-memory
pool. When empty, it calls `AllocateUuids(count=1024)` once and hands out UUIDs in
response order. The pool is neither persisted nor shared across clients; closing
the client or exiting the process discards unused entries. Pass the UUID
explicitly when publishing; the SDK does not fill it in or retry automatically.

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

`payload` is an opaque byte array. You can encode it as JSON, MessagePack,
custom binary, or plain text.

For an uncertain Publish result, retain the UUID and follow the recovery procedure in API contract section 2.9. The
final duplicate-UUID step obtains the committed sequence without fetching history:

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, payload, uuid=uuid)
except grpc.RpcError as exc:
    if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
        raise
    committed_seq = client.get_seq_by_uuid(uuid)
```

`get_seq_by_uuid()` returns the sequence for a UUID consumed by a committed message. Its protocol behavior is defined
in API contract section 3.3.

SDK-created gRPC channels use fixed per-message send and receive limits of
64 MiB. The transport limit needed to read historical messages does not decrease
with the current payload write limit. See the [Payload API](API.md#23-payload).

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

Read metadata and a byte range with the ObjectKey. Object reads do not carry a
principal token because the ObjectKey itself is the authorization:

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

Objects are immutable, nonempty, and limited to 4 MiB. ObjectKeys are permanent,
transferable bearer credentials; attaching one to a message discloses it to every
caller that can read that message, including administrative readers. Do not log
object tokens. See sections 2.7 and 4 of [API.md](API.md) for exact limits, range
behavior, errors, and uncertain WriteObject results.

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

The example reads only the selected application Channel and filters out the
system Channel. Use the response's `next_seq` as the next `from_seq` to continue;
`next_seq > last_seq` means the tail observed by this page was reached. A short or
empty page need not be the end. Full filtering, pagination, and error rules are in
the [Fetch API](API.md#36-fetch).

To fetch only messages targeted to the current principal:

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
    only_my_recipient=True,
)
```

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

`wait_started(timeout_ms)` waits for the server's initial response metadata. It is
useful when a caller must distinguish a subscription accepted by the server from
one that is still trying to connect. `timeout_ms` must be a finite number of
milliseconds greater than zero. On timeout, it cancels this stream and raises
`TimeoutError`.
An RPC error before acceptance is propagated unchanged. A successful return does
not impose a deadline on the established stream and does not wait for an
OpenEvent message.

The example scans the selected application Channel from seq 0 and waits for new
messages at the tail. Starting-sequence boundaries, termination or filtering
after permission changes, and recovery after server shutdown are defined in the
[Subscribe API](API.md#37-subscribe).

The high-level `OpenEventClient.subscribe` iterator suppresses repeated or
backward message `seq` values within one stream. The SDK does not reconnect
automatically, and its duplicate watermark does not span connections. Save the
last processed seq and handle replays according to the API resume rules. Use the
generated gRPC stub when the raw stream is required.

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

binding = admin.add_token(target_principal=1001).binding
print("created token for principal:", binding.principal)

admin.delete_token(binding.token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

The example creates and deletes a token, then reads one administrative message
page. Continue message pages with `next_seq`. Query contracts are defined in
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
