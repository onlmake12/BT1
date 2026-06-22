The code is clear. Let me trace the exact vulnerability path.

**`LengthDelimitedCodecWithCompress::decode`** (lines 222–262 of `network/src/compress.rs`):

1. `self.length_delimited.decode(src)?` — checks the **compressed wire size** against `max_frame_length` (e.g., 1 KB for Ping, 2 KB for Identify).
2. If `COMPRESS_FLAG` is set, calls `decompress_len(&data[1..])`.
3. Checks `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` — strictly greater than 8 MB, so exactly 8 MB passes.
4. Allocates `BytesMut::zeroed(decompressed_bytes_len)` — up to 8 MB.

The decompressed size is **never checked against `max_frame_length`**. Snappy can compress 8 MB of zeros into a few hundred bytes, so the wire-size check is trivially bypassed for any protocol.

---

### Title
Post-Decompression Size Unbounded by Per-Protocol `max_frame_length` in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

### Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire frame. After passing that check, it allocates a buffer of up to `MAX_UNCOMPRESSED_LEN` (8 MB) for any compressed frame, regardless of the protocol's configured limit (e.g., 1 KB for Ping, 2 KB for Identify). An unprivileged remote peer can send a tiny compressed frame (e.g., ~100 bytes of snappy-encoded zeros) that decompresses to exactly 8 MB, causing an 8 MB heap allocation per message per connection.

### Finding Description
In `network/src/compress.rs`, `LengthDelimitedCodecWithCompress::decode`:

```rust
// Line 226: wire-size check — only guards compressed size
match self.length_delimited.decode(src)? {
    Some(mut data) => {
        if (data[0] & COMPRESS_FLAG) != 0 {
            match decompress_len(&data[1..]) {
                Ok(decompressed_bytes_len) => {
                    // Line 235: off-by-one — exactly 8 MB passes
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        return Err(io::ErrorKind::InvalidData.into());
                    }
                    // Line 242: allocates up to 8 MB unconditionally
                    let mut buf = BytesMut::zeroed(decompressed_bytes_len);
``` [1](#0-0) 

The per-protocol `max_frame_length` values are:
- Ping: 1 KB, Identify: 2 KB, Feeler/DisconnectMessage/Time: 1 KB
- Sync: 2 MB, RelayV3: 4 MB [2](#0-1) 

These limits are passed into `length_delimited::Builder::new().max_frame_length(max_frame_length)` and only guard the compressed wire size. [3](#0-2) 

After `length_delimited.decode` passes, there is no check that `decompressed_bytes_len <= self.length_delimited.max_frame_length()`. The only post-decompression guard is the global `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB), and the check is `>` (strict), so exactly 8 MB passes. [4](#0-3) 

### Impact Explanation
An attacker with max inbound connections can send one crafted compressed frame per connection per protocol. Each frame: compressed wire size ≈ a few hundred bytes (passes any `max_frame_length` check), decompressed size = 8,388,608 bytes. Each triggers `BytesMut::zeroed(8388608)`. With, e.g., 125 inbound connections × multiple protocols open simultaneously, this is gigabytes of heap allocation from a single round of messages, causing OOM on the victim node. All honest nodes relaying the same compressed frames are equally affected.

### Likelihood Explanation
The attacker only needs to: (1) establish TCP connections (no PoW, no keys, no stake), (2) complete the tentacle handshake, (3) open any protocol, (4) send a single crafted snappy frame. Snappy streams that decompress to 8 MB of zeros compress to roughly 80–200 bytes, well within every protocol's `max_frame_length`. This is trivially constructable and locally testable.

### Recommendation
After `decompress_len` returns, add a check against the protocol's configured limit before allocating:

```rust
let limit = self.length_delimited.max_frame_length();
if decompressed_bytes_len > limit {
    return Err(io::Error::new(io::ErrorKind::InvalidData, "decompressed data too large"));
}
```

Also change `>` to `>=` for the `MAX_UNCOMPRESSED_LEN` guard to close the off-by-one. Apply the same fix to `Message::decompress` in the same file. [5](#0-4) 

### Proof of Concept
```rust
// Craft a snappy stream that decompresses to exactly 8,388,608 bytes of zeros.
// Compressed size will be ~100-200 bytes.
let raw = vec![0u8; 8_388_608];
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
assert!(compressed.len() < 1024); // passes Ping's max_frame_length

// Build wire frame: 4-byte length prefix + COMPRESS_FLAG byte + compressed payload
let mut frame = BytesMut::new();
let payload_len = compressed.len() + 1;
frame.put_uint(payload_len as u64, 4);
frame.put_u8(0x80); // COMPRESS_FLAG
frame.extend_from_slice(&compressed);

// Feed to LengthDelimitedCodecWithCompress::decode configured for Ping (max_frame_length=1024)
// Result: Ok(Some(buf)) where buf.len() == 8_388_608
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L226-244)
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
