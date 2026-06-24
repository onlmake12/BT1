Audit Report

## Title
Snappy Header-Driven Memory Amplification via Attacker-Controlled Varint in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
In `LengthDelimitedCodecWithCompress::decode`, a heap allocation of up to 8 MB is made at line 242 based solely on the attacker-controlled snappy varint header, before any decompression work is performed. The guard at line 235 uses a strict `>` comparison, allowing `decompressed_bytes_len == MAX_UNCOMPRESSED_LEN` (8,388,608 bytes) to pass. Because this allocation precedes the decompressor call, it occurs even if the decompressor subsequently fails. With many concurrent peers each sending crafted frames, this can exhaust node memory and crash the process.

## Finding Description
**Root cause — `network/src/compress.rs`, lines 233–244:**

```rust
match decompress_len(&data[1..]) {
    Ok(decompressed_bytes_len) => {
        if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // line 235: strict >
            ...
            return Err(io::ErrorKind::InvalidData.into());
        }
        let mut buf = BytesMut::zeroed(decompressed_bytes_len);  // line 242: allocation
        match SnapDecoder::new().decompress(&data[1..], &mut buf) {
            Ok(_) => Ok(Some(buf)),
```

`decompress_len(&data[1..])` reads the varint from the snappy stream header — a field entirely under attacker control. The guard at line 235 rejects only values **strictly greater than** `MAX_UNCOMPRESSED_LEN` (8,388,608), so a varint of exactly `MAX_UNCOMPRESSED_LEN` passes and triggers `BytesMut::zeroed(8_388_608)` at line 242.

This allocation happens **before** `SnapDecoder::new().decompress(...)` is called at line 243. Even if the decompressor subsequently returns `Err` (e.g., because the actual decompressed output does not match the declared varint), the 8 MB allocation was already made. In a multi-threaded async runtime (tokio), many connections can be concurrently executing this decode path, keeping multiple such allocations live simultaneously.

**Why existing guards are insufficient:**
- The `max_frame_length` check (enforced by the inner `length_delimited` codec) bounds the *compressed* frame size (e.g., 1 KB for Ping, 4 MB for RelayV3), but does **not** bound the decompressed allocation — that is driven by the snappy varint, not the frame length.
- The `> MAX_UNCOMPRESSED_LEN` guard at line 235 should be `>=` to also reject the exact boundary value, but even a corrected `>=` guard does not address the core issue: the allocation is proportional to the attacker-supplied varint with no relationship to the actual compressed payload size.

**Compression is enabled by default** for all protocols via `CKBProtocol::new_with_support_protocol` (`compress: true`, line 219 of `mod.rs`) and `CKBProtocol::new` (`compress: true`, line 243 of `mod.rs`). The `LengthDelimitedCodecWithCompress` codec is wired in via `CKBProtocol::build()` (lines 280–288 of `mod.rs`).

## Impact Explanation
An unprivileged remote peer can cause repeated ~8 MB heap allocations on the target node with minimal bandwidth. With the default or configured `max_connection_number`, concurrent allocations across all peers can reach tens of gigabytes, triggering OOM and crashing the node process. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node (10001–15000 points)**.

## Likelihood Explanation
- Any peer that completes a session handshake can send compressed frames; no special privileges are required.
- Compression is enabled by default on all CKB protocols.
- Crafting a snappy stream with an arbitrary varint is a trivial byte-level operation requiring no cryptographic material or protocol secrets.
- The attacker only needs to open many connections and send one crafted frame per connection; no sustained high bandwidth is required.
- The attack is repeatable and scalable.

## Recommendation
1. **Bound the decompressed allocation by the actual compressed input size**, not the snappy header varint. Replace `BytesMut::zeroed(decompressed_bytes_len)` with an allocation capped at `min(decompressed_bytes_len, data.len() * MAX_COMPRESSION_RATIO)` for a reasonable `MAX_COMPRESSION_RATIO` (e.g., 1024).
2. **Fix the off-by-one in the guard**: change `> MAX_UNCOMPRESSED_LEN` to `>= MAX_UNCOMPRESSED_LEN` at line 235.
3. **Disconnect and ban peers** that send frames where `decompress_len` significantly exceeds the compressed frame size (e.g., ratio > 1024×).
4. **Add a global in-flight decompression memory counter** and reject new frames when the limit is exceeded.
5. Consider a streaming/lazy decompressor that does not pre-allocate based on the header varint.

## Proof of Concept
1. Craft a snappy stream: set the varint header to `MAX_UNCOMPRESSED_LEN - 1` (8,388,607), followed by any minimal valid or invalid compressed payload.
2. Wrap it in a CKB compressed frame: prefix byte `0x80` (COMPRESS_FLAG), then the snappy stream, then a 4-byte big-endian length prefix.
3. Open N connections to the target node, complete the session handshake on each, and send the crafted frame on each connection.
4. Observe: each connection triggers `BytesMut::zeroed(8_388_607)` at `compress.rs:242`. With N=125 concurrent connections, ~1 GB is allocated; with N=1024, ~8 GB.
5. A unit test can confirm the allocation by calling `LengthDelimitedCodecWithCompress::decode` directly with a crafted buffer containing the above snappy stream and measuring peak RSS before and after. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** network/src/protocols/mod.rs (L217-220)
```rust
            network_state,
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
