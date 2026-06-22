### Title
Attacker-Controlled Snappy Decompressed-Length Field Triggers Up-to-8 MB Allocation Per P2P Message Before Validation — (`network/src/compress.rs`)

---

### Summary

`LengthDelimitedCodecWithCompress::decode()` reads the declared uncompressed size from an attacker-controlled snappy stream header and allocates up to `MAX_UNCOMPRESSED_LEN` (8 MB) of memory **before** attempting decompression. Because the declared size is never validated against the actual compressed payload size, any unauthenticated P2P peer can send a tiny compressed frame (as small as ~5 bytes on the wire) that forces the node to allocate ~8 MB per message. This is a direct analog to the Firedancer bug: in both cases an attacker-controlled length field embedded in a network message is consumed by the parser without cross-checking it against the actual data present, leading to resource exhaustion.

---

### Finding Description

The codec used for every CKB P2P protocol that has compression enabled is `LengthDelimitedCodecWithCompress`, built in `network/src/protocols/mod.rs` and applied to Sync (2 MB frame limit), RelayV3 (4 MB frame limit), LightClient (2 MB), and others. [1](#0-0) 

Its `decode` path is: [2](#0-1) 

Step-by-step:

1. `self.length_delimited.decode(src)` returns a framed `BytesMut` whose size is bounded by `max_frame_length` (e.g. 4 MB for RelayV3).
2. If `data[0] & COMPRESS_FLAG != 0`, `decompress_len(&data[1..])` is called. This reads the **varint-encoded uncompressed length** from the snappy stream header — a field that is entirely attacker-controlled and requires only ~4 bytes to encode a value of ~8 MB.
3. The only guard is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (8 MB). A value of `MAX_UNCOMPRESSED_LEN − 1` passes.
4. `BytesMut::zeroed(decompressed_bytes_len)` allocates up to **8 MB** of zeroed memory.
5. `SnapDecoder::new().decompress(...)` is then called. If the actual payload does not decompress to the declared size, it returns an error and the buffer is freed — but the allocation already occurred. [3](#0-2) 

The snappy format places the uncompressed-length varint at the very start of the compressed stream. An attacker can craft a frame consisting of:

- 1 byte: `COMPRESS_FLAG` (0x80)
- 4 bytes: varint encoding `MAX_UNCOMPRESSED_LEN − 1` (~8 MB)
- 0–N bytes: garbage or truncated snappy blocks

Total wire size: **~5 bytes**. Allocation triggered: **~8 MB**. Amplification ratio: **~1,600,000×**.

The `max_frame_length` check only bounds the compressed frame size, not the declared uncompressed size. There is no check that `decompressed_bytes_len` is plausible given `data[1..].len()`. [4](#0-3) 

---

### Impact Explanation

An unauthenticated peer can repeatedly send minimal compressed frames, each forcing an ~8 MB allocation on the receiving node. With a sustained stream of such frames across multiple connections, the node's heap grows rapidly, leading to:

- **Out-of-memory process termination** (OOM kill), causing a full node crash and denial of service.
- **Severe memory pressure** degrading block/transaction processing, sync, and RPC responsiveness.

Because the allocation and decompression happen at the codec layer — before any protocol-level rate limiting or peer banning — the attacker can exhaust memory before any defensive action is taken.

---

### Likelihood Explanation

- **No authentication required**: any TCP peer that completes the Tentacle handshake can open the Sync or RelayV3 protocol and send compressed frames.
- **Trivial to craft**: the malicious frame is ~5 bytes and requires no knowledge of CKB internals.
- **No per-message rate limit at the codec layer**: `LengthDelimitedCodecWithCompress::decode` is called synchronously for every arriving frame with no throttling.
- **Multiple protocols affected**: Sync, RelayV3, LightClient, HolePunching, Discovery, and Alert all use `LengthDelimitedCodecWithCompress` with compression enabled by default. [5](#0-4) 

---

### Recommendation

Before allocating the decompression buffer, validate that the declared uncompressed size is consistent with the compressed payload size. Snappy's compression ratio is bounded (it never expands data by more than ~1.5×), so a reasonable guard is:

```rust
// Reject if declared uncompressed size is implausibly large
// relative to the actual compressed payload.
const MAX_SNAPPY_EXPANSION: usize = 2; // conservative upper bound
if decompressed_bytes_len > data[1..].len().saturating_mul(MAX_SNAPPY_EXPANSION)
    && decompressed_bytes_len > SOME_MINIMUM_THRESHOLD
{
    return Err(io::ErrorKind::InvalidData.into());
}
```

Additionally, lower `MAX_UNCOMPRESSED_LEN` to match the actual maximum uncompressed protocol message size (e.g. 4 MB for RelayV3), and apply the same guard in `Message::decompress()`. [6](#0-5) 

---

### Proof of Concept

```python
import socket, struct

# Snappy varint encoding of MAX_UNCOMPRESSED_LEN - 1 = 8388607
def encode_varint(n):
    buf = []
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)
    return bytes(buf)

# Build a minimal "compressed" frame:
# byte 0: COMPRESS_FLAG (0x80)
# bytes 1+: snappy stream = varint(8388607) + no valid blocks
compress_flag = b'\x80'
snappy_payload = encode_varint(8388607)  # claims 8MB-1 uncompressed
frame_payload = compress_flag + snappy_payload

# LengthDelimitedCodec prefix: 4-byte big-endian length
frame = struct.pack('>I', len(frame_payload)) + frame_payload

# After Tentacle handshake, send this frame repeatedly on the Sync protocol
# Each frame forces BytesMut::zeroed(8388607) on the target node
conn = socket.create_connection(('TARGET_IP', 8115))
# ... complete Tentacle/secio handshake ...
for _ in range(1000):
    conn.sendall(frame)
```

Each iteration allocates ~8 MB on the target node before the decompression error is returned. 1,000 rapid frames = ~8 GB of allocation pressure.

### Citations

**File:** network/src/protocols/mod.rs (L218-221)
```rust
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

**File:** network/src/compress.rs (L68-100)
```rust
    pub(crate) fn decompress(mut self) -> Result<Bytes, io::Error> {
        if self.inner.is_empty() {
            Err(io::ErrorKind::InvalidData.into())
        } else if self.compress_flag() {
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
                            Ok(_) => Ok(buf.into()),
                            Err(e) => {
                                debug!("snappy decompress error: {:?}", e);
                                Err(io::ErrorKind::InvalidData.into())
                            }
                        }
                    }
                }
                Err(e) => {
                    debug!("snappy decompress_len error: {:?}", e);
                    Err(io::ErrorKind::InvalidData.into())
                }
            }
        } else {
            let _ = self.inner.split_to(1);
            Ok(self.inner.freeze())
        }
    }
```

**File:** network/src/compress.rs (L219-263)
```rust
impl tokio_util::codec::Decoder for LengthDelimitedCodecWithCompress {
    type Item = BytesMut;
    type Error = io::Error;
    fn decode(&mut self, src: &mut BytesMut) -> Result<Option<BytesMut>, io::Error> {
        if src.is_empty() {
            return Ok(None);
        }
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
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
                            }
                        }
                        Err(e) => {
                            debug!("snappy decompress_len error: {:?}", e);
                            Err(io::ErrorKind::InvalidData.into())
                        }
                    }
                } else {
                    Ok(Some(data.split_off(1)))
                }
            }
            None => Ok(None),
        }
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
