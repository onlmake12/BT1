### Title
Decompression Bomb via Snappy Varint Mismatch Bypasses `max_frame_length` Memory Bound — (`network/src/compress.rs`)

---

### Summary

The `LengthDelimitedCodecWithCompress::decode` implementation enforces `max_frame_length` only on the **compressed wire frame**, but allocates a buffer sized by the **snappy-declared uncompressed length** before attempting decompression. An unprivileged remote peer can send a ~1 KB compressed frame (within the Ping protocol's 1024-byte limit) whose snappy varint header declares an uncompressed length of up to 8,388,607 bytes, causing the decoder to allocate ~8 MB per frame — an 8192× amplification ratio that directly violates the invariant that `max_frame_length` bounds per-message memory cost.

---

### Finding Description

**Root cause — `decode` in `network/src/compress.rs`:**

The decoder path is:

1. `self.length_delimited.decode(src)?` — enforces `max_frame_length` (1024 bytes for Ping) on the **compressed** wire frame. [1](#0-0) 

2. If `COMPRESS_FLAG` is set, `decompress_len(&data[1..])` reads the snappy varint from the attacker-controlled payload. [2](#0-1) 

3. The only guard is `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` where `MAX_UNCOMPRESSED_LEN = 1 << 23` (8,388,608 bytes). A declared length of 8,388,607 passes this check. [3](#0-2) [4](#0-3) 

4. **Before** attempting decompression, the decoder allocates: `let mut buf = BytesMut::zeroed(decompressed_bytes_len);` — up to ~8 MB. [5](#0-4) 

5. `SnapDecoder::new().decompress(...)` then fails (the actual compressed body does not expand to 8 MB), the error is returned, and the connection is dropped — but the allocation already occurred. [6](#0-5) 

There is no check that `decompressed_bytes_len` is bounded by `max_frame_length` or any per-protocol limit. The `process` (encoder) function does check `len > self.length_delimited.max_frame_length()`, but this is only on the **outbound** path and has no effect on inbound decoding. [7](#0-6) 

**Protocol configuration:**

The Ping protocol sets `max_frame_length = 1024` (1 KB). [8](#0-7) 

The codec is wired directly from `max_frame_length` into `LengthDelimitedCodecWithCompress` for every protocol. [9](#0-8) 

The same amplification applies to other small-frame protocols: Identify (2 KB → 8 MB, 4096×), Feeler (1 KB → 8 MB, 8192×), DisconnectMessage (1 KB → 8 MB, 8192×), Time (1 KB → 8 MB, 8192×). [10](#0-9) 

**Default peer limits:**

`max_peers = 125`, `max_outbound_peers = 8`, so `max_inbound = 117`. [11](#0-10) 

---

### Impact Explanation

With 117 simultaneous inbound connections each sending one crafted Ping frame, the node transiently allocates 117 × ~8 MB ≈ **~936 MB** of heap memory. Even though each allocation is freed after the decompression failure, the peak RSS spike is sufficient to trigger OOM on memory-constrained nodes or cause significant GC/allocator pressure. The attacker can reconnect (no automatic ban is applied for `InvalidData` errors in this path) and repeat the cycle continuously, sustaining memory pressure. The attack requires no authentication, no PoW, and no valid CKB message content.

---

### Likelihood Explanation

The attack is fully reachable from any unprivileged P2P peer. Constructing a valid snappy stream with an arbitrary varint header and a small body is trivial — the snappy format places the uncompressed-length varint at the very start of the stream, and `decompress_len` reads only that varint without validating the rest of the stream. The attacker needs only a TCP connection to port 8115 and the ability to send ~1 KB of crafted bytes.

---

### Recommendation

In `LengthDelimitedCodecWithCompress::decode`, after reading `decompressed_bytes_len`, add a second guard:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This ensures the declared uncompressed length is bounded by the same per-protocol limit that governs the compressed wire frame, eliminating the amplification entirely. The same fix should be applied symmetrically in `Message::decompress` (the `decompress` free function path). [12](#0-11) 

---

### Proof of Concept

```python
import socket, struct

# Snappy stream: varint = 8388607 (just under MAX_UNCOMPRESSED_LEN), body = garbage
# Snappy varint encoding of 8388607 = 0xFF, 0xFF, 0xFF, 0x03 (little-endian base-128)
snappy_varint = bytes([0xFF, 0xFF, 0xFF, 0x03])
# Minimal valid-looking snappy literal tag (will fail decompression, but allocation happens first)
snappy_body = bytes([0x00] * 10)  # garbage compressed body

payload = snappy_varint + snappy_body

# Frame format: 1 byte flag (COMPRESS_FLAG=0x80) + payload
frame_body = bytes([0x80]) + payload

# LengthDelimited: 4-byte big-endian length prefix
frame = struct.pack(">I", len(frame_body)) + frame_body

# Send on Ping protocol channel (after tentacle handshake)
# frame is ~17 bytes on the wire; decoder allocates ~8MB before failing
s = socket.create_connection(("target-node", 8115))
# ... perform tentacle/secio handshake, open Ping substream ...
s.sendall(frame)
# Node allocates BytesMut::zeroed(8388607) then returns InvalidData, drops connection
# Repeat with 117 simultaneous connections for ~936MB peak allocation
```

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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
