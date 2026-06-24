Audit Report

## Title
Pre-Decompression Heap Allocation via Attacker-Controlled Snappy Uncompressed-Length Varint — (`network/src/compress.rs`)

## Summary
In both `LengthDelimitedCodecWithCompress::decode` and `Message::decompress`, the node allocates a heap buffer sized by the snappy stream's uncompressed-length varint before attempting decompression. A remote peer with no authentication can craft a frame whose snappy varint claims up to 8,388,607 bytes while the actual compressed payload is a handful of bytes, causing an ~8 MB heap allocation that is freed only after decompression fails and the connection is closed. Sustained across many connections, this enables heap exhaustion and OOM on memory-constrained nodes.

## Finding Description
`MAX_UNCOMPRESSED_LEN` is defined as `1 << 23` (8,388,608). [1](#0-0) 

In `LengthDelimitedCodecWithCompress::decode`, the guard uses a strict `>` comparison, so a varint value of exactly `8,388,607` (MAX − 1) passes without rejection. [2](#0-1) 

Immediately after the guard, a full zero-initialized buffer is allocated based on the attacker-supplied varint value, before any decompression is attempted: [3](#0-2) 

Only then is `SnapDecoder::decompress` called. If the actual payload does not decompress to the claimed size, the call returns an error, `buf` is dropped, and the connection is closed — but the ~8 MB allocation has already occurred and been freed. [4](#0-3) 

The identical pattern exists in `Message::decompress`, where `vec![0; decompressed_bytes_len]` is allocated under the same guard condition before decompression is attempted. [5](#0-4) 

The `max_frame_length` check (enforced by `length_delimited.decode`) bounds only the compressed wire frame size (e.g., 1 KB for Ping, 4 MB for RelayV3), not the claimed decompressed size. A Ping frame of a few bytes can carry a varint claiming 8,388,607 bytes of decompressed output. [6](#0-5) 

Compression is enabled by default for every protocol built with `CKBProtocol::new_with_support_protocol` and `CKBProtocol::new`. [7](#0-6) 

## Impact Explanation
Each crafted frame (a few bytes on the wire) forces an ~8 MB heap allocation. With the default peer limit, an attacker controlling many inbound connections and cycling them continuously can induce repeated large allocations and deallocations. On memory-constrained nodes this leads to OOM and process termination. This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
- Requires only a TCP connection to the P2P port — no authentication, no PoW, no stake.
- Compression is on by default for all CKB protocols built via `CKBProtocol`.
- Crafting a valid snappy varint header with a minimal body is trivial; the snappy format is public and the varint encoding is straightforward (8,388,607 encodes as `0xFF 0xFF 0xFF 0x03`).
- The peer registry enforces a connection cap but does not rate-limit reconnections, so the attacker can cycle connections continuously to maintain sustained heap pressure.

## Recommendation
Do not trust the snappy header varint for allocation sizing. Before calling `BytesMut::zeroed` or `vec![0; ...]`, cap the allocation at a value derived from the compressed frame size: the snappy maximum expansion ratio is ~1.004×, so `min(decompressed_bytes_len, compressed_len * 2)` is a safe upper bound. Alternatively, use a streaming/incremental decompressor that writes into a pre-capped ring buffer rather than a single pre-allocated slab. Additionally, add a per-connection message-rate limit to slow reconnect cycling.

## Proof of Concept
```rust
// Craft a snappy frame: varint = 8_388_607, body = valid 1-byte snappy literal
// Snappy uncompressed-length varint for 8_388_607 = 0xFF, 0xFF, 0xFF, 0x03
// (127 + 127<<7 + 127<<14 + 3<<21 = 8_388_607)
// Minimal snappy body that decompresses to 1 byte: 0x01, 0x00, 0x61 (len=1, literal 'a')
let varint: &[u8] = &[0xFF, 0xFF, 0xFF, 0x03];
let body:   &[u8] = &[0x01, 0x00, 0x61];
let payload = [varint, body].concat();

// Prepend CKB compress flag (0x80) and 4-byte big-endian length-delimited header
let frame_len = (1 + payload.len()) as u32;
let mut frame = frame_len.to_be_bytes().to_vec();
frame.push(0x80); // COMPRESS_FLAG
frame.extend_from_slice(&payload);

// Send `frame` to the target node's P2P port on any compressed protocol (e.g., Ping).
// Each receipt causes BytesMut::zeroed(8_388_607) before decompression fails.
// Repeat across many connections for ~1 GB peak heap pressure per cycle.
// Cycle connections continuously to maintain sustained pressure.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L74-88)
```rust
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
```

**File:** network/src/compress.rs (L235-240)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
```

**File:** network/src/compress.rs (L242-242)
```rust
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

**File:** network/src/protocols/mod.rs (L217-220)
```rust
            network_state,
            handler,
            compress: true,
        }
```
