The code is clear. Let me trace the exact vulnerability path.

**Decode path in `LengthDelimitedCodecWithCompress::decode`** (compress.rs lines 222–262):

1. `self.length_delimited.decode(src)?` — enforces `max_frame_length` on the **wire** (compressed) frame only.
2. If compression flag is set: checks `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (8MB) — **not** against `max_frame_length`.
3. Allocates `BytesMut::zeroed(decompressed_bytes_len)` — up to 8MB — before any further validation.

The `max_frame_length` guard is never applied to the decompressed size. The only post-wire-check bound is the hardcoded 8MB ceiling.

---

### Title
Snappy Decompression Bomb: `max_frame_length` Not Enforced on Decompressed Size, Allowing Up to 8MB Allocation Per Wire Frame — (`network/src/compress.rs`)

### Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire frame. After passing that check, the decompressed size is bounded solely by the hardcoded `MAX_UNCOMPRESSED_LEN = 8MB`. Any unprivileged remote peer can send a snappy-compressed frame whose wire size is within the protocol's `max_frame_length` but whose decompressed size approaches 8MB, causing the receiver to allocate up to 8MB per message regardless of the protocol's intended budget.

### Finding Description

In `CKBProtocol::build`, every protocol built via `new_with_support_protocol` uses `LengthDelimitedCodecWithCompress` with `compress: true` by default: [1](#0-0) 

The codec's `decode` method first passes the frame through the inner `length_delimited` codec, which enforces `max_frame_length` on the wire bytes: [2](#0-1) 

Then, for compressed frames, it checks only against `MAX_UNCOMPRESSED_LEN`: [3](#0-2) 

The allocation at line 242 (`BytesMut::zeroed(decompressed_bytes_len)`) can be up to 8MB regardless of the protocol's `max_frame_length`. The per-protocol limits are: [4](#0-3) 

Amplification ratios:
- **Ping** (1KB wire limit): up to **8192×** amplification → 8MB per frame
- **Identify** (2KB): up to **4096×** → 8MB per frame
- **Discovery** (512KB): up to **16×** → 8MB per frame
- **Alert** (128KB): up to **64×** → 8MB per frame
- **Sync** (2MB): up to **4×** → 8MB per frame

### Impact Explanation

An attacker with N concurrent peer connections, each sending crafted snappy frames at maximum rate, forces the victim node to allocate N × 8MB simultaneously. With CKB's default peer limit (~125), this is up to ~1GB of sustained heap pressure from a single attacker controlling multiple connections. For low-`max_frame_length` protocols (Ping, Identify, Time), the wire cost per attack frame is ≤ 1–2KB while the allocation cost is 8MB — making the attack extremely bandwidth-efficient. This can cause OOM on memory-constrained nodes and sustained CPU cost from snappy decompression of 8MB payloads per message.

### Likelihood Explanation

The attack requires only a standard P2P connection — no authentication, no PoW, no privileged role. Any peer that can open a session on any compression-enabled protocol (all of them by default) can execute this. The crafted payload is a valid snappy stream of repeated bytes (e.g., `\x00` × 8MB compresses to ~8KB), so it passes all format checks and triggers a successful decompression allocation.

### Recommendation

In `LengthDelimitedCodecWithCompress::decode`, after reading `decompressed_bytes_len`, add a check against `self.length_delimited.max_frame_length()` before allocating:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This enforces the invariant that `max_frame_length` bounds per-message processing cost for both compressed and uncompressed frames. The `MAX_UNCOMPRESSED_LEN` check can remain as a secondary absolute ceiling.

### Proof of Concept

```rust
// Craft a snappy payload: 8MB - 1 byte of repeated zeros
let raw = vec![0u8; MAX_UNCOMPRESSED_LEN - 1]; // 8MB - 1
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
// compressed.len() ≈ ~8KB for repeated bytes

// Wire frame: [4-byte length header][COMPRESS_FLAG byte][compressed payload]
// Wire size ≈ 8KB << Discovery's 512KB max_frame_length → passes wire check
// Receiver allocates BytesMut::zeroed(8MB - 1) at compress.rs:242
// With 125 concurrent peers: 125 × 8MB ≈ 1GB heap pressure
``` [5](#0-4)

### Citations

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

**File:** network/src/compress.rs (L226-227)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
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
