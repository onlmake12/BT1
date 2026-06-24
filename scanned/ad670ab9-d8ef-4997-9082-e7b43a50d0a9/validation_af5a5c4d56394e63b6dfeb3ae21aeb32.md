Audit Report

## Title
Decompression Size Check Uses Global Constant Instead of Per-Protocol Frame Limit, Allowing Memory Amplification — (`network/src/compress.rs`)

## Summary

`LengthDelimitedCodecWithCompress::decode` validates decompressed payload size against the global constant `MAX_UNCOMPRESSED_LEN = 8MB` rather than `self.length_delimited.max_frame_length()`. For RelayV3 (wire limit = 4MB), a peer can send a compressed frame that decompresses to 8MB — a 2× amplification. For protocols with smaller limits (Ping at 1KB, Discovery at 512KB), the amplification reaches 8192× and 16× respectively. All protocols using `CKBProtocol::new_with_support_protocol` are affected since compression is enabled by default.

## Finding Description

`MAX_UNCOMPRESSED_LEN` is defined as a module-level constant of 8MB: [1](#0-0) 

RelayV3's per-protocol wire limit is 4MB: [2](#0-1) 

In `LengthDelimitedCodecWithCompress::decode`, after `self.length_delimited.decode(src)` enforces the wire limit (e.g., 4MB for RelayV3), the decompressed-size check at line 235 uses the global 8MB constant — not the codec's own `max_frame_length`: [3](#0-2) 

The encoder's `process` method correctly uses `self.length_delimited.max_frame_length()` for its size check, but the decoder never consults it: [4](#0-3) 

All `CKBProtocol::new_with_support_protocol` instances set `compress: true` by default, and `build()` wires up `LengthDelimitedCodecWithCompress` with the per-protocol `max_frame_length` passed to the inner `LengthDelimitedCodec` — but the decoder ignores it: [5](#0-4) [6](#0-5) 

The exploit path is:
1. `LengthDelimitedCodec` accepts a frame ≤ 4MB (wire limit enforced).
2. `decompress_len()` returns up to 8MB — passes the `MAX_UNCOMPRESSED_LEN` check.
3. `BytesMut::zeroed(decompressed_bytes_len)` allocates up to 8MB.
4. Decompression succeeds; the 8MB buffer is returned to the protocol handler as a valid message.
5. The session stays alive; the attacker repeats continuously.

For Ping (1KB wire limit), the same global 8MB cap creates an 8192× amplification factor — a single 1KB compressed frame can cause an 8MB allocation.

## Impact Explanation

This matches **High: Vulnerabilities which could easily crash a CKB node**. An attacker with only an established P2P session can continuously stream compressed frames that cause allocations far exceeding the protocol's intended limit. Across many concurrent peers, this drives unbounded memory growth. The Ping protocol's 8192× amplification (1KB wire → 8MB allocation) is particularly severe: an attacker can flood a node with tiny Ping frames, each triggering an 8MB allocation, rapidly exhausting available memory and crashing the node.

## Likelihood Explanation

- Requires only an established P2P session — no privilege, no PoW, no key material.
- Compression is enabled by default for all `CKBProtocol` instances.
- The snappy frame format is public; crafting a frame with a specific claimed decompressed length is trivial.
- Valid compressed payloads (e.g., compressed zero-filled buffers) are straightforward to construct.
- The session remains alive after successful decompression, allowing continuous exploitation.

## Recommendation

In `LengthDelimitedCodecWithCompress::decode`, replace the check against the global `MAX_UNCOMPRESSED_LEN` with a check against `self.length_delimited.max_frame_length()`:

```rust
// current (line 235):
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {

// fixed:
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
```

This enforces the invariant that decompressed output never exceeds the per-protocol wire limit, eliminating the amplification uniformly across all protocols.

## Proof of Concept

```
1. Connect to victim as a Ping peer (/ckb/ping, wire limit = 1KB).
2. Construct a snappy frame:
   - Payload: compress(vec![0u8; 8 * 1024 * 1024])  // 8MB zeros → ~8KB compressed
   - Prepend COMPRESS_FLAG byte (0x80)
   - Wrap in 4-byte length-delimited frame (total < 1KB wire limit? No — use RelayV3 for 4MB limit)
   
   For RelayV3 (4MB wire limit):
   - Payload: compress(vec![0u8; 8 * 1024 * 1024])  // 8MB zeros → ~8KB compressed
   - Prepend COMPRESS_FLAG byte (0x80)
   - Wrap in 4-byte length-delimited frame (total ~8KB, well under 4MB wire limit)
3. Send frame.
4. Victim decoder:
   - LengthDelimitedCodec accepts (~8KB < 4MB) ✓
   - decompress_len() returns 8_388_608 ✓
   - 8_388_608 <= MAX_UNCOMPRESSED_LEN (8MB) → check passes ✓
   - BytesMut::zeroed(8_388_608) allocated ✓
   - SnapDecoder::decompress succeeds ✓
   - 8MB BytesMut returned to handler
5. Repeat continuously on the same session.
Assert: received buffer len (8MB) > RelayV3::max_frame_length() (4MB) — invariant violated.

Unit test: construct a LengthDelimitedCodecWithCompress with max_frame_length=4MB,
feed it a valid snappy-compressed 8MB payload in a <4MB wire frame,
assert decode() returns Ok(buf) where buf.len() == 8MB > 4MB.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L142-148)
```rust
    fn process(&self, data: &[u8], flag: u8, dst: &mut BytesMut) -> Result<(), io::Error> {
        let len = data.len() + 1;
        if len > self.length_delimited.max_frame_length() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "data too large",
            ));
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
