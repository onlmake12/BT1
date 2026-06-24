Audit Report

## Title
Attacker-Controlled Snappy Uncompressed-Length Varint Triggers Up-to-8 MB Allocation Per P2P Frame Before Decompression Validation — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode()` reads the snappy uncompressed-length varint from an attacker-controlled frame and allocates up to `MAX_UNCOMPRESSED_LEN` (8 MB) via `BytesMut::zeroed(decompressed_bytes_len)` before attempting decompression. There is no check that the declared uncompressed size is plausible relative to the actual compressed payload size. Any unauthenticated TCP peer that completes the Tentacle handshake can send a ~9-byte frame that forces an ~8 MB allocation on the receiving node, and can repeat this at wire speed to cause severe memory pressure or OOM-kill.

## Finding Description
The vulnerable path is in `LengthDelimitedCodecWithCompress::decode()`:

1. `self.length_delimited.decode(src)` accepts any frame up to `max_frame_length` (e.g. 4 MB for RelayV3, 2 MB for Sync). A 5-byte payload `[0x80, 0xFF, 0xFF, 0xFF, 0x03]` is well within every protocol's limit.
2. `data[0] & COMPRESS_FLAG != 0` is true (byte 0 = `0x80`).
3. `decompress_len(&data[1..])` reads the snappy uncompressed-length varint from the attacker-supplied bytes. The varint `0xFF 0xFF 0xFF 0x03` encodes `8388607` (≈ 8 MB − 1). `decompress_len` does not validate the remainder of the stream; it returns `Ok(8388607)`.
4. The only guard is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (line 235). `8388607 < 8388608`, so this check passes.
5. `BytesMut::zeroed(8388607)` (line 242) allocates ~8 MB of zeroed memory.
6. `SnapDecoder::new().decompress(&data[1..], &mut buf)` fails because the 4-byte payload is not a valid snappy stream, returning an error. The buffer is dropped — but the allocation already occurred.

There is no guard of the form `decompressed_bytes_len ≤ f(data[1..].len())`. Snappy's maximum expansion ratio is ~1.164×, so a 4-byte compressed payload cannot produce 8 MB of output; this cross-check is entirely absent.

The codec is installed for all protocols built via `CKBProtocol::new_with_support_protocol` with `compress: true` (the default), covering Sync (2 MB frame limit), RelayV3 (4 MB), LightClient (2 MB), Filter (2 MB), and others.

## Impact Explanation
An unauthenticated peer can pipeline thousands of such frames per second over a single TCP connection. Each frame causes an ~8 MB allocation followed by an immediate free. Because the system allocator (jemalloc or glibc) does not immediately return freed memory to the OS, RSS grows rapidly under sustained load. With multiple concurrent connections each sending at wire speed, the node's heap can be exhausted, triggering an OOM-kill and a full node crash. Even short of OOM, the allocation/zeroing/free cycle at high rate causes severe CPU and memory-bus pressure, degrading block processing, transaction relay, and RPC responsiveness. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation
- No authentication is required beyond completing the Tentacle handshake, which is open to any TCP peer.
- The malicious frame is 9 bytes on the wire (4-byte length prefix + 5-byte payload) and requires no knowledge of CKB internals.
- The attack is repeatable at wire speed with no per-message rate limiting at the codec layer.
- Multiple protocols are affected, so the attacker can open several sub-streams simultaneously to multiply the allocation rate.

## Recommendation
Before allocating the decompression buffer, validate that the declared uncompressed size is consistent with the compressed payload size. Snappy's worst-case expansion is bounded, so a conservative guard is:

```rust
// In LengthDelimitedCodecWithCompress::decode, after the MAX_UNCOMPRESSED_LEN check:
const MAX_SNAPPY_EXPANSION: usize = 2; // conservative; actual max is ~1.164x
if decompressed_bytes_len > data[1..].len().saturating_mul(MAX_SNAPPY_EXPANSION)
    && decompressed_bytes_len > COMPRESSION_SIZE_THRESHOLD
{
    return Err(io::ErrorKind::InvalidData.into());
}
```

Apply the same guard in `Message::decompress()` (line 81). Additionally, consider lowering `MAX_UNCOMPRESSED_LEN` to match the actual maximum uncompressed protocol message size per protocol rather than using a single global 8 MB ceiling.

## Proof of Concept
```python
import socket, struct

def encode_varint(n):
    buf = []
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)
    return bytes(buf)

# Craft frame: COMPRESS_FLAG + varint(8388607)
payload = b'\x80' + encode_varint(8388607)  # 5 bytes total
# LengthDelimitedCodec 4-byte big-endian length prefix
frame = struct.pack('>I', len(payload)) + payload  # 9 bytes total

conn = socket.create_connection(('TARGET_IP', 8115))
# ... complete Tentacle/secio handshake and open Sync or RelayV3 sub-stream ...
for _ in range(10000):
    conn.sendall(frame)
# Each iteration: BytesMut::zeroed(8388607) allocated then freed on the target node
# 10,000 frames = 10,000 × 8 MB allocation/free cycles at wire speed
```

A unit test can confirm the allocation by instrumenting `BytesMut::zeroed` or by running the `decode` method directly with the crafted 5-byte payload and observing that it reaches line 242 before returning an error.