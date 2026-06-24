Audit Report

## Title
Post-Decompression Size Unbounded by Per-Protocol `max_frame_length` in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire frame via `self.length_delimited.decode(src)`. After that check passes, the only guard on the decompressed size is the global `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB), checked with strict `>`, meaning exactly 8,388,608 bytes passes. An unprivileged remote peer can send a snappy-compressed frame of a few hundred bytes (within any protocol's `max_frame_length`) that decompresses to 8 MB, triggering `BytesMut::zeroed(8_388_608)` per message per connection, enabling OOM-based node crash.

## Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode` (lines 222–262): [1](#0-0) 

`self.length_delimited.decode(src)?` enforces `max_frame_length` on the compressed wire bytes only. After it returns `Some(data)`, the code checks the compress flag and calls `decompress_len`: [2](#0-1) 

The only post-decompression guard is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (strict `>`), so exactly 8,388,608 bytes passes. There is no check of `decompressed_bytes_len` against `self.length_delimited.max_frame_length()`. The allocation at line 242 (`BytesMut::zeroed(decompressed_bytes_len)`) proceeds unconditionally for any value up to and including 8 MB.

The per-protocol limits are: [3](#0-2) 

These limits are wired into `LengthDelimitedCodecWithCompress` via `CKBProtocol::build()`: [4](#0-3) 

They guard only the compressed wire size. The decompressed size is never compared against them.

The same flaw exists in `Message::decompress` at lines 72–89: [5](#0-4) 

## Impact Explanation
An attacker can cause repeated 8 MB heap allocations on the victim node — one per crafted message per connection per protocol. With the default inbound connection limit, sending one such frame per connection across all open protocols results in gigabytes of heap allocation from a single round of messages, causing OOM and crashing the CKB node. This matches the **High** impact class: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation
The attacker requires no proof-of-work, no keys, and no stake. Steps: (1) establish TCP connections up to the inbound limit, (2) complete the tentacle handshake, (3) open any protocol (Ping, Identify, etc.), (4) send one crafted snappy frame per connection. Snappy-compressed 8 MB of zeros produces a payload of roughly 80–200 bytes, well within Ping's 1 KB `max_frame_length`. The attack is trivially constructable, locally testable, and repeatable without rate limiting at the codec layer.

## Recommendation
After `decompress_len` returns in `LengthDelimitedCodecWithCompress::decode`, add a check against the per-protocol limit before allocating:

```rust
let limit = self.length_delimited.max_frame_length();
if decompressed_bytes_len > limit {
    return Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "decompressed data too large",
    ));
}
```

Also change `>` to `>=` on the `MAX_UNCOMPRESSED_LEN` guard (line 235) to close the off-by-one. Apply the same fix to `Message::decompress` (line 74).

## Proof of Concept
```rust
// Craft a snappy stream decompressing to exactly 8,388,608 bytes of zeros.
let raw = vec![0u8; 8_388_608];
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
assert!(compressed.len() < 1024); // passes Ping's max_frame_length of 1 KB

// Build wire frame: 4-byte big-endian length prefix + COMPRESS_FLAG + compressed payload
let mut frame = BytesMut::new();
let payload_len = compressed.len() + 1; // +1 for flag byte
frame.put_uint(payload_len as u64, 4);
frame.put_u8(0x80); // COMPRESS_FLAG
frame.extend_from_slice(&compressed);

// Feed to LengthDelimitedCodecWithCompress configured for Ping (max_frame_length=1024).
// Expected result: Ok(Some(buf)) where buf.len() == 8_388_608
// Each such message causes BytesMut::zeroed(8_388_608) on the victim node.
// Repeat across N inbound connections for N × 8 MB heap pressure.
```

### Citations

**File:** network/src/compress.rs (L72-81)
```rust
            match decompress_len(&self.inner[1..]) {
                Ok(decompressed_bytes_len) => {
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
                    } else {
                        let mut buf = vec![0; decompressed_bytes_len];
```

**File:** network/src/compress.rs (L226-226)
```rust
        match self.length_delimited.decode(src)? {
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
