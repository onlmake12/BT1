Audit Report

## Title
Attacker-Controlled Snappy Uncompressed-Length Varint Triggers Up-to-8 MB Allocation Per P2P Frame Without Payload Plausibility Check — (`network/src/compress.rs`)

## Summary
`LengthDelimitedCodecWithCompress::decode()` reads the declared uncompressed size from the attacker-controlled snappy stream header and allocates up to `MAX_UNCOMPRESSED_LEN` (8 MB) of zeroed memory before attempting decompression. There is no check that the declared uncompressed size is plausible relative to the actual compressed payload size. An unauthenticated peer can send a ~5-byte frame that forces an ~8 MB allocation on the receiving node; with many concurrent connections this creates sustained memory pressure sufficient to crash the node.

## Finding Description
`LengthDelimitedCodecWithCompress` is constructed in `CKBProtocol::build()` with `compress: true` by default for all protocols built via `CKBProtocol::new_with_support_protocol()`. The decode path in `network/src/compress.rs` (lines 232–249) is:

1. `self.length_delimited.decode(src)` returns a framed `BytesMut` bounded by `max_frame_length` (e.g. 4 MB for RelayV3, 2 MB for Sync).
2. If `data[0] & COMPRESS_FLAG != 0`, `decompress_len(&data[1..])` reads the varint-encoded uncompressed length from the snappy stream header — a field entirely under attacker control requiring only ~4 bytes to encode ~8 MB.
3. The sole guard is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (8 MB). A value of `MAX_UNCOMPRESSED_LEN − 1` (8,388,607) passes.
4. `BytesMut::zeroed(decompressed_bytes_len)` allocates up to **8 MB** of zeroed memory.
5. `SnapDecoder::new().decompress(...)` is then called. If the payload is garbage or truncated, it returns an error and the buffer is freed — but the allocation already occurred.

There is no cross-check of the form `decompressed_bytes_len ≤ f(data[1..].len())`. The `max_frame_length` guard only bounds the compressed frame size on the wire, not the declared uncompressed size embedded within it.

A malicious frame consists of:
- 1 byte: `COMPRESS_FLAG` (`0x80`)
- 4 bytes: varint encoding `8,388,607`
- 0 bytes of valid snappy data

Total wire size: **5 bytes**. Allocation triggered: **~8 MB**. Amplification: **~1,600,000×**.

## Impact Explanation
An unauthenticated peer can open the Sync or RelayV3 protocol (or any other protocol using `CKBProtocol::new_with_support_protocol`) and repeatedly send 5-byte malicious frames. With N concurrent connections each triggering an 8 MB allocation simultaneously, the node's heap is placed under N × 8 MB of pressure. At the default peer limit (~125 peers), this is ~1 GB of simultaneous allocation pressure. Sustained across reconnections (the connection is closed after each decode error, but the attacker can reconnect immediately), this leads to OOM process termination or severe memory pressure degrading block/transaction processing and sync. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
- No authentication is required: any TCP peer completing the Tentacle handshake can open Sync or RelayV3.
- The malicious frame is trivially crafted (~5 bytes, no CKB internals knowledge needed).
- The codec layer has no per-message rate limiting or throttling before the allocation occurs.
- Multiple protocols are affected: Sync (2 MB frame limit), RelayV3 (4 MB frame limit), LightClient (2 MB), Filter (2 MB), Discovery (512 KB), HolePunching (512 KB), Alert (128 KB) — all built via `CKBProtocol::new_with_support_protocol` with `compress: true`.
- The attacker can reconnect immediately after each connection is closed by the decode error, sustaining the attack indefinitely.

## Recommendation
Before allocating the decompression buffer, validate that the declared uncompressed size is plausible relative to the actual compressed payload size. Snappy's maximum expansion ratio is bounded (raw snappy never expands data by more than ~1.5×), so a conservative guard is:

```rust
// In LengthDelimitedCodecWithCompress::decode, after the MAX_UNCOMPRESSED_LEN check:
const MAX_SNAPPY_EXPANSION: usize = 2;
if decompressed_bytes_len > data[1..].len().saturating_mul(MAX_SNAPPY_EXPANSION)
    && decompressed_bytes_len > COMPRESSION_SIZE_THRESHOLD
{
    return Err(io::ErrorKind::InvalidData.into());
}
```

Apply the same guard in `Message::decompress()`. Additionally, consider lowering `MAX_UNCOMPRESSED_LEN` to match the actual maximum uncompressed protocol message size per protocol (e.g. 4 MB for RelayV3 rather than a global 8 MB).

## Proof of Concept

```python
import socket, struct

def encode_varint(n):
    buf = []
    while n > 0x7F:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)
    return bytes(buf)

# COMPRESS_FLAG byte + varint(MAX_UNCOMPRESSED_LEN - 1)
frame_payload = b'\x80' + encode_varint(8388607)
# LengthDelimitedCodec 4-byte big-endian length prefix
frame = struct.pack('>I', len(frame_payload)) + frame_payload

# After Tentacle handshake on Sync (port 8115) or RelayV3:
# Each send triggers BytesMut::zeroed(8388607) on the target before
# SnapDecoder returns an error and the connection is closed.
# Reconnect and repeat to sustain allocation pressure.
conn = socket.create_connection(('TARGET_IP', 8115))
# ... complete Tentacle handshake ...
conn.sendall(frame)
# ~8 MB allocated and freed per iteration; with N concurrent connections = N * 8 MB peak
```

The minimal reproducible unit test: construct a `LengthDelimitedCodecWithCompress` with `enable_compress: true`, feed it a `BytesMut` containing the 4-byte length prefix followed by `0x80` + varint(8388607), and observe that `decode` allocates ~8 MB before returning `Err(InvalidData)`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L232-249)
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
