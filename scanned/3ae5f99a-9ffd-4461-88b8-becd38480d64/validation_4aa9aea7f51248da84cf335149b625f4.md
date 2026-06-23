### Title
Decompression Size Check Uses Global Constant Instead of Per-Protocol Frame Limit, Allowing 2× Memory Amplification — (`network/src/compress.rs`)

### Summary

The `LengthDelimitedCodecWithCompress::decode` function validates decompressed payload size against a global constant `MAX_UNCOMPRESSED_LEN = 8MB` rather than the per-protocol `max_frame_length`. For RelayV3 (wire limit = 4MB), this allows any peer to send a 4MB compressed frame that causes an 8MB allocation — a confirmed 2× amplification. The invariant "decompressed bytes ≤ max_frame_length" is broken.

### Finding Description

`MAX_UNCOMPRESSED_LEN` is defined as a module-level constant: [1](#0-0) 

RelayV3's wire limit is 4MB: [2](#0-1) 

In `LengthDelimitedCodecWithCompress::decode`, after the `LengthDelimitedCodec` accepts a frame (enforcing the 4MB wire limit), the decompressed-size check uses the global 8MB constant — not the codec's own `max_frame_length`: [3](#0-2) 

The `LengthDelimitedCodecWithCompress` is constructed with `max_frame_length` available via `self.length_delimited.max_frame_length()` (used in the encoder's `process` method at line 144), but the decoder never consults it. The 8MB allocation at line 242 proceeds for any frame whose snappy header claims ≤ 8MB, regardless of the protocol's actual frame limit. [4](#0-3) 

The codec is wired up with `compress: true` by default for all `CKBProtocol::new_with_support_protocol` instances: [5](#0-4) 

### Impact Explanation

**Two scenarios:**

1. **Invalid compressed payload (crafted snappy header, garbage body):** The 8MB `BytesMut::zeroed` is allocated at line 242, decompression fails, the error propagates, and the connection is dropped. The allocation is transient but real. The attacker must reconnect to repeat — bounded by reconnect rate and peer slot limits.

2. **Valid compressed payload (e.g., 4MB of snappy-compressed zeros expanding to 8MB):** Decompression succeeds. The 8MB `BytesMut` is returned to the protocol handler as a valid message. The connection stays alive. The attacker can continuously stream such frames, keeping 8MB buffers in flight per session across all connected peers.

The amplification factor for RelayV3 is 2×. For protocols with smaller limits (e.g., Ping at 1KB, Discovery at 512KB), the same global 8MB cap creates amplification factors of 8192× and 16× respectively — though those are outside the stated scope.

### Likelihood Explanation

- Requires only an established P2P session (no privilege, no PoW, no key).
- Compression is enabled by default for all `CKBProtocol` instances.
- The snappy header format is public; crafting a frame with a specific claimed decompressed length is trivial.
- Scenario 2 (valid compressed data) is straightforward: compress a large zero-filled buffer.

### Recommendation

In `LengthDelimitedCodecWithCompress::decode`, replace the check against the global `MAX_UNCOMPRESSED_LEN` with a check against `self.length_delimited.max_frame_length()`:

```rust
// current (line 235):
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {

// fixed:
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
```

This enforces the invariant that decompressed output never exceeds the per-protocol wire limit, eliminating the amplification for all protocols uniformly.

### Proof of Concept

```
1. Connect to victim as a RelayV3 peer (/ckb/relay3).
2. Construct a snappy frame:
   - Payload: compress(vec![0u8; 8 * 1024 * 1024])  // 8MB zeros → ~8KB compressed
   - Prepend COMPRESS_FLAG byte (0x80)
   - Wrap in 4-byte length-delimited frame (total < 4MB wire limit)
3. Send frame.
4. Victim decoder:
   - LengthDelimitedCodec accepts (< 4MB) ✓
   - decompress_len() returns 8_388_608 ✓
   - 8_388_608 <= MAX_UNCOMPRESSED_LEN (8MB) → check passes ✓
   - BytesMut::zeroed(8_388_608) allocated ✓
   - SnapDecoder::decompress succeeds ✓
   - 8MB BytesMut returned to handler
5. Repeat continuously on the same session.
Assert: received buffer len (8MB) > RelayV3::max_frame_length() (4MB) — invariant violated.
```

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L142-145)
```rust
    fn process(&self, data: &[u8], flag: u8, dst: &mut BytesMut) -> Result<(), io::Error> {
        let len = data.len() + 1;
        if len > self.length_delimited.max_frame_length() {
            return Err(io::Error::new(
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
