### Title
Attacker-Controlled Pre-Decompression Heap Allocation via Snappy Varint Forgery — (`network/src/compress.rs`)

---

### Summary

In `LengthDelimitedCodecWithCompress::decode`, the node allocates a heap buffer sized by the snappy stream's uncompressed-length varint **before** attempting decompression. An unprivileged remote peer can craft a frame whose snappy varint claims up to `MAX_UNCOMPRESSED_LEN - 1` (8,388,607 bytes) while the actual compressed payload is a handful of bytes. Each such frame causes an ~8 MB heap allocation that is freed only after decompression fails and the connection is closed. The amplification ratio is on the order of 800,000×.

---

### Finding Description

`MAX_UNCOMPRESSED_LEN` is defined as `1 << 23` (8 MB). [1](#0-0) 

The guard in `decode` rejects frames only when `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN`, meaning a value of exactly `8,388,607` (8 MB − 1) passes the check. [2](#0-1) 

Immediately after the guard, the full buffer is zero-initialised based on the attacker-supplied varint:

```rust
let mut buf = BytesMut::zeroed(decompressed_bytes_len);   // line 242
``` [3](#0-2) 

Only then is `SnapDecoder::decompress` called. If the actual payload does not decompress to the claimed size, the call returns an error, `buf` is dropped, and the connection is closed — but the ~8 MB allocation already occurred. [4](#0-3) 

The same pattern exists in `Message::decompress` (used by the older codec path), where `vec![0; decompressed_bytes_len]` is allocated under the same condition. [5](#0-4) 

Compression is **enabled by default** for every protocol built with `CKBProtocol::new_with_support_protocol`. [6](#0-5) 

The default `max_peers` is 125, giving up to ~117 simultaneous inbound connections. [7](#0-6) 

---

### Impact Explanation

Each crafted frame (a few bytes on the wire) forces an ~8 MB heap allocation. With the default peer limit, an attacker controlling 117 inbound connections and timing their frames to arrive concurrently can induce ~936 MB of simultaneous heap pressure. Because `decode` is synchronous and runs on tokio worker threads, the practical concurrency is bounded by the thread-pool size (typically 4–8), giving 32–64 MB per burst. However, the attacker can cycle connections continuously (reconnect after each disconnect) to maintain sustained pressure, causing repeated large allocations and deallocations that degrade allocator performance and can exhaust heap on memory-constrained nodes. The amplification ratio (~800,000×) makes this cheap to sustain.

---

### Likelihood Explanation

- Requires only a TCP connection to the P2P port — no authentication, no PoW, no stake.
- Compression is on by default for all CKB protocols.
- Crafting a valid snappy varint header with a minimal body is trivial (the snappy format is public and the varint encoding is straightforward).
- The peer registry enforces a connection cap but does not rate-limit reconnections, so the attacker can cycle connections continuously.

---

### Recommendation

Do not trust the snappy header varint for allocation sizing. Instead:

1. **Bound allocation by the compressed frame size**: the decompressed output cannot exceed the compressed input by more than the snappy maximum expansion ratio (~1.004×). Reject or cap the allocation at `min(decompressed_bytes_len, compressed_len * 2)` before allocating.
2. **Alternatively**, use a streaming/incremental decompressor that writes into a pre-capped ring buffer rather than a single pre-allocated slab.
3. **Add a per-connection message-rate limit** to slow reconnect cycling.

---

### Proof of Concept

```rust
// Craft a snappy frame: varint = 8_388_607, body = valid 1-byte snappy literal
// Snappy uncompressed-length varint for 8_388_607 = 0xFF, 0xFF, 0xFF, 0x03
// Minimal snappy body that decompresses to 1 byte: 0x01, 0x00, 0x61  (len=1, literal 'a')
let varint: &[u8] = &[0xFF, 0xFF, 0xFF, 0x03];
let body:   &[u8] = &[0x01, 0x00, 0x61];
let payload = [varint, body].concat();

// Prepend CKB compress flag (0x80) and length-delimited header (4-byte big-endian length)
let frame_len = (1 + payload.len()) as u32;
let mut frame = frame_len.to_be_bytes().to_vec();
frame.push(0x80);          // COMPRESS_FLAG
frame.extend_from_slice(&payload);

// Send `frame` to the target node's P2P port.
// Each receipt causes BytesMut::zeroed(8_388_607) before decompression fails.
// Repeat across 117 connections for ~936 MB peak pressure per cycle.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L81-88)
```rust
                        let mut buf = vec![0; decompressed_bytes_len];
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
                            Ok(_) => Ok(buf.into()),
                            Err(e) => {
                                debug!("snappy decompress error: {:?}", e);
                                Err(io::ErrorKind::InvalidData.into())
                            }
                        }
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

**File:** network/src/compress.rs (L242-249)
```rust
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
                            }
```

**File:** network/src/protocols/mod.rs (L218-220)
```rust
            handler,
            compress: true,
        }
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
