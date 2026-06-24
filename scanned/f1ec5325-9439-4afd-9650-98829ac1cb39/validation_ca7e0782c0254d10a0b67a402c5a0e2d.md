Audit Report

## Title
Pre-allocation heap amplification via crafted snappy decompressed-length header — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` allocates a zeroed buffer of `decompressed_bytes_len` bytes before attempting actual decompression. The guard uses strict `>` instead of `>=`, so a frame claiming exactly `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes) passes the check. An attacker can craft a tiny snappy frame with a forged varint header claiming exactly 8 MB decompressed, triggering an 8 MB heap allocation per connection before the decode error propagates and the session is closed.

## Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`:

The constant is defined as: [1](#0-0) 

The guard and pre-allocation in `decode`: [2](#0-1) 

The check `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (strict `>`) allows a claimed length of exactly 8,388,608 to pass. `BytesMut::zeroed(decompressed_bytes_len)` is then called unconditionally before any decompression is attempted. The snappy `decompress_len` function reads only the varint header from the snappy stream — it does not validate that the actual compressed payload can produce that many bytes. An attacker can therefore craft a frame where the snappy varint header claims exactly 8,388,608 bytes but the actual compressed payload is a handful of bytes (e.g., a minimal literal block). This frame:

1. Passes the `length_delimited` codec's `max_frame_length` check (compressed size is tiny, well under any protocol's limit) [3](#0-2) 
2. Passes the `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` guard (8,388,608 is not strictly greater than 8,388,608)
3. Triggers `BytesMut::zeroed(8,388,608)` — an 8 MB zero-fill allocation
4. Fails decompression (actual data doesn't expand to 8 MB), returns `InvalidData`, closes the session

The same structural issue exists in `Message::decompress` at line 81 (`vec![0; decompressed_bytes_len]`), though that path is reached via a different call site. [4](#0-3) 

## Impact Explanation
Default configuration allows up to 117 simultaneous inbound connections (`max_peers = 125`, `max_outbound_peers = 8`). An attacker opening 117 concurrent connections and sending one crafted frame per connection simultaneously causes up to `117 × 8 MB ≈ 936 MB` of heap allocation before any error propagates. On nodes with ≤1 GB available RAM this causes OOM / process crash. After each batch of connections is dropped on decode error, the attacker can immediately reconnect and repeat, sustaining memory pressure indefinitely. This matches the **High** impact class: "Vulnerabilities which could easily crash a CKB node."

The service-level cap is 1024: [5](#0-4) 

## Likelihood Explanation
- No authentication or proof-of-work is required — any TCP client can open inbound connections
- SECIO handshake is a standard key exchange with no rate limit or PoW; it is not a meaningful barrier
- The crafted snappy frame is trivial to construct: set the varint to `0x80 0x80 0x80 0x04` (= 8,388,608), append any minimal literal block
- Default `max_peers = 125` is publicly documented; the attack is fully deterministic and repeatable

## Recommendation
1. Change the guard to `>=`: `if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN`
2. More importantly, do not pre-allocate based on the claimed length. Either use a streaming decompressor that does not require a pre-sized output buffer, or validate that the compressed payload length is plausible relative to the claimed decompressed length before allocating (snappy's maximum compression ratio is ~8:1, so `compressed_len * 8 < claimed_decompressed_len` is a strong signal of a forged header)
3. Add per-IP or per-session rate limiting on decode errors to slow reconnect-and-repeat attacks

## Proof of Concept
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
# (after completing SECIO handshake)
# Each triggers BytesMut::zeroed(8388608) before returning InvalidData
# Peak RSS spike ≈ 117 * 8 MB ≈ 936 MB
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L74-82)
```rust
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
                    } else {
                        let mut buf = vec![0; decompressed_bytes_len];
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
```

**File:** network/src/compress.rs (L235-243)
```rust
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

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```
