The code is fully readable. Let me trace the exact path.

**`LengthDelimitedCodecWithCompress::decode`** (lines 222–262):

1. `self.length_delimited.decode(src)?` — enforces `max_frame_length` on the **wire** (compressed) bytes. A Ping frame ≤ 1024 bytes passes. [1](#0-0) 

2. If `COMPRESS_FLAG` is set, it reads the snappy-declared decompressed length. [2](#0-1) 

3. The **only** guard on the decompressed size is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (8 MB). There is **no** check against `self.length_delimited.max_frame_length()`. [3](#0-2) 

4. It allocates `BytesMut::zeroed(decompressed_bytes_len)` and returns it to the protocol handler. [4](#0-3) 

The `process()` encoder-side method does check `max_frame_length` against the compressed payload, but the decoder never checks the decompressed output against it. [5](#0-4) 

Ping's `max_frame_length` is 1024 bytes; Identify's is 2 KB; DisconnectMessage's is 1 KB. [6](#0-5) 

`MAX_UNCOMPRESSED_LEN` is 8 MB — 8192× larger than Ping's limit. [7](#0-6) 

---

### Title
Decompressed frame size not bounded by per-protocol `max_frame_length` in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

### Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire bytes. After decompression, the returned `BytesMut` is bounded solely by the global `MAX_UNCOMPRESSED_LEN` (8 MB), not by the per-protocol limit. An unprivileged remote peer can send a compressed frame within the wire limit (e.g., ≤ 1024 bytes for Ping) that decompresses to up to 8 MB, causing the decoder to allocate and deliver an 8 MB buffer to the protocol handler.

### Finding Description
In `decode` (compress.rs:222–262), after `self.length_delimited.decode(src)` accepts a wire frame within `max_frame_length`, the compressed-flag branch calls `decompress_len` and checks only:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN { // 8 MB
    return Err(...);
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len);
```

There is no `if decompressed_bytes_len > self.length_delimited.max_frame_length()` guard. The encoder-side `process()` method does enforce `max_frame_length` on the outgoing compressed payload, but the decoder has no symmetric check on the decompressed output.

### Impact Explanation
Any protocol with compression enabled (all protocols built via `CKBProtocol::new_with_support_protocol`, which sets `compress: true`) is affected. For Ping (1 KB limit), a single crafted 1 KB wire frame causes an 8 MB allocation. With multiple concurrent peers each sending such frames, memory pressure scales linearly. Protocol handlers receive messages far exceeding their intended size bounds, potentially triggering unexpected behavior in message parsers (e.g., flatbuffers deserialization of an 8 MB Ping message).

### Likelihood Explanation
Reachable by any unauthenticated peer. No PoW, no key, no privilege required — just a TCP connection and a crafted snappy frame. Snappy's format makes it straightforward to construct a frame that compresses well (e.g., a run-length-encoded payload).

### Recommendation
In `decode`, after obtaining `decompressed_bytes_len`, add:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::Error::new(io::ErrorKind::InvalidData, "decompressed data too large"));
}
```

This mirrors the existing encoder-side check in `process()`.

### Proof of Concept
For Ping (`max_frame_length = 1024`):
1. Construct a byte string of 8 MB of zeros.
2. Snappy-compress it — the compressed form is well under 1024 bytes.
3. Prepend the 4-byte length header and `COMPRESS_FLAG` byte.
4. Feed the resulting ≤ 1024-byte buffer into `LengthDelimitedCodecWithCompress::decode` configured with `max_frame_length = 1024`.
5. Assert the returned `BytesMut` has `len() == 8_388_608` — 8192× the declared protocol limit.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
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

**File:** network/src/compress.rs (L226-227)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
```

**File:** network/src/compress.rs (L232-234)
```rust
                if (data[0] & COMPRESS_FLAG) != 0 {
                    match decompress_len(&data[1..]) {
                        Ok(decompressed_bytes_len) => {
```

**File:** network/src/compress.rs (L235-241)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
                            }
```

**File:** network/src/compress.rs (L242-244)
```rust
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
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
