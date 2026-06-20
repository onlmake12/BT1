### Title
Snappy Decompression Buffer Pre-Allocation Amplification DoS — (`network/src/compress.rs`)

---

### Summary

In `LengthDelimitedCodecWithCompress::decode` and `Message::decompress`, a near-8 MB heap buffer is unconditionally allocated based solely on the attacker-controlled snappy stream header value returned by `decompress_len()`, **before** actual decompression is attempted. An unprivileged remote peer can craft a minimal compressed frame (a few bytes) whose snappy header claims `MAX_UNCOMPRESSED_LEN - 1` (8 MB − 1 byte) decompressed size, triggering an ~8 MB allocation per message. Multiplied across many concurrent sessions, this exhausts node heap memory and prevents legitimate block/header buffer allocation.

---

### Finding Description

**Entrypoint**: Any open P2P protocol session. Sync (`max_frame_length` = 2 MB) and RelayV3 (`max_frame_length` = 4 MB) are the highest-bandwidth targets.

**Codec path** (`network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`):

1. `self.length_delimited.decode(src)?` enforces `max_frame_length` on the **compressed** wire frame. [1](#0-0) 

2. If the compress flag byte is set, `decompress_len(&data[1..])` reads the uncompressed length from the snappy varint header — a fully attacker-controlled field. [2](#0-1) 

3. The guard only rejects values **strictly greater than** `MAX_UNCOMPRESSED_LEN` (8 MB). A value of `8 MB − 1` passes. [3](#0-2) 

4. **The allocation occurs unconditionally before decompression**: `BytesMut::zeroed(decompressed_bytes_len)` allocates up to ~8 MB. [4](#0-3) 

5. Only then is `SnapDecoder::new().decompress()` called. If the payload is malformed, the error is returned and the buffer is dropped — but the peak allocation already occurred. [5](#0-4) 

The identical pattern exists in `Message::decompress` at line 81 (`vec![0; decompressed_bytes_len]`). [6](#0-5) 

**Protocol frame limits** confirm the amplification ratio:

| Protocol | `max_frame_length` (compressed) | Max allocation (decompressed) | Amplification |
|---|---|---|---|
| Sync | 2 MB | ~8 MB | ~4× |
| RelayV3 | 4 MB | ~8 MB | ~2× | [7](#0-6) 

The `MAX_UNCOMPRESSED_LEN` constant is `1 << 23` = 8,388,608 bytes. [8](#0-7) 

---

### Impact Explanation

With N concurrent inbound peers (CKB default allows ~125), each sending one crafted frame simultaneously, peak heap pressure reaches `N × ~8 MB`. At 125 peers: ~1 GB of transient allocation. Because the allocations occur in the codec decode path (synchronous within the async I/O loop), they can pile up faster than they are freed. This starves the allocator for legitimate block/header processing buffers, causing the node to stall or OOM-crash, preventing it from tracking the chain tip — a loss of availability equivalent to consensus exclusion.

---

### Likelihood Explanation

- No authentication or privilege required; any peer that completes a TCP handshake and opens a Sync or RelayV3 session can send this frame.
- The crafted frame is trivial: a 1-byte compress flag + a snappy varint header encoding `8 MB − 1`, followed by a few garbage bytes. Total wire size: ~10 bytes, well within `max_frame_length`.
- No PoW, no key material, no Sybil majority needed.
- The attack is repeatable: after the connection is closed on error, the attacker reconnects and repeats.

---

### Recommendation

Move the allocation **after** a successful decompression, or use a streaming/bounded decompressor that does not require pre-allocating the full claimed size. Concretely:

- Replace `BytesMut::zeroed(decompressed_bytes_len)` with a capacity-bounded allocation that grows only as actual decompressed bytes arrive.
- Alternatively, add a secondary guard: `decompressed_bytes_len > compressed_data.len() * MAX_EXPANSION_RATIO` to reject implausible expansion ratios before allocating.
- Apply the same fix to `Message::decompress` (line 81). [9](#0-8) 

---

### Proof of Concept

```python
import socket, struct

# Snappy stream: varint encoding of (8MB - 1) = 0x7FFFFF
# Snappy uncompressed length varint for 8388607:
# 8388607 = 0x7FFFFF -> little-endian base-128: 0xFF 0xFF 0xFF 0x03
snappy_header = b'\xff\xff\xff\x03'  # claims 8MB-1 decompressed
garbage_literal = b'\x00' * 4        # invalid snappy literal block

compress_flag = b'\x80'              # COMPRESS_FLAG
payload = compress_flag + snappy_header + garbage_literal

# 4-byte big-endian length prefix (LengthDelimitedCodec, 4-byte header)
frame = struct.pack('>I', len(payload)) + payload

# Open N concurrent Sync protocol sessions and send frame
for _ in range(125):
    s = socket.create_connection(('target_node', 8115))
    # ... complete tentacle/yamux/protocol handshake for /ckb/syn ...
    s.sendall(frame)
    # Each triggers BytesMut::zeroed(8_388_607) before failing
```

Each connection causes an ~8 MB allocation in `LengthDelimitedCodecWithCompress::decode` at `network/src/compress.rs:242` before the decompression error is returned. [10](#0-9)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L81-83)
```rust
                        let mut buf = vec![0; decompressed_bytes_len];
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
                            Ok(_) => Ok(buf.into()),
```

**File:** network/src/compress.rs (L226-227)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
```

**File:** network/src/compress.rs (L233-234)
```rust
                    match decompress_len(&data[1..]) {
                        Ok(decompressed_bytes_len) => {
```

**File:** network/src/compress.rs (L235-241)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
                            }
```

**File:** network/src/compress.rs (L242-249)
```rust
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
                            }
```

**File:** network/src/protocols/support_protocols.rs (L129-130)
```rust
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```
