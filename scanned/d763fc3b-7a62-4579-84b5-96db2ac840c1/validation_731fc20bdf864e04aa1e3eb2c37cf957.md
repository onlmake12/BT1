### Title
P2P Snappy Decompression Memory Amplification via Attacker-Controlled Varint Header - (File: `network/src/compress.rs`)

---

### Summary

The `LengthDelimitedCodecWithCompress::decode` and `Message::decompress` functions in `network/src/compress.rs` unconditionally trust the attacker-controlled uncompressed-length varint embedded in the snappy header to pre-allocate a decompression buffer. An unprivileged peer can send a tiny compressed P2P message (a few bytes on the wire) whose snappy varint header claims up to 8 MB of decompressed output, causing the node to allocate up to 8 MB per message before decompression fails. Repeated delivery of such messages exhausts node memory.

---

### Finding Description

CKB's P2P layer wraps all protocol messages (Sync, RelayV3, Discovery, etc.) in `LengthDelimitedCodecWithCompress`, which is installed as the codec for every `CKBProtocol` built via `CKBProtocol::build`. [1](#0-0) 

When a frame arrives with the compress flag set, the decoder calls `snap::raw::decompress_len` on the raw compressed bytes. This function reads the uncompressed length from the snappy varint header — a value that is entirely attacker-controlled — and returns it as a `usize`. [2](#0-1) 

The only guard is a comparison against `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB): [3](#0-2) 

If the claimed length is ≤ 8 MB the code immediately allocates a zeroed buffer of exactly that size before attempting decompression: [4](#0-3) 

The same pattern exists in `Message::decompress`: [5](#0-4) 

**Crafted payload**: An attacker sends a frame whose first byte is `0x80` (compress flag) followed by a snappy varint encoding 8 388 608 (e.g., `0x80 0x80 0x80 0x04`) and then a handful of garbage bytes. The total wire size is ~10 bytes. `decompress_len` returns 8 MB, the size check passes, 8 MB is allocated, `SnapDecoder::decompress` fails on the garbage payload, and the error is returned — but the allocation has already occurred. There is no proportionality check between the compressed input size and the claimed output size.

---

### Impact Explanation

Any unprivileged peer that establishes a TCP connection to a CKB node can send a continuous stream of these ~10-byte crafted frames. Each frame causes an 8 MB heap allocation (amplification ratio ~800 000×). Sustained delivery exhausts the node's available memory, triggering OOM termination or severe swap pressure, effectively taking the node offline. All protocols that use `LengthDelimitedCodecWithCompress` are affected, including the core Sync (2 MB frame limit) and RelayV3 (4 MB frame limit) protocols. [6](#0-5) 

---

### Likelihood Explanation

The attack requires only an inbound or outbound TCP connection to a CKB node — no authentication, no stake, no special role. CKB nodes accept connections from arbitrary peers by default. The crafted frame is trivially constructable from the public snappy specification. The `MAX_UNCOMPRESSED_LEN` guard was introduced to prevent unbounded allocations (GHSA-3gjh-29fv-8hr6) but does not prevent the amplification attack because it only caps the allocation size, not the ratio of allocation to wire bytes.

---

### Recommendation

Before allocating the decompression buffer, validate that the claimed decompressed size is proportional to the compressed input size. For example:

```rust
const MAX_COMPRESSION_RATIO: usize = 256;
if decompressed_bytes_len > compressed_data.len() * MAX_COMPRESSION_RATIO {
    return Err(io::ErrorKind::InvalidData.into());
}
```

Alternatively, replace the pre-allocation pattern with `SnapDecoder::decompress_vec`, which sizes the output buffer internally and avoids trusting the header length for allocation purposes. The same fix must be applied to both `Message::decompress` and `LengthDelimitedCodecWithCompress::decode`. [7](#0-6) 

---

### Proof of Concept

```rust
// Craft a ~10-byte "compressed" message that claims 8 MB decompressed size.
// Snappy raw format: varint(uncompressed_len) || compressed_blocks
// varint(8_388_608) = [0x80, 0x80, 0x80, 0x04]
let crafted_payload: &[u8] = &[
    0x80,                         // CKB compress flag
    0x80, 0x80, 0x80, 0x04,       // snappy varint: 8_388_608 (8 MB)
    0x00, 0x00, 0x00,             // garbage "compressed" bytes
];

use ckb_network::compress::decompress;
use p2p::bytes::BytesMut;

// Each call allocates ~8 MB before returning Err.
for _ in 0..1000 {
    let _ = decompress(BytesMut::from(crafted_payload));
    // ~8 GB of allocations after 1000 iterations
}
```

The `decompress_len` call returns 8 388 608, the size check `8_388_608 > MAX_UNCOMPRESSED_LEN` is false (equal), `BytesMut::zeroed(8_388_608)` allocates 8 MB, `SnapDecoder::decompress` fails on the garbage payload, and the error is returned — but the allocation has already been made and freed. Under concurrent peer connections each sending this frame at line rate, the allocator is overwhelmed. [8](#0-7)

### Citations

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

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
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

**File:** network/src/compress.rs (L232-248)
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
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
```

**File:** network/src/protocols/support_protocols.rs (L122-136)
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
```
