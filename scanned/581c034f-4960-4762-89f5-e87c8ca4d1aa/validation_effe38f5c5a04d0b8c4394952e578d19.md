The code is confirmed. Here is the analysis:

---

### Title
Per-Protocol Frame Limit Bypassed at Decompression Layer — (`network/src/compress.rs`)

### Summary

`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the **compressed** wire bytes. After accepting a frame, it checks the snappy-header-declared decompressed size only against the global `MAX_UNCOMPRESSED_LEN` (8 MB), never against the per-protocol `max_frame_length`. Any unprivileged peer can therefore cause the node to allocate up to 8 MB per message on RelayV3, which has a 4 MB protocol limit — a 2× amplification — and up to 8 MB on Sync, which has a 2 MB limit (4× amplification).

### Finding Description

`MAX_UNCOMPRESSED_LEN` is set to 8 MB: [1](#0-0) 

`RelayV3` declares a 4 MB per-protocol frame limit: [2](#0-1) 

In `decode`, after `self.length_delimited.decode(src)?` accepts the compressed frame (enforcing `max_frame_length` on the wire), the only guard on the decompressed size is the global constant — there is no comparison against `self.length_delimited.max_frame_length()`: [3](#0-2) 

The encoder-side `process()` does enforce `max_frame_length` on the data it writes: [4](#0-3) 

But this check is absent on the decoder path. `self.length_delimited.max_frame_length()` is accessible in `decode` (it is the same field used by `process`), so the omission is not structural — it is simply a missing guard.

### Impact Explanation

A peer sends a compressed RelayV3 frame whose compressed size is just under 4 MB but whose snappy header declares a decompressed size just under 8 MB. The decoder:

1. Accepts the frame (≤ 4 MB, within `max_frame_length`).
2. Reads `decompress_len` → ~8 MB.
3. Passes the `> MAX_UNCOMPRESSED_LEN` check (8 MB is not exceeded).
4. Allocates `BytesMut::zeroed(~8 MB)`.
5. Returns the 8 MB buffer to the relay handler.

The per-protocol memory budget is doubled per message. With concurrent peers, this multiplies. The relay handler's rate limiter (30 req/s per peer) fires **after** the codec layer allocates the buffer, so it does not prevent the allocation.

The same path applies to Sync (2 MB limit → up to 8 MB decompressed, 4× amplification). [5](#0-4) 

### Likelihood Explanation

The attacker needs only a standard P2P connection — no credentials, no PoW, no privileged role. Crafting a snappy-compressed payload that satisfies the ratio is straightforward (highly repetitive input compresses well beyond 2:1 with snappy). The path is reachable on mainnet by any peer.

### Recommendation

In `decode`, after the `decompress_len` call, add a check against the per-protocol limit before allocating:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN
    || decompressed_bytes_len > self.length_delimited.max_frame_length()
{
    return Err(io::ErrorKind::InvalidData.into());
}
``` [6](#0-5) 

### Proof of Concept

```rust
// Unit test sketch (RelayV3 codec, max_frame_length = 4 MB)
let max = 4 * 1024 * 1024;
let codec = LengthDelimitedCodecWithCompress::new(
    true,
    length_delimited::Builder::new().max_frame_length(max).new_codec(),
    SupportProtocols::RelayV3.protocol_id(),
);

// Build a snappy payload whose compressed form is < 4 MB
// but whose declared decompressed length is > 4 MB (e.g., 7 MB of zeros).
let raw = vec![0u8; 7 * 1024 * 1024];
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
assert!(compressed.len() < max);  // fits on the wire

// Encode manually: 4-byte length prefix + COMPRESS_FLAG byte + compressed payload
let mut frame = BytesMut::new();
frame.put_uint((compressed.len() + 1) as u64, 4);
frame.put_u8(0b1000_0000); // COMPRESS_FLAG
frame.extend_from_slice(&compressed);

// decode() should return Err because decompressed_len (7 MB) > max_frame_length (4 MB)
let result = codec.decode(&mut frame);
assert!(result.is_err(), "expected Err: decompressed size exceeds max_frame_length");
```

Currently this test **fails** (decode returns `Ok(7 MB buffer)`), confirming the missing guard.

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
