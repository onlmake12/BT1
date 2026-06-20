### Title
Per-Frame Memory Amplification via Crafted Snappy Varint on RelayV3 — (`network/src/compress.rs`)

---

### Summary

An unprivileged remote peer on the RelayV3 protocol can send a compressed frame whose snappy header varint claims exactly `MAX_UNCOMPRESSED_LEN` (8 MB) of decompressed output. The decoder allocates an 8 MB zeroed buffer **before** attempting decompression, while the wire-frame data (up to 4 MB) is simultaneously live in the `src` accumulation buffer. This yields up to **12 MB of peak memory per inbound frame**, a 3× amplification over RelayV3's 4 MB `max_frame_length`. With N concurrent peers each sending such frames, total peak memory is N × 12 MB.

---

### Finding Description

**Constants involved:**

- `MAX_UNCOMPRESSED_LEN = 1 << 23 = 8,388,608` bytes (8 MB) [1](#0-0) 

- RelayV3 `max_frame_length = 4 * 1024 * 1024` (4 MB) [2](#0-1) 

**Decode path:**

In `LengthDelimitedCodecWithCompress::decode`, after the inner `length_delimited` codec returns `Some(data)` (the wire frame, up to 4 MB, still live in `src`), the code reads the snappy varint and applies a **strict** greater-than guard:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // strict: 8_388_608 passes
    return Err(...);
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len);  // allocates up to 8 MB
match SnapDecoder::new().decompress(&data[1..], &mut buf) { ... }
``` [3](#0-2) 

Two problems compound:

1. **Off-by-one guard**: `> MAX_UNCOMPRESSED_LEN` (strict) allows a varint of exactly 8,388,608 to pass. A `>=` check would reject it.
2. **Eager allocation before validation**: `BytesMut::zeroed(decompressed_bytes_len)` is called unconditionally before `decompress()` is attempted. If the compressed payload is malformed, the 8 MB is allocated and then immediately freed — but the peak allocation still occurred.

**Simultaneous live allocations during one `decode()` call:**

| Buffer | Source | Max size |
|---|---|---|
| `src` accumulation (wire frame) | `length_delimited` codec | 4 MB (RelayV3) |
| `buf` (decompression target) | `BytesMut::zeroed(...)` | 8 MB |
| **Total** | | **12 MB** |

A peer does **not** need to send a full 4 MB wire frame to trigger the 8 MB allocation. A minimal snappy-formatted payload (a few bytes) with a varint header claiming 8,388,608 bytes is sufficient to allocate 8 MB per frame.

---

### Impact Explanation

With N concurrent RelayV3 connections each sending one such crafted frame:

- **Peak memory** = N × (wire_frame_size + 8 MB) ≤ N × 12 MB
- At 1,000 concurrent connections: up to **12 GB** peak memory
- The allocation is transient (freed after decompression error), but the connection is also dropped, so the attacker must re-establish connections — however, the CKB default inbound connection limit is in the hundreds, and each connection can fire the frame immediately upon opening

The amplification ratio is up to **80,000×** for a minimal wire frame (10 bytes → 8 MB allocation).

---

### Likelihood Explanation

The attack requires only:
1. Opening a TCP connection to a CKB node
2. Negotiating the RelayV3 (`/ckb/relay3`) protocol
3. Sending a single frame with `COMPRESS_FLAG` set and a snappy varint = 8,388,608

No authentication, PoW, or privileged access is required. The RelayV3 protocol is open to all peers by default. [4](#0-3) [5](#0-4) 

---

### Recommendation

1. **Change the guard to `>=`** so that exactly `MAX_UNCOMPRESSED_LEN` is also rejected:
   ```rust
   if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN { ... }
   ```

2. **Bound decompressed size by `max_frame_length`**, not a global constant. Pass `max_frame_length` into the decoder and use `min(MAX_UNCOMPRESSED_LEN, max_frame_length)` as the cap. For RelayV3 this reduces the cap from 8 MB to 4 MB.

3. **Allocate lazily or validate the snappy stream header** before zeroing the full output buffer, to avoid the eager 8 MB allocation on malformed input.

---

### Proof of Concept

```rust
// Craft a minimal snappy frame claiming 8_388_608 bytes decompressed
// Snappy uncompressed-length varint encoding of 8_388_608 = [0x80, 0x80, 0x80, 0x04]
let mut frame = BytesMut::new();
frame.put_u8(0b1000_0000); // COMPRESS_FLAG
// snappy varint for 8_388_608
frame.extend_from_slice(&[0x80, 0x80, 0x80, 0x04]);
// (rest of payload can be garbage — allocation happens before decompress())

// Wrap in a 4-byte length-delimited header (RelayV3 uses 4-byte length field)
let len = frame.len() as u32;
let mut wire = BytesMut::new();
wire.put_u32(len);
wire.extend_from_slice(&frame);

// Send `wire` over a RelayV3 connection.
// decode() will:
//   1. length_delimited.decode() returns Some(frame) — tiny allocation
//   2. decompress_len() returns 8_388_608
//   3. 8_388_608 > 8_388_608 → false → passes guard
//   4. BytesMut::zeroed(8_388_608) → 8 MB allocated
//   5. decompress() fails → Err returned, connection dropped
// Peak allocation per connection: ~8 MB
// With 1000 concurrent connections firing simultaneously: ~8 GB peak
``` [6](#0-5)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L232-249)
```rust
                if (data[0] & COMPRESS_FLAG) != 0 {
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
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
                            }
```

**File:** network/src/protocols/support_protocols.rs (L45-45)
```rust
    RelayV3,
```

**File:** network/src/protocols/support_protocols.rs (L130-130)
```rust
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
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
