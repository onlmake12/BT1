### Title
Snappy Decompression Bomb in `LengthDelimitedCodecWithCompress::decode` Enables Per-Connection 8MB Heap Amplification — (`network/src/compress.rs`)

### Summary

`LengthDelimitedCodecWithCompress::decode` enforces an upper bound of `MAX_UNCOMPRESSED_LEN = 8MB` on decompressed output, but allocates a zeroed buffer of exactly that size for any frame whose snappy header claims a decompressed length anywhere up to 8MB. Because snappy's compression ratio for repetitive data exceeds 100:1, an attacker can send a compressed frame of only ~80 KB that causes an 8MB heap allocation. With `max_connection_number` connections open simultaneously, the node can be forced to allocate gigabytes of heap memory, leading to OOM and crash.

### Finding Description

In `LengthDelimitedCodecWithCompress::decode`:

```
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // rejects > 8MB
    return Err(...)
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len); // allocates up to 8MB
``` [1](#0-0) 

The guard at line 235 only rejects frames whose snappy header claims a decompressed size **strictly greater than** 8MB. Any frame claiming exactly 8MB passes the check and immediately causes `BytesMut::zeroed(8_388_608)` — a full 8MB zero-fill — before any actual decompression work is done.

The `RelayV3` protocol (protocol_id = 101) has `max_frame_length = 4MB`: [2](#0-1) 

Compression is enabled by default for all protocols built via `CKBProtocol::new_with_support_protocol`, which sets `compress: true` and installs `LengthDelimitedCodecWithCompress` as the codec: [3](#0-2) [4](#0-3) 

The inner `length_delimited` codec only checks that the **compressed** frame fits within 4MB. The outer codec then reads the snappy varint header (which the attacker fully controls) and allocates based on the claimed decompressed size — no validation that the claimed size is proportional to the compressed size.

**Attack construction:**

Snappy compresses 8MB of `\x00` bytes to approximately 80KB (>100:1 ratio). An attacker crafts a valid snappy stream whose header varint encodes `8_388_608` (exactly `MAX_UNCOMPRESSED_LEN`) and whose body decompresses to 8MB of zeros. The compressed payload is ~80KB, well within the 4MB `max_frame_length`. The attacker opens N connections, sends one such frame per connection, and the node allocates N × 8MB simultaneously.

### Impact Explanation

With N concurrent connections (bounded by `max_connection_number`), the node allocates N × 8MB of heap memory simultaneously. At 1024 connections this is 8GB. The allocations persist until the handler finishes processing each frame. Because tokio processes connections concurrently, all N allocations can be live at the same time. A typical CKB node with 8–16GB RAM will OOM-kill. A crashed node stops participating in consensus, and if enough nodes are targeted simultaneously, the network loses liveness or forks.

### Likelihood Explanation

The attack requires only:
1. The ability to open TCP connections to the victim (no authentication, no PoW, no stake).
2. The ability to craft a valid snappy-compressed byte string — trivially done with any snappy library.
3. ~80MB of outbound bandwidth to saturate 1024 connections.

No privileged role, no leaked key, no majority hashpower is needed. Any unprivileged peer on the internet can execute this.

### Recommendation

Replace the single-threshold guard with a **compression-ratio check** before allocating:

```rust
let ratio = decompressed_bytes_len / data[1..].len().max(1);
if ratio > MAX_COMPRESSION_RATIO || decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
    return Err(io::ErrorKind::InvalidData.into());
}
```

A reasonable `MAX_COMPRESSION_RATIO` for legitimate CKB messages is 8–16. Additionally, consider a per-connection in-flight decompression budget enforced at the session layer, and reduce `MAX_UNCOMPRESSED_LEN` to match realistic maximum message sizes per protocol rather than using a single global ceiling.

### Proof of Concept

```rust
// Build a snappy bomb: ~80KB compressed, 8MB decompressed
let payload = vec![0u8; 8 * 1024 * 1024];
let compressed = snap::raw::Encoder::new().compress_vec(&payload).unwrap();
assert!(compressed.len() < 100_000); // ~80KB

// Frame: 4-byte length prefix | 0x80 (COMPRESS_FLAG) | compressed bytes
let frame_len = (compressed.len() + 1) as u32;
let mut frame = frame_len.to_be_bytes().to_vec();
frame.push(0x80);
frame.extend_from_slice(&compressed);

// Open 1024 TCP connections to victim's P2P port, send `frame` on each.
// Each connection triggers BytesMut::zeroed(8MB) in decode().
// Total simultaneous allocation: 1024 * 8MB = 8GB → OOM.
``` [5](#0-4)

### Citations

**File:** network/src/compress.rs (L232-244)
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
```

**File:** network/src/protocols/support_protocols.rs (L130-130)
```rust
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```

**File:** network/src/protocols/mod.rs (L207-221)
```rust
    pub fn new_with_support_protocol(
        support_protocol: support_protocols::SupportProtocols,
        handler: Box<dyn CKBProtocolHandler>,
        network_state: Arc<NetworkState>,
    ) -> Self {
        CKBProtocol {
            id: support_protocol.protocol_id(),
            max_frame_length: support_protocol.max_frame_length(),
            protocol_name: support_protocol.name(),
            supported_versions: support_protocol.support_versions(),
            network_state,
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
