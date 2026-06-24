Audit Report

## Title
Snappy Decompression Bomb: `max_frame_length` Not Enforced on Decompressed Size, Allowing Up to 8MB Allocation Per Wire Frame — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the compressed wire frame via the inner `length_delimited` codec. After passing that check, the decompressed size is bounded solely by the hardcoded `MAX_UNCOMPRESSED_LEN = 8MB`. Any unprivileged remote peer can send a snappy-compressed frame whose wire size is within `max_frame_length` but whose decompressed size approaches 8MB, causing the receiver to allocate up to 8MB per message regardless of the protocol's intended budget.

## Finding Description

`MAX_UNCOMPRESSED_LEN` is hardcoded to 8MB: [1](#0-0) 

In `LengthDelimitedCodecWithCompress::decode`, the inner codec enforces `max_frame_length` on the wire bytes only: [2](#0-1) 

For compressed frames, the only post-wire-check bound is `MAX_UNCOMPRESSED_LEN`, not `max_frame_length`. `BytesMut::zeroed(decompressed_bytes_len)` allocates up to 8MB unconditionally, with no reference to the per-protocol `max_frame_length`: [3](#0-2) 

The `max_frame_length` field is stored in `self.length_delimited` and is already consulted in the `process` (encode) path at line 144, but is never consulted during decode for the decompressed size: [4](#0-3) 

All protocols are built with `compress: true` by default via both `new_with_support_protocol` and `new`: [5](#0-4) 

Per-protocol wire limits range from 1KB (Ping/Feeler/Time/DisconnectMessage) to 4MB (RelayV3): [6](#0-5) 

For protocols with larger wire limits (Discovery 512KB, Alert 128KB, Sync 2MB, RelayV3 4MB), an attacker crafts a snappy payload of repeated bytes that fits within `max_frame_length` on the wire but decompresses to the full 8MB ceiling. For example, a ~393KB compressed payload of repeated zeros fits within Discovery's 512KB wire limit and decompresses to ~8MB (>16× amplification). Even for small-limit protocols (Ping/Identify/Time/Feeler at 1–2KB), snappy's copy-command overhead still allows decompressed output to far exceed the protocol's intended budget.

## Impact Explanation

**High severity** — matches: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* and *"Vulnerabilities which could easily crash a CKB node."*

An attacker controlling N concurrent peer connections forces the victim to allocate N × 8MB of heap simultaneously. With CKB's default peer limit (~125), this is up to ~1GB of sustained heap pressure. The wire cost per attack frame is bounded by `max_frame_length` (as low as 512KB for Discovery), while the allocation cost is always up to 8MB — making the attack bandwidth-efficient. Sustained attack traffic can cause OOM on memory-constrained nodes and sustained CPU cost from snappy decompression of 8MB payloads per message.

## Likelihood Explanation

The attack requires only a standard P2P connection — no authentication, no PoW, no privileged role. Any peer that can open a session on any compression-enabled protocol (all of them by default) can execute this. The crafted payload is a valid snappy stream (e.g., repeated `\x00` bytes), passes all format checks, and triggers a successful decompression allocation. The attack is repeatable at the rate the protocol allows message delivery.

## Recommendation

In `LengthDelimitedCodecWithCompress::decode` (`network/src/compress.rs`), after reading `decompressed_bytes_len` and before allocating, add a check against `self.length_delimited.max_frame_length()`:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This enforces the invariant that `max_frame_length` bounds per-message processing cost for both compressed and uncompressed frames. The `MAX_UNCOMPRESSED_LEN` check can remain as a secondary absolute ceiling. The `max_frame_length()` accessor is already used in the `process` method at line 144, confirming it is accessible on `self.length_delimited`.

## Proof of Concept

```rust
// Craft a snappy payload: repeated zeros up to MAX_UNCOMPRESSED_LEN - 1
let raw = vec![0u8; MAX_UNCOMPRESSED_LEN - 1]; // ~8MB
let compressed = snap::raw::Encoder::new().compress_vec(&raw).unwrap();
// compressed.len() ≈ ~393KB for repeated bytes (fits within Discovery's 512KB limit)

// Wire frame: [4-byte length header][COMPRESS_FLAG byte][compressed payload]
// Wire size ≈ 393KB < Discovery's 512KB max_frame_length → passes wire check
// Receiver executes BytesMut::zeroed(8MB - 1) at compress.rs:242
// With 125 concurrent peers: 125 × 8MB ≈ 1GB heap pressure

// Unit test plan:
// 1. Construct a LengthDelimitedCodecWithCompress with max_frame_length = 512KB
// 2. Feed it a valid snappy-compressed frame of ~393KB wire size
// 3. Observe that decode() allocates ~8MB (assert buf.capacity() > max_frame_length)
// 4. Apply fix; observe decode() returns Err(InvalidData) instead
```

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
