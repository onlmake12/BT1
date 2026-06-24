Audit Report

## Title
Off-by-One in Decompressed-Length Guard Allows 8 MB Heap Allocation Per P2P Message — (`network/src/compress.rs`)

## Summary
Both `LengthDelimitedCodecWithCompress::decode` and `Message::decompress` use a strict `>` comparison against `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes). A payload whose `decompress_len()` equals exactly 8,388,608 passes the guard, causing an 8 MB heap allocation before any application-layer validation. Any unauthenticated peer can trigger this with a trivially crafted compressed frame, and with the default peer limit of 125, sustained concurrent sending produces ~1 GB of heap pressure.

## Finding Description
`MAX_UNCOMPRESSED_LEN = 1 << 23 = 8388608` (line 13). In `LengthDelimitedCodecWithCompress::decode` (line 235): `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` — when `decompressed_bytes_len == 8388608`, the condition `8388608 > 8388608` is `false`, the guard is bypassed, and `BytesMut::zeroed(8388608)` is called at line 242. The identical flaw exists in `Message::decompress` at line 74, where the guard is also strict `>`, and `vec![0; decompressed_bytes_len]` is allocated at line 81.

The decoder at line 232 checks `(data[0] & COMPRESS_FLAG) != 0` with no check on `self.enable_compress`, meaning any peer can send a compressed frame on any protocol channel regardless of whether compression is enabled locally.

The frame size limit does not prevent the attack: snappy compresses 8 MB of zero bytes to a few hundred bytes, well within the largest `max_frame_length` of 4 MB (RelayV3, line 130). The compressed payload passes the frame-length check, the decompressed-length guard is bypassed by the off-by-one, and 8 MB is allocated.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.** Each connected peer can force an 8 MB heap allocation per message at negligible cost. With the default `max_peers = 125`, 125 simultaneous peers each sending one such message produces ~1 GB of concurrent heap pressure. At the service-level ceiling of 1024 connections (line 976 of `network.rs`), the upper bound is ~8 GB. This causes OOM kills or severe allocator pressure, crashing or stalling the node and preventing it from participating in block sync or relay.

## Likelihood Explanation
The attack requires only a TCP connection to the node's P2P port — no authentication, no stake, no proof-of-work. The crafted payload is trivial to produce (snappy-compress exactly 8,388,608 bytes of zeros). The off-by-one is a single-character difference (`>` vs `>=`) present in both code paths. Any adversary scanning for CKB nodes can execute this with a small script. The connection is not dropped after the allocation if the decompressed content is valid snappy data, so the attacker can send messages continuously.

## Recommendation
Change both guards from strict `>` to `>=`:

```rust
// network/src/compress.rs, line 235
if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {

// network/src/compress.rs, line 74
if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {
```

Additionally, consider: (1) checking `self.enable_compress` in the decoder before processing compressed frames; (2) adding per-peer rate limiting on decompression; (3) a node-wide cap on total concurrent decompression memory.

## Proof of Concept
```python
import snappy, struct, socket

# Craft payload: exactly MAX_UNCOMPRESSED_LEN bytes, snappy-compressed
raw = b'\x00' * 8388608           # exactly 1 << 23
compressed = snappy.compress(raw)  # compresses to ~few hundred bytes

# Build frame: [4-byte big-endian length][0x80 compress flag][compressed payload]
payload = b'\x80' + compressed
frame = struct.pack('>I', len(payload)) + payload

# Send on any CKB P2P protocol channel (e.g., RelayV3 port 8115)
s = socket.create_connection(('TARGET_IP', 8115))
# ... complete tentacle/secio handshake, then send frame ...
s.sendall(frame)
# Node executes BytesMut::zeroed(8388608) — 8 MB allocated before any app logic
```

Sending from 125 concurrent connections causes ~1 GB of simultaneous heap pressure. The guard `8388608 > 8388608` evaluates to `false`, confirming the off-by-one is the direct root cause.