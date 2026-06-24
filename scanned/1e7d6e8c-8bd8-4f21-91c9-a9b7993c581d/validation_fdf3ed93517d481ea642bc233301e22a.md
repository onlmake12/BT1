Audit Report

## Title
Snappy Decompression Bomb via Attacker-Controlled Header Enables Per-Connection 8MB Heap Amplification — (`network/src/compress.rs`)

## Summary

`LengthDelimitedCodecWithCompress::decode` allocates a zeroed buffer of up to `MAX_UNCOMPRESSED_LEN` (8MB) based solely on the attacker-controlled snappy varint header, before any decompression is performed. Because snappy can compress 8MB of zeros into ~80KB, an attacker can send a ~80KB compressed frame that triggers an 8MB heap allocation per connection. With many concurrent connections, this causes unbounded heap growth and OOM crash of the target node.

## Finding Description

In `LengthDelimitedCodecWithCompress::decode` at `network/src/compress.rs` lines 232–244:

```rust
if (data[0] & COMPRESS_FLAG) != 0 {
    match decompress_len(&data[1..]) {
        Ok(decompressed_bytes_len) => {
            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {  // strictly >
                return Err(io::ErrorKind::InvalidData.into());
            }
            let mut buf = BytesMut::zeroed(decompressed_bytes_len); // up to 8MB
``` [1](#0-0) 

`MAX_UNCOMPRESSED_LEN` is defined as `1 << 23 = 8_388_608` (8MB). [2](#0-1) 

The guard uses strict `>`, so a frame whose snappy header claims exactly 8MB passes the check and immediately triggers `BytesMut::zeroed(8_388_608)` — a full 8MB zero-fill — before any decompression work is done. The allocation is driven entirely by the attacker-controlled varint in the snappy stream header, with no validation that the claimed decompressed size is proportional to the actual compressed payload size.

The inner `length_delimited` codec only enforces that the **compressed** frame fits within `max_frame_length`. For `RelayV3` (protocol_id = 101), this is 4MB: [3](#0-2) 

Compression is enabled by default for all protocols built via `CKBProtocol::new_with_support_protocol` (`compress: true`): [4](#0-3) 

`LengthDelimitedCodecWithCompress` is installed as the codec for every such protocol: [5](#0-4) 

**Exploit path:**
1. Attacker compresses 8MB of `\x00` bytes with snappy (~80KB result, >100:1 ratio).
2. Attacker crafts a frame: 4-byte length prefix | `0x80` (COMPRESS_FLAG) | compressed bytes. Total frame size ~80KB, well within the 4MB `max_frame_length`.
3. Attacker opens N TCP connections to the victim's P2P port and sends one such frame per connection.
4. Each connection triggers `BytesMut::zeroed(8_388_608)` in `decode()`.
5. Because tokio processes connections concurrently, all N allocations are live simultaneously: N × 8MB total heap.

No authentication, PoW, or stake is required — any unprivileged peer on the internet can open TCP connections to the P2P port.

## Impact Explanation

This directly maps to the **High** impact class: *"Vulnerabilities which could easily crash a CKB node."* With a realistic number of concurrent connections (e.g., 125 connections × 8MB = 1GB; 1024 connections = 8GB), a typical CKB node with 8–16GB RAM will be OOM-killed. The crash stops the node from participating in block propagation and consensus. The attack is repeatable: after the node restarts, the attacker can immediately re-execute it.

## Likelihood Explanation

Requirements are minimal: (1) ability to open TCP connections to the victim's P2P port (no authentication), (2) ability to produce a valid snappy-compressed byte string (any snappy library), (3) ~80MB of outbound bandwidth to saturate 1024 connections. No privileged role, leaked key, or majority hashpower is needed. Any unprivileged internet peer can execute this.

## Recommendation

Add a compression-ratio check before allocating, rejecting frames where the claimed decompressed size is disproportionate to the compressed payload size:

```rust
let compressed_len = data[1..].len().max(1);
let ratio = decompressed_bytes_len / compressed_len;
if ratio > MAX_COMPRESSION_RATIO || decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
    return Err(io::ErrorKind::InvalidData.into());
}
```

A reasonable `MAX_COMPRESSION_RATIO` for legitimate CKB messages is 8–16. Additionally, consider reducing `MAX_UNCOMPRESSED_LEN` per-protocol to match realistic maximum message sizes rather than using a single global 8MB ceiling.

## Proof of Concept

```rust
// Build a snappy bomb: ~80KB compressed, 8MB decompressed
let payload = vec![0u8; 8 * 1024 * 1024];
let compressed = snap::raw::Encoder::new().compress_vec(&payload).unwrap();
assert!(compressed.len() < 100_000); // ~80KB

// Frame: 4-byte big-endian length | 0x80 (COMPRESS_FLAG) | compressed bytes
let frame_len = (compressed.len() + 1) as u32;
let mut frame = frame_len.to_be_bytes().to_vec();
frame.push(0x80);
frame.extend_from_slice(&compressed);

// Open N TCP connections to victim's P2P port, send `frame` on each.
// Each triggers BytesMut::zeroed(8_388_608) in decode().
// N=1024 → 8GB simultaneous heap → OOM.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

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
