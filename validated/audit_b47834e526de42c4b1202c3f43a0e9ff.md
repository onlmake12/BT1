### Title
Decompressed-Size Check Bypasses Per-Protocol `max_frame_length`, Enabling 8MB Heap Allocation per Frame — (`network/src/compress.rs`)

---

### Summary

`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the *compressed* wire frame, but allocates a decompression buffer sized by the snappy varint header — bounded only by `MAX_UNCOMPRESSED_LEN` (8 MB). Any unprivileged peer can send a ≤1 KB compressed frame on the Ping protocol (or any other small-`max_frame_length` protocol) that causes an 8 MB heap allocation before any protocol-level size check runs.

---

### Finding Description

The decode path in `LengthDelimitedCodecWithCompress` has two sequential size checks that operate on different quantities:

**Gate 1 — compressed frame size** (line 226): [1](#0-0) 

`self.length_delimited.decode(src)?` rejects any frame whose wire length exceeds `max_frame_length`. For Ping this is 1 024 bytes. [2](#0-1) 

**Gate 2 — decompressed size** (lines 233–242): [3](#0-2) 

`decompress_len` reads the snappy varint header (attacker-controlled). The only rejection threshold is `MAX_UNCOMPRESSED_LEN` = 8 MB. If the claimed length is ≤ 8 MB, `BytesMut::zeroed(decompressed_bytes_len)` is called unconditionally — **before** any check against `max_frame_length`.

There is no guard of the form:
```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() { … }
```

The `max_frame_length` field is present in the struct and used in `process` (the encoder path, line 144), but is never consulted during decoding for the decompressed size. [4](#0-3) 

**Why the compressed frame can be tiny while decompressing to 8 MB:**

Snappy uses LZ77-style back-references. A payload consisting of 8 MB of a single repeated byte (e.g., `0x00`) compresses to well under 200 bytes. The snappy varint at the start of that stream correctly encodes `8388608`. `decompress_len` returns `8388608`, the check `8388608 > 8388608` is false, and `BytesMut::zeroed(8388608)` executes. The frame's wire size is ~200 bytes — comfortably within Ping's 1 024-byte limit.

The codec is wired into every `CKBProtocol::build` call with `compress: true` (the default): [5](#0-4) 

---

### Impact Explanation

| Protocol | `max_frame_length` | Max allocation per frame | Amplification |
|---|---|---|---|
| Ping | 1 KB | 8 MB | 8 192× |
| Identify | 2 KB | 8 MB | 4 096× |
| Feeler | 1 KB | 8 MB | 8 192× |
| DisconnectMessage | 1 KB | 8 MB | 8 192× |
| Time | 1 KB | 8 MB | 8 192× |

A node with N inbound peers, each sending one such frame per second on the Ping protocol, sustains N × 8 MB of concurrent heap pressure from the codec layer alone, independent of any application-level rate limiting. This can cause significant memory pressure, increased GC/allocator overhead, and potential OOM on resource-constrained nodes.

---

### Likelihood Explanation

- No authentication or privilege required — any peer that completes the P2P handshake can send protocol messages.
- The crafted frame is valid snappy; it passes all existing checks.
- The attack is trivially reproducible with a few lines of code using any snappy library.
- The Ping protocol is always open to all connected peers, making it the lowest-friction attack surface.

---

### Recommendation

Add a decompressed-size check against `max_frame_length` immediately after `decompress_len`, before the allocation:

```rust
// In LengthDelimitedCodecWithCompress::decode, after decompress_len succeeds:
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This mirrors the existing guard in `process` (the encoder side) and closes the asymmetry between encode and decode. [4](#0-3) 

---

### Proof of Concept

```rust
// Craft ~200-byte snappy stream that decompresses to 8 MB of zeros
let raw = vec![0u8; 8 * 1024 * 1024]; // 8 MB of zeros
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
// compressed.len() ≈ 130–200 bytes

// Build a wire frame: [4-byte length prefix][COMPRESS_FLAG=0x80][compressed payload]
// Total wire size ≈ 205 bytes — well within Ping's 1024-byte max_frame_length

// Send on the Ping protocol connection.
// Node calls BytesMut::zeroed(8388608) before any protocol-level check.
// Assert: node heap grows by 8 MB per such frame received.
```

The `decompress_len` call on the crafted stream returns exactly `8388608`; the check `8388608 > 8388608` is `false`; the 8 MB allocation proceeds. [6](#0-5)

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

**File:** network/src/compress.rs (L226-226)
```rust
        match self.length_delimited.decode(src)? {
```

**File:** network/src/compress.rs (L233-242)
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
```

**File:** network/src/protocols/support_protocols.rs (L122-131)
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
