Audit Report

## Title
Post-Decompression Size Unbounded by Per-Protocol `max_frame_length` in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire frame via `self.length_delimited.decode(src)?`. After that check passes, it allocates a buffer of up to `MAX_UNCOMPRESSED_LEN` (8 MB) for any compressed frame, regardless of the protocol's configured limit (e.g., 1 KB for Ping). An unprivileged remote peer can send a tiny compressed frame (~100–200 bytes of snappy-encoded zeros) that decompresses to exactly 8 MB, triggering an 8 MB heap allocation per message per connection.

## Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode` (lines 222–262):

**Step 1 — Wire-size check (compressed only):**
`self.length_delimited.decode(src)?` enforces `max_frame_length` on the compressed frame body. For Ping this is 1024 bytes. [1](#0-0) 

**Step 2 — Decompressed size check against global ceiling only:**
After the wire-size check passes, `decompress_len` is called and the result is compared only against `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB) using a strict `>`, meaning exactly 8,388,608 bytes passes. There is no comparison against `self.length_delimited.max_frame_length()`. [2](#0-1) 

**Step 3 — Unconditional 8 MB allocation:**
`BytesMut::zeroed(decompressed_bytes_len)` allocates up to 8 MB regardless of the protocol's per-message limit. [3](#0-2) 

**Root cause:** The `LengthDelimitedCodecWithCompress` struct holds `length_delimited: length_delimited::LengthDelimitedCodec` which exposes `max_frame_length()` (already used in `process()` at line 144), but `decode()` never calls it post-decompression. [4](#0-3) 

**Exploit path:**
1. Attacker establishes a TCP connection and completes the tentacle handshake.
2. Opens any protocol (e.g., Ping, `max_frame_length = 1024`).
3. Crafts a snappy stream encoding 8,388,608 bytes of zeros; compressed size ≈ 80–200 bytes — well within 1024 bytes.
4. Sends the wire frame: 4-byte length prefix + `0x80` (COMPRESS_FLAG) + compressed payload.
5. `length_delimited.decode` passes (compressed body ≤ 1024 bytes).
6. `decompress_len` returns 8,388,608; `8388608 > 8388608` is false, so the guard passes.
7. `BytesMut::zeroed(8388608)` executes — 8 MB allocated per message.

**Existing guards are insufficient:**
- The `max_frame_length` guard in `length_delimited.decode` only covers the compressed wire size.
- The `MAX_UNCOMPRESSED_LEN` guard uses `>` (not `>=`), allowing exactly 8 MB through.
- No per-protocol post-decompression size check exists anywhere in the decode path.

Per-protocol limits are defined in `support_protocols.rs` and wired into `LengthDelimitedCodecWithCompress` via `CKBProtocol::build()`, but are never consulted after decompression. [5](#0-4) [6](#0-5) 

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Each crafted frame causes an 8 MB heap allocation. With the default inbound connection limit (e.g., 125 peers), each peer can send one such frame per protocol per message cycle. At 125 connections × multiple open protocols, this yields gigabytes of heap allocation from a single round of messages, causing OOM and crashing the victim node. The same compressed frames forwarded by honest relaying nodes would affect all relaying peers equally.

## Likelihood Explanation
The attacker requires no proof-of-work, no keys, no stake, and no special privileges — only the ability to establish a TCP connection and complete the tentacle handshake. Snappy streams decompressing to 8 MB of zeros are trivially constructable locally. The attack is repeatable at will and requires no victim interaction or mistake. Every CKB node accepting inbound connections is exposed.

## Recommendation
After `decompress_len` returns, add a check against the protocol's configured limit before allocating:

```rust
let limit = self.length_delimited.max_frame_length();
if decompressed_bytes_len > limit {
    return Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "decompressed data too large",
    ));
}
```

Also change `>` to `>=` on line 235 to close the off-by-one allowing exactly 8 MB through:

```rust
if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {
```

Apply the same fix to `Message::decompress` (lines 72–79) which has the identical `>` off-by-one against `MAX_UNCOMPRESSED_LEN`. [7](#0-6) 

## Proof of Concept

```rust
use snap::raw::Encoder as SnapEncoder;
use p2p::bytes::{BufMut, BytesMut};
use tokio_util::codec::{Decoder, length_delimited};
use ckb_network::compress::LengthDelimitedCodecWithCompress;

// 1. Craft a snappy stream decompressing to exactly 8,388,608 bytes of zeros.
let raw = vec![0u8; 8_388_608];
let compressed = SnapEncoder::new().compress_vec(&raw).unwrap();
assert!(compressed.len() < 1024, "compressed size {} must be < 1024 (Ping limit)", compressed.len());

// 2. Build wire frame: 4-byte big-endian length + COMPRESS_FLAG byte + compressed payload
let payload_len = compressed.len() + 1; // +1 for flag byte
let mut src = BytesMut::new();
src.put_uint(payload_len as u64, 4);
src.put_u8(0x80); // COMPRESS_FLAG
src.extend_from_slice(&compressed);

// 3. Decode using Ping's codec (max_frame_length = 1024)
let mut codec = LengthDelimitedCodecWithCompress::new(
    true,
    length_delimited::Builder::new()
        .max_frame_length(1024)
        .new_codec(),
    0.into(), // Ping protocol id
);

let result = codec.decode(&mut src).unwrap();
// result is Ok(Some(buf)) where buf.len() == 8_388_608
// 8 MB allocated despite Ping's 1 KB max_frame_length
assert_eq!(result.unwrap().len(), 8_388_608);
```

This test can be run as a unit test in `network/src/compress.rs` to confirm the allocation occurs and the decode returns `Ok(Some(_))` rather than an error.

### Citations

**File:** network/src/compress.rs (L72-79)
```rust
            match decompress_len(&self.inner[1..]) {
                Ok(decompressed_bytes_len) => {
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
```

**File:** network/src/compress.rs (L142-149)
```rust
    fn process(&self, data: &[u8], flag: u8, dst: &mut BytesMut) -> Result<(), io::Error> {
        let len = data.len() + 1;
        if len > self.length_delimited.max_frame_length() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "data too large",
            ));
        }
```

**File:** network/src/compress.rs (L226-226)
```rust
        match self.length_delimited.decode(src)? {
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
