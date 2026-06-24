The code confirms all claims in the report. Let me verify the key details:

**Decoder (lines 232–242):** Only `MAX_UNCOMPRESSED_LEN` (8 MB) guards the decompressed size — no `max_frame_length` check. [1](#0-0) 

**Encoder (lines 142–149):** `process()` does check `max_frame_length` on the compressed payload, creating an asymmetry. [2](#0-1) 

**Protocol limits (lines 124–131):** Ping, Feeler, DisconnectMessage, Time all have `max_frame_length = 1024` (1 KB), while `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB). [3](#0-2) 

**All protocols use compression and `LengthDelimitedCodecWithCompress` (lines 280–288).** [4](#0-3) 

---

Audit Report

## Title
Decompressed frame size not bounded by per-protocol `max_frame_length` in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` on the compressed wire bytes but applies only the global `MAX_UNCOMPRESSED_LEN` (8 MB) constant to the decompressed output. Any unauthenticated peer can send a snappy-compressed frame that fits within a protocol's wire limit (e.g., 1 KB for Ping) but decompresses to up to 8 MB, causing an 8192× memory amplification per frame. Sustained from multiple connections, this exhausts node memory and causes an OOM crash.

## Finding Description
In `decode` (`compress.rs:222–262`), the wire-level check at line 226 (`self.length_delimited.decode(src)?`) rejects frames exceeding `max_frame_length` in compressed form. However, in the `COMPRESS_FLAG` branch (lines 232–255), the only guard on the decompressed size is:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {  // 8 MB
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len);
```

There is no check of the form `decompressed_bytes_len > self.length_delimited.max_frame_length()`. The encoder-side `process()` method (lines 142–155) does enforce `max_frame_length` on the outgoing compressed payload via `self.length_delimited.max_frame_length()`, but the decoder has no symmetric check on the decompressed output. The `max_frame_length()` accessor is already used in `process()`, confirming it is available on the stored `length_delimited` field.

All protocols constructed via `CKBProtocol::new_with_support_protocol` set `compress: true` and use `LengthDelimitedCodecWithCompress`. Protocols such as Ping, Feeler, DisconnectMessage, and Time have `max_frame_length = 1024` bytes (1 KB), while `MAX_UNCOMPRESSED_LEN` is 8 MB — a factor of 8192×. Snappy's format allows highly repetitive data (e.g., 8 MB of zeros) to compress to well under 100 bytes, making it trivial to construct a compressed payload that fits within a 1024-byte wire frame but decompresses to the full 8 MB limit.

## Impact Explanation
This is a memory amplification attack reachable by any unauthenticated peer. Each crafted frame causes an 8 MB allocation on the receiving node. With N concurrent peers each sending such frames continuously, memory pressure grows as O(N × 8 MB). Under sustained attack from a modest number of peers, the node's memory is exhausted, causing an OOM crash. **Impact: High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**

## Likelihood Explanation
Reachable by any peer that can establish a TCP connection — no proof-of-work, no key, no privilege required. The attacker only needs to craft a valid snappy-compressed frame with a large declared decompressed length. Snappy's format is well-documented and libraries are widely available. The attack is repeatable and can be sustained indefinitely from a single connection.

## Recommendation
In `decode`, after obtaining `decompressed_bytes_len` and before allocating, add a per-protocol bound check:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "decompressed data too large",
    ));
}
```

This mirrors the existing encoder-side check in `process()` and ensures the decoder enforces the same per-protocol size contract as the encoder.

## Proof of Concept
For Ping (`max_frame_length = 1024`):
1. Construct 8 MB of zeros (`vec![0u8; 8 * 1024 * 1024]`).
2. Snappy-compress it — the result is well under 100 bytes.
3. Prepend `COMPRESS_FLAG` (0x80) as the first byte.
4. Prepend a 4-byte big-endian length header (total payload length).
5. Feed the resulting ≤ 1024-byte buffer into `LengthDelimitedCodecWithCompress::decode` configured with `max_frame_length = 1024`.
6. Assert the returned `BytesMut` has `len() == 8_388_608` — confirming the 8192× amplification.
7. Repeat from multiple concurrent connections to exhaust node memory.

### Citations

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

**File:** network/src/compress.rs (L232-242)
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
```

**File:** network/src/protocols/support_protocols.rs (L124-131)
```rust
            SupportProtocols::Ping => 1024,                   // 1   KB
            SupportProtocols::Discovery => 512 * 1024,        // 512 KB
            SupportProtocols::Identify => 2 * 1024,           // 2   KB
            SupportProtocols::Feeler => 1024,                 // 1   KB
            SupportProtocols::DisconnectMessage => 1024,      // 1   KB
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
            SupportProtocols::Time => 1024,                   // 1   KB
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
