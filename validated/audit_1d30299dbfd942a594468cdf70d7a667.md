Now I have all the information needed to evaluate this claim. Let me analyze the exact code path.

**Key facts from the code:**

**1. The off-by-one in `decode`:** [1](#0-0) 

The check is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (strict `>`), so a frame claiming exactly `8388608` passes. Then `BytesMut::zeroed(decompressed_bytes_len)` allocates 8MB **before** any decompression attempt.

**2. `MAX_UNCOMPRESSED_LEN = 1 << 23 = 8388608`:** [2](#0-1) 

**3. `max_frame_length` per protocol (compressed wire size):** [3](#0-2) 

The `max_frame_length` bounds the **compressed** frame. An attacker can craft a snappy frame with a varint header claiming `8388608` bytes decompressed but with only ~10 bytes of actual payload — well under any protocol's frame limit.

**4. Connection limits:** [4](#0-3) [5](#0-4) 

Default: `max_peers=125`, `max_outbound_peers=8` → `max_inbound = 117`. The service-level cap is 1024. [6](#0-5) 

---

### Title
Pre-allocation heap amplification via crafted snappy decompressed-length header across concurrent inbound connections — (`network/src/compress.rs`)

### Summary
`LengthDelimitedCodecWithCompress::decode` allocates a zeroed buffer of `decompressed_bytes_len` bytes **before** attempting actual decompression. The guard uses strict `>` instead of `>=`, so a frame claiming exactly `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes) passes the check. An attacker can craft a tiny (~10-byte) snappy frame with a forged varint header claiming exactly 8 MB decompressed, triggering an 8 MB heap allocation per connection. With up to 117 simultaneous inbound connections (default config), this yields ~936 MB of simultaneous heap allocation from minimal attacker bandwidth.

### Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`:

```rust
// line 235
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // strict >, not >=
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len);  // line 242 — allocated BEFORE decompression
match SnapDecoder::new().decompress(&data[1..], &mut buf) { ... }
```

The snappy format encodes the uncompressed length as a varint at the start of the stream. `decompress_len` reads only this header — it does not validate that the actual compressed payload can produce that many bytes. An attacker can therefore craft a frame where:
- The snappy varint header claims exactly `8388608` bytes
- The actual compressed payload is a handful of bytes (e.g., a literal block)

This frame:
1. Passes `length_delimited` codec's `max_frame_length` check (compressed size is tiny)
2. Passes the `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` guard (8388608 is not strictly greater than 8388608)
3. Triggers `BytesMut::zeroed(8388608)` — an 8 MB zero-fill allocation
4. Fails decompression (actual data doesn't expand to 8 MB), returns `InvalidData`, closes the session

The off-by-one (`>` vs `>=`) is the direct enabler: without it, the maximum claimable size would be 8,388,607 bytes — functionally identical in impact, but the intent of the constant is clearly to be an exclusive upper bound.

### Impact Explanation
With `max_inbound = 117` (default), an attacker opening 117 concurrent connections and sending one crafted frame per connection simultaneously causes up to `117 × 8 MB ≈ 936 MB` of heap allocation before any error propagates. On nodes with ≤1 GB available RAM this causes OOM / process crash. The attacker's bandwidth cost is negligible: each malicious frame is ~10–20 bytes on the wire.

After each batch of connections is dropped (on decode error), the attacker can immediately reconnect and repeat, sustaining memory pressure indefinitely.

### Likelihood Explanation
- No authentication or PoW required — any TCP client can open inbound connections
- SECIO handshake is required, but it is a standard key-exchange with no rate limit or proof-of-work
- The crafted snappy frame is trivial to construct (set varint to `0x80 0x80 0x80 0x04` = 8388608, append any valid literal block)
- Default `max_peers = 125` is publicly documented; the attack is fully deterministic

### Recommendation
1. Change the guard to `>=` (fix the off-by-one): `if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN`
2. More importantly, **do not pre-allocate based on the claimed length**. Either:
   - Use a streaming decompressor that does not require a pre-sized output buffer, or
   - Validate that the compressed payload length is plausible relative to the claimed decompressed length before allocating (snappy's maximum compression ratio is ~8:1, so `compressed_len * 8 < claimed_decompressed_len` is a strong signal of a forged header)
3. Add per-IP or per-session rate limiting on decode errors to slow reconnect-and-repeat attacks

### Proof of Concept
```python
import socket, struct

# Snappy stream: varint 8388608 (= 0x800000) followed by a 1-byte literal block
# Varint encoding of 8388608: 0x80 0x80 0x80 0x04
# Minimal literal: tag byte 0x00 (literal, len=1), one data byte 0x00
snappy_payload = bytes([0x80, 0x80, 0x80, 0x04,  # varint: 8388608
                        0x00, 0x00])              # 1-byte literal block

# Frame: compress flag (0x80) + snappy_payload
frame_body = bytes([0x80]) + snappy_payload

# Length-delimited framing (4-byte big-endian length prefix)
frame = struct.pack(">I", len(frame_body)) + frame_body

# Open 117 connections, send frame on each simultaneously
# (after completing SECIO handshake — omitted for brevity)
# Each triggers BytesMut::zeroed(8388608) before returning InvalidData
# Peak RSS spike ≈ 117 * 8 MB ≈ 936 MB
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L233-243)
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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** util/app-config/src/configs/network.rs (L354-357)
```rust
    /// Gets maximum inbound peers.
    pub fn max_inbound_peers(&self) -> u32 {
        self.max_peers.saturating_sub(self.max_outbound_peers)
    }
```

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```
