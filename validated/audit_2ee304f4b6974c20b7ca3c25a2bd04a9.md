Audit Report

## Title
Pre-allocation heap amplification via crafted snappy decompressed-length header — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` allocates a zeroed buffer of `decompressed_bytes_len` bytes **before** attempting decompression. The guard uses strict `>` instead of `>=`, so a frame claiming exactly `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes) passes the check. An attacker can craft a tiny snappy frame with a forged varint header claiming exactly 8 MB decompressed, triggering an 8 MB heap allocation per connection before the decode error is returned. With up to 117 simultaneous inbound connections under default config, this yields ~936 MB of simultaneous heap allocation from negligible attacker bandwidth.

## Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode` (lines 233–249):

```rust
match decompress_len(&data[1..]) {
    Ok(decompressed_bytes_len) => {
        if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // strict >, not >=
            ...
            return Err(io::ErrorKind::InvalidData.into());
        }
        let mut buf = BytesMut::zeroed(decompressed_bytes_len);  // allocated BEFORE decompression
        match SnapDecoder::new().decompress(&data[1..], &mut buf) {
``` [1](#0-0) 

`MAX_UNCOMPRESSED_LEN = 1 << 23 = 8388608`: [2](#0-1) 

The snappy format encodes the uncompressed length as a varint at the start of the stream. `decompress_len` reads **only this header** — it does not validate that the actual compressed payload can produce that many bytes. An attacker crafts a frame where:
1. The snappy varint header claims exactly `8388608` bytes
2. The actual compressed payload is a handful of bytes (e.g., a 1-byte literal block)

This frame:
- Passes `length_delimited`'s `max_frame_length` check (compressed size is tiny, well under even the smallest protocol limit of 1 KB for Ping — but the attacker targets a protocol with a larger limit, e.g., RelayV3 at 4 MB)
- Passes the `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` guard (8388608 is not strictly greater than 8388608)
- Triggers `BytesMut::zeroed(8388608)` — an 8 MB zero-fill allocation
- Fails decompression (actual data doesn't expand to 8 MB), returns `InvalidData`, closes the session

`LengthDelimitedCodecWithCompress` is confirmed to be used in production via `CKBProtocol::build()`: [3](#0-2) 

All protocols built via `CKBProtocol::new_with_support_protocol` have `compress: true` by default: [4](#0-3) 

The `max_frame_length` values (compressed wire size limits) do not prevent this attack since the malicious frame is tiny: [5](#0-4) 

## Impact Explanation
**High: Vulnerabilities which could easily crash a CKB node.**

Default config: `max_peers = 125`, `max_outbound_peers = 8` → `max_inbound = 117`: [6](#0-5) [7](#0-6) 

With 117 concurrent inbound connections each sending one crafted frame simultaneously: `117 × 8 MB ≈ 936 MB` of heap allocation before any error propagates. On nodes with ≤1 GB available RAM this causes OOM / process crash. After each batch is dropped on decode error, the attacker immediately reconnects and repeats, sustaining memory pressure indefinitely.

## Likelihood Explanation
- No authentication or proof-of-work required — any TCP client can open inbound connections
- SECIO handshake is required but is a standard key exchange with no rate limit or PoW
- The crafted snappy frame is trivial to construct: set varint to `0x80 0x80 0x80 0x04` (= 8388608), append any valid literal block
- Default `max_peers = 125` is publicly documented; the attack is fully deterministic
- The service-level cap of 1024 connections does not prevent the attack at the default 117-inbound limit [8](#0-7) 

## Recommendation
1. Change the guard to `>=`: `if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN`
2. More importantly, **do not pre-allocate based on the claimed length**. Either use a streaming decompressor that does not require a pre-sized output buffer, or validate that the compressed payload length is plausible relative to the claimed decompressed length before allocating (snappy's maximum compression ratio is ~8:1, so `compressed_len * 8 < claimed_decompressed_len` is a strong signal of a forged header)
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

# Open 117 connections, complete SECIO handshake on each, then send frame simultaneously
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

**File:** network/src/protocols/mod.rs (L207-221)
```rust
    pub fn new_with_support_protocol(
        support_protocol: support_protocols::SupportProtocols,
        handler: Box<dyn CKBProtocolHandler>,
        network_state: Arc<NetworkState>,
    ) -> Self {
        CKBProtocol {
            id: support_protocol.protocol_id(),
            max_frame_length: support_protocol.max_frame_length(),
            protocol_name: support_protocol.name(),
            supported_versions: support_protocol.support_versions(),
            network_state,
            handler,
            compress: true,
        }
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
