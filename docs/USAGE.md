# OpenEvent SDK Usage Guide

[中文版](USAGE_cn.md)

This document explains how community users can install and use `openevent-sdk`
in Python projects. Protocol fields and boundary semantics are defined by
[the API contract](API.md).

## Install

Build a wheel from this repository:

```bash
make build
python3 -m pip install dist/openevent_sdk-0.4.0-py3-none-any.whl
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

`OpenEventClient` and `AdminClient` accept an optional constructor `timeout`
with a 30-second default. The value is used by every non-streaming RPC; pass
`None` at construction to disable those deadlines. `subscribe` is a long-lived
stream and does not use the client timeout.

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

### Reconciling an Uncertain Publish Failure

If Publish or PublishAutoSeq ends with a broken connection, cancellation,
`DEADLINE_EXCEEDED`, or some `UNAVAILABLE` failures, the server may already have
committed the message. Do not retry immediately: PublishAutoSeq could create a
duplicate, while Publish may return `ABORTED` because the original seq committed.

Only `UNAUTHENTICATED`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_ARGUMENT`,
`RESOURCE_EXHAUSTED`, and `ABORTED` for Publish seq-CAS conflicts guarantee that
this publish was not committed. Treat every other non-`OK` status, or the absence
of a gRPC status, as uncertain. A non-commit guarantee does not imply that the
cause is recoverable; see section 2.7 of [API.md](API.md) for the full contract.

Recommended flow:

1. Before publishing, record `get_status(...).max_seq` and include a unique event
   ID in the payload's application protocol.
2. On an uncertain result, call GetStatus again and record
   `reconcile_max_seq`. If the original publish committed, this watermark must
   include it.
3. Fetch from the previous `max_seq + 1`, following `next_seq` until the scan
   passes `reconcile_max_seq`. Subscribe can help find a matching message quickly,
   but cannot by itself prove absence because filtered seq values do not produce
   progress responses.
4. For Publish, inspect the requested seq. For PublishAutoSeq, search the scanned
   range for the same event ID.
5. If found, treat the operation as successful and use the message seq. Only
   after confirming it is absent should the application decide whether to publish
   again.

The server provides one linearized order for mutations and non-streaming stateful
reads. A GetStatus, Fetch, or other non-streaming read issued after a write RPC
returned success must observe the complete write. Subscribe is excluded and does
not replace GetStatus/Fetch for watermark-based reconciliation.

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

`response.messages` contains `EventMessage` values. Pass `channels=[...]` to
fetch only selected channels; omit it or pass an empty list to fetch all channels
visible to the caller. Use the previous response's `next_seq` as the next
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
for item in client.subscribe(principal, token, from_seq=0):
    if item.HasField("message"):
        message = item.message
        print(message.seq, message.payload)
    else:
        print("next seq:", item.next_seq)
```

`from_seq=0` means wait for messages published after the subscription is
established. If the connection is interrupted, the client should record the last
processed `seq` and resume from `seq + 1`.

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
