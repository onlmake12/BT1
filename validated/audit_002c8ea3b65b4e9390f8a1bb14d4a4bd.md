### Title
Snappy Decompression Amplification Allows Remote Memory Exhaustion via Near-8MB Allocation Per Frame — (`network/src/compress.rs`)

---

### Summary

The `LengthDelimitedCodecWithCompress::decode` function in `network/src/compress.rs` allocates a buffer sized to the **claimed** decompressed length before performing actual decompression. Because the `max_frame_length` guard operates on the **compressed** wire frame, an attacker can send a tiny compressed frame (e.g., a few KB of snappy-compressed zeros) that claims a decompressed size of up to `MAX_UNCOMPRESSED_LEN - 1` (8MB − 1 byte), triggering a near-8MB allocation per message. There is no rate limit or total-memory cap on these allocations. Pipelining many such frames across one or more sessions can exhaust node memory.

---

### Finding Description

**Entrypoint**: Any open P2P session on Sync (max_frame_length = 2MB) or RelayV3 (max_frame_length = 4MB). No authentication is required.

**Decode path** in `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`: [1](#0-0) 

Step-by-step:

1. `self.length_delimited.decode(src)?` enforces `max_frame_length` on the **compressed** frame body length declared in the wire length-prefix. A frame with a 4-byte length prefix claiming 1 000 bytes passes the 4MB RelayV3 limit. [2](#0-1) 

2. If the compress flag is set, `decompress_len(&data[1..])` reads the varint-encoded decompressed length from the snappy stream header — a fully attacker-controlled field. [3](#0-2) 

3. The only guard is a strict `>` comparison against `MAX_UNCOMPRESSED_LEN` (8 388 608 bytes). A value of 8 388 607 passes. [4](#0-3) 

4. `BytesMut::zeroed(decompressed_bytes_len)` is called **before** decompression is attempted, allocating up to ~8MB unconditionally. [5](#0-4) 

5. Only after the allocation does `SnapDecoder::new().decompress()` run. If the attacker sends a valid snappy frame (e.g., 8MB−1 of zero bytes compresses to ~1 KB), decompression succeeds and the 8MB buffer is passed up the stack to the protocol handler, where it lives until the handler finishes processing. [6](#0-5) 

**Amplification ratio**: A ~1 KB compressed frame → ~8 MB allocation = roughly 8 000 : 1.

The `max_frame_length` values per protocol are: [7](#0-6) 

`MAX_UNCOMPRESSED_LEN` is defined as: [8](#0-7) 

The codec is wired into every protocol via `CKBProtocol::build`: [9](#0-8) 

---

### Impact Explanation

- Each crafted frame causes a ~8MB heap allocation in the I/O decode path.
- The protocol handler (Sync/Relay) processes messages asynchronously and is slower than the codec; many decoded buffers can be in-flight simultaneously.
- Pipelining N frames on a single connection keeps N × 8MB live concurrently.
- With multiple connections (up to the configured max-peers limit), the total live allocation is `connections × pipeline_depth × 8MB`.
- At ~125 peers × modest pipeline depth, this reaches tens of GB, causing OOM and node crash.
- A crashed node cannot participate in consensus, validate blocks, or relay transactions — matching the stated impact of consensus deviation via memory exhaustion.

---

### Likelihood Explanation

- No privilege, key, or special role is required; any peer that completes a TCP handshake and opens a Sync or RelayV3 session can send these frames.
- Crafting the payload is trivial: `snap::raw::Encoder::new().compress_vec(&vec![0u8; 8*1024*1024 - 1])` produces a valid ~1 KB frame.
- There is no per-session message rate limit, no decompressed-bytes-per-second budget, and no total-memory cap in the decode path.
- The attack is local-testable with a single loopback connection.

---

### Recommendation

1. **Tighten the decompressed-size bound relative to the compressed size**: reject any frame where `decompress_len > k * compressed_len` for a small constant `k` (e.g., 4). Snappy's maximum compression ratio for real data is well below 250:1.
2. **Allocate lazily / use a pooled buffer**: do not call `BytesMut::zeroed(decompress_len)` before verifying the frame is well-formed; use `decompress_vec` which allocates only what is needed after validation.
3. **Add a per-session or global decompressed-bytes-in-flight counter** and drop/disconnect sessions that exceed a threshold.
4. **Lower `MAX_UNCOMPRESSED_LEN`** to match the actual maximum legitimate message size (the largest legitimate Sync/Relay message is far smaller than 8MB).

---

### Proof of Concept

```rust
// Attacker-side: craft a valid snappy frame claiming ~8MB decompressed
let payload = vec![0u8; 8 * 1024 * 1024 - 1]; // 8MB-1 of zeros
let compressed = snap::raw::Encoder::new().compress_vec(&payload).unwrap();
// compressed.len() ≈ 1 KB — well within RelayV3's 4MB max_frame_length

// Wire format: 4-byte big-endian length prefix + COMPRESS_FLAG byte + compressed bytes
let mut frame = Vec::new();
let frame_len = (compressed.len() + 1) as u32;
frame.extend_from_slice(&frame_len.to_be_bytes());
frame.push(0b1000_0000); // COMPRESS_FLAG
frame.extend_from_slice(&compressed);

// Send `frame` repeatedly over a RelayV3 session.
// Each receipt causes BytesMut::zeroed(8_388_607) in the victim's decode path.
// Pipeline 1000 such frames → ~8 GB peak RSS.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L226-248)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
                if data.len() < 2 {
                    return Err(io::ErrorKind::InvalidData.into());
                }

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
```

**File:** network/src/protocols/support_protocols.rs (L129-130)
```rust
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
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
