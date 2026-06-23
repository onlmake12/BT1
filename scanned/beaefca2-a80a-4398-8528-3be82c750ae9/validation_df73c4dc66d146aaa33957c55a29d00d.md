### Title
Snappy Header-Driven Memory Amplification in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

### Summary

In `compress.rs`, the `decode` path allocates a buffer sized from the snappy stream's varint header **before** decompression, with no global cap. An unprivileged remote peer can craft a compressed frame whose snappy varint advertises ~8 MB of decompressed output while the actual compressed payload is minimal (≥ 1025 bytes to pass the compression threshold). Each such frame causes an ~8 MB allocation. With many concurrent peers, this exhausts node memory.

---

### Finding Description

**Root cause — line 242:**

```rust
let mut buf = BytesMut::zeroed(decompressed_bytes_len);
```

The guard at line 235 only rejects values **strictly greater than** `MAX_UNCOMPRESSED_LEN` (8 MB):

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
    return Err(io::ErrorKind::InvalidData.into());
}
```

So `decompressed_bytes_len = MAX_UNCOMPRESSED_LEN` (8,388,608 bytes) passes the check and triggers a full 8 MB `BytesMut::zeroed` allocation. This allocation is driven entirely by the attacker-controlled varint in the snappy stream header, read by `decompress_len(&data[1..])` at line 233, before any actual decompression work is done.

**Compression is enabled by default** for all production protocols via `CKBProtocol::new_with_support_protocol` and `CKBProtocol::new`, both of which set `compress: true`. The `LengthDelimitedCodecWithCompress` codec is wired in via `CKBProtocol::build()`.

**The amplification ratio is unbounded relative to compressed input size.** A snappy stream can legally encode a varint of ~8 MB followed by a tiny literal block (e.g., 1025 bytes of compressed data). The snappy decompressor writes however many bytes the elements produce and returns `Ok(n)` — it does not require the output to exactly match the varint. The code at line 244 returns the full `buf` regardless:

```rust
Ok(_) => Ok(Some(buf)),
```

So the protocol handler receives an 8 MB `BytesMut` even if only a few hundred bytes were actually decompressed.

**Per-protocol frame size limits** (`max_frame_length`) bound the *compressed* frame (e.g., 4 MB for RelayV3, 2 MB for Sync), but do **not** bound the decompressed allocation — that is controlled by the snappy varint, not the frame length.

---

### Impact Explanation

- Each malicious frame: ~8 MB heap allocation (`BytesMut::zeroed`)
- N concurrent peers × back-to-back frames = N × 8 MB concurrent allocations
- No global memory cap exists in the decompression path
- With the default or configured `max_connection_number`, total concurrent allocation can reach tens of gigabytes, causing OOM and process crash

---

### Likelihood Explanation

- Any unprivileged peer that completes a session handshake can send compressed frames
- Compression is enabled by default on all CKB protocols
- Crafting a valid snappy stream with a large varint and minimal payload requires no special privileges — it is a trivial byte-level manipulation
- The attacker only needs to open many connections and send one crafted frame per connection; no sustained bandwidth is required

---

### Recommendation

1. **Bound the decompressed allocation by the actual compressed input size**, not the snappy header varint. A safe upper bound is `min(decompressed_bytes_len, actual_compressed_len * MAX_COMPRESSION_RATIO)`.
2. **Add a global in-flight decompression memory counter** and reject new frames when the limit is exceeded.
3. **Disconnect and ban peers** that send frames where `decompress_len` significantly exceeds the compressed frame size (e.g., ratio > 1024×).
4. Consider replacing `BytesMut::zeroed(decompressed_bytes_len)` with a lazy/streaming decompressor that does not pre-allocate based on the header.

---

### Proof of Concept

```python
import socket, struct

# Craft snappy stream: varint = 8MB-1, followed by a 1-byte literal block
varint = (8 * 1024 * 1024 - 1)
# Encode varint (snappy uses little-endian base-128)
def encode_varint(n):
    buf = []
    while n > 0x7f:
        buf.append((n & 0x7f) | 0x80)
        n >>= 7
    buf.append(n)
    return bytes(buf)

# Minimal snappy stream: varint header + 1-byte literal element
snappy_payload = encode_varint(varint) + b'\x00\x01A'  # literal: 1 byte 'A'

# Frame: compress_flag=0x80, then snappy_payload
frame_body = bytes([0x80]) + snappy_payload
frame = struct.pack('>I', len(frame_body)) + frame_body  # 4-byte length prefix

# Open N connections, send frame on each
for _ in range(125):
    s = socket.socket()
    s.connect(('target', 8115))
    # ... complete secio handshake ...
    s.sendall(frame)
    # Each connection triggers BytesMut::zeroed(8MB) at compress.rs:242
```

Each connection causes an ~8 MB allocation at `compress.rs:242`. With 125 peers, ~1 GB is allocated concurrently. With 1024 peers, ~8 GB.

---

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L233-244)
```rust
                    match decompress_len(&data[1..]) {
                        Ok(decompressed_bytes_len) => {
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
```

**File:** network/src/protocols/mod.rs (L218-220)
```rust
            handler,
            compress: true,
        }
```

**File:** network/src/protocols/mod.rs (L280-288)
```rust
            .codec(move || {
                Box::new(LengthDelimitedCodecWithCompress::new(
                    self.compress,
                    length_delimited::Builder::new()
                        .max_frame_length(max_frame_length)
                        .new_codec(),
                    self.id,
                ))
            })
```

**File:** network/src/protocols/support_protocols.rs (L122-137)
```rust
    pub fn max_frame_length(&self) -> usize {
        match self {
            SupportProtocols::Ping => 1024,                   // 1   KB
            SupportProtocols::Discovery => 512 * 1024,        // 512 KB
            SupportProtocols::Identify => 2 * 1024,           // 2   KB
            SupportProtocols::Feeler => 1024,                 // 1   KB
            SupportProtocols::DisconnectMessage => 1024,      // 1   KB
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
            SupportProtocols::Time => 1024,                   // 1   KB
            SupportProtocols::Alert => 128 * 1024,            // 128 KB
            SupportProtocols::LightClient => 2 * 1024 * 1024, // 2 MB
            SupportProtocols::Filter => 2 * 1024 * 1024,      // 2   MB
            SupportProtocols::HolePunching => 512 * 1024,     // 512 KB
        }
    }
```
