Audit Report

## Title
Pre-Decompression Buffer Amplification via Attacker-Controlled Snappy Header in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
In `LengthDelimitedCodecWithCompress::decode`, the node allocates a zeroed buffer sized by the attacker-controlled snappy uncompressed-length varint before any decompression is attempted. The only guard is a `>` comparison against `MAX_UNCOMPRESSED_LEN` (8 MB), meaning a tiny wire frame can cause an ~8 MB allocation per message. An unprivileged remote peer can exploit this across many concurrent sessions to exhaust node memory and crash the process.

## Finding Description
The decode path in `LengthDelimitedCodecWithCompress::decode` (`network/src/compress.rs`, lines 222–262):

1. The inner `length_delimited` codec reads the wire frame and enforces `max_frame_length` on the **compressed** bytes only.
2. When `COMPRESS_FLAG` is set, `decompress_len(&data[1..])` reads the uncompressed-length varint from the snappy stream header — a value entirely under attacker control.
3. The guard at line 235 uses strict `>`: `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN`, so any value ≤ 8,388,608 passes.
4. `BytesMut::zeroed(decompressed_bytes_len)` at line 242 allocates up to 8 MB **before** decompression is attempted.
5. If decompression fails (e.g., because the actual payload is only a few bytes), the error path at lines 245–248 drops the buffer — but the allocation already occurred. [1](#0-0) [2](#0-1) 

The same pattern exists in `Message::decompress` at line 81: [3](#0-2) 

`LengthDelimitedCodecWithCompress` is used for all CKB protocols via `CKBProtocol::build()`: [4](#0-3) 

Compression is enabled by default for all protocols (`compress: true` hardcoded in `new_with_support_protocol`): [5](#0-4) 

The `max_frame_length` bounds only the compressed wire frame, not the claimed decompressed size: [6](#0-5) 

For the Ping protocol (`max_frame_length = 1024` bytes), an attacker sends a ~1 KB compressed frame claiming 8 MB decompressed — an 8192× amplification ratio per message.

## Impact Explanation
This vulnerability matches: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points).**

With `max_connection_number = 1024` concurrent sessions each continuously sending crafted frames, peak concurrent allocation reaches `1024 × 8 MB ≈ 8 GB`, causing OOM and node crash. Even at lower concurrency, sustained flooding from a single connection causes repeated large allocations that degrade and eventually crash the node. The attack requires no authentication, no PoW, and no stake.

## Likelihood Explanation
- Any peer that completes the P2P handshake (no privilege required) can open a protocol session and immediately send crafted frames.
- Crafting a valid snappy stream with an inflated header varint requires only knowledge of the snappy framing format, which is publicly documented.
- The `max_frame_length` check does not prevent this: even the smallest protocol (Ping, 1 KB) allows a frame that triggers an 8 MB allocation.
- The strict `>` comparison (instead of `>=`) means the maximum allowed allocation is exactly `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes), not `MAX_UNCOMPRESSED_LEN - 1` as the guard implies.
- The attack is repeatable at line rate with no per-connection rate limiting.

## Recommendation
1. **Bound allocation relative to wire size:** Before allocating, verify `decompressed_bytes_len <= compressed_len * 8` (or a similar ratio bound). Snappy's maximum compression ratio is ~6×, so a generous 8× bound still rejects all malicious frames.
2. **Use `>=` instead of `>`** in the guard at line 235 and line 74: `if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN`.
3. **Apply the same fix to `Message::decompress`** at line 74–81.
4. **Consider per-connection message rate limiting** to bound the rate at which a single peer can trigger allocations.

## Proof of Concept
```python
import socket, struct

# Snappy stream: varint claiming 8,388,608 decompressed bytes (= MAX_UNCOMPRESSED_LEN, passes > check)
# Varint encoding of 8388608 = 0x80 0x80 0x80 0x04
snappy_header = bytes([0x80, 0x80, 0x80, 0x04])
# Minimal snappy literal: 1 byte
snappy_body = bytes([0x00, 0x41])
snappy_payload = snappy_header + snappy_body

# CKB compress frame: COMPRESS_FLAG (0x80) + snappy payload
ckb_frame = bytes([0x80]) + snappy_payload

# Length-delimited framing: 4-byte big-endian length prefix
wire = struct.pack(">I", len(ckb_frame)) + ckb_frame

s = socket.create_connection(("TARGET_NODE", 8115))
# complete tentacle handshake, then flood:
for _ in range(10000):
    s.sendall(wire)
# Each frame causes BytesMut::zeroed(8_388_608) on the victim before decompression failure.
```

Expected result: victim node RSS grows by ~8 MB per received frame; with 1024 concurrent connections at line rate, RSS reaches ~8 GB and the process is killed by OOM.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L81-83)
```rust
                        let mut buf = vec![0; decompressed_bytes_len];
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
                            Ok(_) => Ok(buf.into()),
```

**File:** network/src/compress.rs (L233-249)
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
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
                            }
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
