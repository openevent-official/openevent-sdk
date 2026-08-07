# OpenEvent SDK Usage Guide

[中文版](USAGE_cn.md)

This document explains how community users can install and use `openevent-sdk`
in Python projects. Protocol fields and boundary semantics are defined by
[the API contract](API.md).

## Install

Build a wheel from this repository:

```bash
make build
python3 -m pip install dist/openevent_sdk-0.4.4-py3-none-any.whl
```

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

`target` is the gRPC service address. By default the SDK uses
`grpc.insecure_channel`. If your deployment requires TLS or a custom channel,
create a `grpc.Channel` and pass it with `OpenEventClient(target, channel=...)`.

`OpenEventClient` and `AdminClient` accept a constructor `timeout` with a
30-second default. It must be a finite number greater than zero; `None`, boolean,
zero, negative, NaN, and infinite values raise `ValueError`. The same deadline is
used by every RPC, including the complete lifetime of a `subscribe` stream, so
high-level client calls cannot wait forever.

Call `client.close()` when the client is no longer needed, or use it as a
context manager. `close()` is idempotent. A client closes only the gRPC channel
it created itself; a channel passed with `channel=...` remains owned by the
caller and must be closed by the caller. The destructor is only a fallback for
an omitted explicit close because Python does not guarantee when it runs.

```python
with OpenEventClient("127.0.0.1:9527") as scoped_client:
    status = scoped_client.get_status(principal, token)
```

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
)

channel_id = created.channel.channel_id
```

Visibility:

- `VISIBILITY_PUBLIC`: all authenticated users can read and write.
- `VISIBILITY_PROTECTED`: all authenticated users can read; members can write.
- `VISIBILITY_PRIVATE`: members can read and write.

## Publish Messages

The simplest method is to let the server assign a global `seq`:

```python
payload = "hello OpenEvent".encode("utf-8")

result = client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    payload=payload,
)

print(result.seq)
```

If the client needs CAS-style publishing, query status first and then call
`publish`:

```python
status = client.get_status(principal, token)
next_seq = status.max_seq + 1

client.publish(
    principal=principal,
    token=token,
    channel_id=channel_id,
    seq=next_seq,
    payload=b"message with expected seq",
)
```

`payload` is an opaque byte array. You can encode it as JSON, MessagePack,
custom binary, or plain text.

SDK-created gRPC channels set the per-message send and receive limits to 64 MiB
to accommodate the default 16 MiB payload limit and paginated responses. Callers
that inject a custom channel must configure sufficient
`grpc.max_send_message_length` and `grpc.max_receive_message_length` values.

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
object tokens. See sections 2.6 and 4 of [API.md](API.md) for exact limits, range
behavior, errors, and uncertain WriteObject results.

## Fetch Messages

```python
response = client.fetch(
    principal=principal,
    token=token,
    from_seq=1,
    limit=100,
    channels=[channel_id],
)

for message in response.messages:
    print(message.seq, message.channel_id, message.payload)

next_seq = response.next_seq
last_seq = response.last_seq
```

`response.messages` contains `EventMessage` values. When `channels=[...]` is
provided, every listed Channel must exist and be readable by the caller; any
failed check rejects the whole request. After validation, only selected channels
are fetched. Omit it or pass an empty list to fetch all channels visible to the
caller. Use the previous response's `next_seq` as the next
`from_seq` to continue reading. If `next_seq <= last_seq`, another fetch may scan
more committed messages; if `next_seq > last_seq`, the response has reached the
current committed tail.

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

`subscribe` returns a stream-compatible iterator. It preserves gRPC call methods
such as `cancel()` while applying the SDK sequence check described below:

```python
for item in client.subscribe(
    principal,
    token,
    from_seq=0,
    channels=[channel_id],
):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` means wait for messages published after the subscription is
established. When `channels=[...]` is provided, every listed Channel must exist
and be readable by the caller; any failed check ends the stream with an error.
After validation, only readable messages from those Channels are pushed. Omit it
or pass an empty list to disable Channel filtering. If an explicitly listed
Channel becomes unreadable while the stream is running, a later batch ends the
stream with `PERMISSION_DENIED`. The stream ends with `DEADLINE_EXCEEDED` when
the client timeout expires. Record the last processed `seq` and resume from
`seq + 1` after any interruption.

When the server begins normal or fatal shutdown, it actively ends every
established stream. Treat this like any other interruption: record the last
processed `seq` and resume from `seq + 1` using Subscribe or Fetch.

The high-level `OpenEventClient.subscribe` iterator suppresses repeated or
backward message `seq` values within one stream. Persist the last processed `seq`
across reconnects and resume from `seq + 1`; the generated gRPC stub remains
available when the raw stream is required.

## Targeted Messages

Pass `recipients` when publishing:

```python
client.publish_auto_seq(
    principal=principal,
    token=token,
    channel_id=channel_id,
    recipients=[1002, 1003],
    payload=b"private note for selected members",
)
```

Each recipient principal must be a member of the channel. When reading with
`only_my_recipient=True`, the server returns only messages whose `recipients`
include the current `principal`.

## Manage Tokens

`AdminClient` accesses `AdminService`. This service usually listens on a
separate admin port; whether it is exposed externally depends on deployment
configuration.

```python
from openevent.sdk import AdminClient

admin = AdminClient("127.0.0.1:9528")

binding = admin.add_token(target_principal=1001).binding
print("created token for principal:", binding.principal)

page_token = ""
while True:
    token_page = admin.list_tokens(page_token=page_token, limit=100)
    print("token bindings in this page:", len(token_page.bindings))
    if not token_page.next_page_token:
        break
    page_token = token_page.next_page_token

admin.delete_token(binding.token)

message_page = admin.list_messages(from_seq=0, limit=100)
for message in message_page.messages:
    print(message.seq, message.channel_id, message.payload)

```

`binding.token`, tokens returned by `ListTokens`, and page tokens are sensitive
credentials. The example only keeps them in memory; do not write them to logs,
traces, error details, or ordinary console output.

`ListTokens` uses opaque `page_token` values; `next_page_token=""` marks the end.
Because tokens are mutable, pages are not a strong cross-RPC snapshot.
`ListMessages` is an administrative query and does not apply business Channel
ACL filtering. Use `next_seq` as the next page's inclusive cursor. Protect the
admin endpoint accordingly.

## Error Handling

The SDK exposes gRPC call errors directly. Callers should catch `grpc.RpcError`
and branch on the status code:

```python
import grpc

try:
    client.publish_auto_seq(principal, token, channel_id, b"hello")
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

Common commands:

```bash
make init
make test
make build
make clean
```

`make init` generates Python protobuf code under
[`src/openevent/sdk/proto/`](../src/openevent/sdk/proto/).
Generated `openevent_pb2*.py` and `admin_pb2*.py` files are not tracked by Git,
but are included in the wheel.

`make build` uses Hatchling to build a wheel. Build dependencies, test
dependencies, caches, and temporary files are placed under `build/`, and final
artifacts are placed under `dist/`.
