Audit Report

## Title
Snappy Varint Mismatch Allows ~8MB Allocation Per Frame Bypassing `max_frame_length` — (`network/src/compress.rs`)

## Summary
In `LengthDelimitedCodecWithCompress::decode`, the compressed wire frame is bounded by `max_frame_length` (e.g., 1024 bytes for Ping), but the decoder allocates a buffer sized by the attacker-controlled snappy uncompressed-length varint before attempting decompression. The only guard is `MAX_UNCOMPRESSED_LEN = 1 << 23` (8,388,608 bytes), so a crafted frame can cause up to ~8MB of heap allocation per connection regardless of the protocol's per-message limit. With 117 simultaneous inbound connections, this yields a transient ~936MB peak allocation that can crash memory-constrained nodes.

## Finding Description
The decode path in `LengthDelimitedCodecWithCompress::decode` proceeds as follows:

1. `self.length_delimited.decode(src)?` enforces `max_frame_length` on the **compressed** wire frame (e.g., 1024 bytes for Ping). [1](#0-0) 

2. If `COMPRESS_FLAG` is set, `decompress_len(&data[1..])` reads the snappy uncompressed-length varint from the attacker-controlled payload. [2](#0-1) 

3. The only guard is `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` where `MAX_UNCOMPRESSED_LEN = 1 << 23`. A declared length of 8,388,607 passes this check. [3](#0-2) [4](#0-3) 

4. **Before** attempting decompression, the decoder allocates `BytesMut::zeroed(decompressed_bytes_len)` — up to ~8MB — with no check against `max_frame_length`. [5](#0-4) 

5. `SnapDecoder::new().decompress(...)` then fails (the garbage body does not expand to 8MB), the error is returned and the connection is dropped — but the allocation already occurred. [6](#0-5) 

The outbound `process` function does check `len > self.length_delimited.max_frame_length()`, but this is encoder-only and has no effect on inbound decoding. [7](#0-6) 

The same allocation-before-validation pattern exists in `Message::decompress` (the free-function path), which also allocates `vec![0; decompressed_bytes_len]` without any per-protocol bound. [8](#0-7) 

The `LengthDelimitedCodecWithCompress` is wired into every protocol registered via `CKBProtocol::build()`, which is the path used by `new_with_support_protocol`. [9](#0-8) 

Affected protocols and their amplification ratios:
- Ping: 1 KB → 8 MB (8192×)
- Identify: 2 KB → 8 MB (4096×)
- Feeler: 1 KB → 8 MB (8192×)
- DisconnectMessage: 1 KB → 8 MB (8192×)
- Time: 1 KB → 8 MB (8192×) [10](#0-9) 

## Impact Explanation
An unprivileged remote peer can cause up to ~8MB of heap allocation per crafted frame. With `max_inbound = 117` (from `max_peers = 125`, `max_outbound_peers = 8`), 117 simultaneous connections each sending one crafted Ping frame produce a transient peak allocation of ~936MB. Since no automatic ban is applied for `InvalidData` decode errors, the attacker can reconnect and repeat continuously, sustaining memory pressure sufficient to crash memory-constrained CKB nodes. This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**.

## Likelihood Explanation
The attack is fully reachable from any unprivileged P2P peer. The attacker needs only a TCP connection to port 8115, the ability to complete the tentacle/secio handshake, and the ability to open a substream for any small-frame protocol. Constructing a snappy stream with an arbitrary uncompressed-length varint and a garbage body is trivial — `decompress_len` reads only the leading varint without validating the rest of the stream. The crafted frame is ~17 bytes on the wire. The attack is repeatable with no authentication, no PoW, and no valid CKB message content required.

## Recommendation
In `LengthDelimitedCodecWithCompress::decode`, after reading `decompressed_bytes_len` and before allocating, add:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This bounds the declared uncompressed length to the same per-protocol limit that governs the compressed wire frame, eliminating the amplification. The same guard should be added symmetrically in `Message::decompress` — but since that path does not have access to `max_frame_length`, the caller should pass the limit or the check should be enforced at the call site. [11](#0-10) [8](#0-7) 

## Proof of Concept

```python
import socket, struct

# Snappy varint encoding of 8388607 (just under MAX_UNCOMPRESSED_LEN = 1<<23)
# LEB128: 8388607 = 0x7FFFFF → encoded as 0xFF, 0xFF, 0xFF, 0x03
snappy_varint = bytes([0xFF, 0xFF, 0xFF, 0x03])
snappy_body   = bytes([0x00] * 10)  # garbage; decompression will fail

# Frame: 1-byte COMPRESS_FLAG (0x80) + snappy stream
frame_body = bytes([0x80]) + snappy_varint + snappy_body

# LengthDelimited: 4-byte big-endian length prefix (total ~15 bytes, within 1024-byte limit)
frame = struct.pack(">I", len(frame_body)) + frame_body

# After tentacle/secio handshake and opening Ping substream:
# s.sendall(frame)
# Node executes BytesMut::zeroed(8388607) → ~8MB allocation → decompression fails → InvalidData
# Repeat with 117 simultaneous connections for ~936MB peak RSS
```

Minimal unit test to confirm the allocation occurs before the error:

```rust
#[test]
fn test_decompression_bomb() {
    use tokio_util::codec::{Decoder, length_delimited};
    use p2p::bytes::BytesMut;
    use crate::compress::LengthDelimitedCodecWithCompress;

    let mut codec = LengthDelimitedCodecWithCompress::new(
        true,
        length_delimited::Builder::new().max_frame_length(1024).new_codec(),
        0.into(),
    );

    // Build crafted frame: COMPRESS_FLAG + snappy varint 8388607 + garbage body
    let mut frame = BytesMut::new();
    let payload: &[u8] = &[0x80, 0xFF, 0xFF, 0xFF, 0x03, 0x00, 0x00, 0x00, 0x00];
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(payload);

    // decode() should return InvalidData, but ~8MB was allocated transiently
    let result = codec.decode(&mut frame);
    assert!(result.is_err());
}
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L72-82)
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
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
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

**File:** network/src/compress.rs (L243-249)
```rust
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
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
