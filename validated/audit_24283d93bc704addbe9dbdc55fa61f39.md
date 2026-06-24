Audit Report

## Title
Pre-Allocation of Up to 8MB Per Malicious Compressed Frame Before Decompression Validation — (`network/src/compress.rs`)

## Summary
In both `LengthDelimitedCodecWithCompress::decode` and `Message::decompress`, the node reads a snappy varint from the compressed payload header to obtain `decompressed_bytes_len`, checks it against `MAX_UNCOMPRESSED_LEN` (8MB), and immediately allocates a buffer of that size before validating the compressed body. A remote peer can craft a minimal frame (compress flag byte + varint claiming ~8MB + garbage body) that passes the frame-length check, triggers an ~8MB allocation, and then causes an `InvalidData` error. With 1024 simultaneous connections, this yields up to ~8GB of transient heap allocation, sufficient to crash or severely degrade a CKB node.

## Finding Description

**Root cause — `LengthDelimitedCodecWithCompress::decode`:**

`self.length_delimited.decode(src)?` at line 226 enforces `max_frame_length` on the *compressed* frame size only. A 10-byte frame passes every protocol's limit. [1](#0-0) 

`decompress_len(&data[1..])` at line 233 reads only the snappy varint prefix; it does not validate the rest of the stream. A 4-byte LEB128 varint encoding `8388607` (`\xff\xff\xff\x03`) is sufficient to pass. [2](#0-1) 

The size check `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` passes for any value ≤ 8MB. [3](#0-2) 

`BytesMut::zeroed(decompressed_bytes_len)` allocates up to 8MB unconditionally before any decompression attempt. [4](#0-3) 

`SnapDecoder::new().decompress(...)` then fails on the garbage body, returning `Err(InvalidData)` — but the allocation already occurred and is only freed after the error propagates. [5](#0-4) 

The identical pattern exists in `Message::decompress` at line 81 (`vec![0; decompressed_bytes_len]`). [6](#0-5) 

**Why existing guards are insufficient:**
- `max_frame_length` guards the compressed wire size, not the claimed uncompressed size. A 10-byte compressed frame is valid under every protocol's limit. [7](#0-6) 
- `MAX_UNCOMPRESSED_LEN = 1 << 23` (8MB) is the upper bound of the allocation, not a prevention. [8](#0-7) 
- The connection limit of 1024 multiplies the per-connection allocation to ~8GB peak. [9](#0-8) 

## Impact Explanation
Each malicious frame (~10 bytes on the wire) causes an ~8MB heap allocation before any error is detected. With 1024 simultaneous connections (the configured maximum), the node can be forced to allocate up to ~8GB transiently, causing OOM, node crash, or severe memory pressure that degrades sync and relay performance. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation
- The P2P port is open to all peers by default; no PoW, stake, or authentication is required.
- The snappy varint format is trivially craftable: encode `8388607` as `\xff\xff\xff\x03` (4 bytes LEB128), prepend the compress flag byte `\x80`, append garbage.
- A single attacker machine can open 1024 TCP connections and send one malicious frame per connection.
- The attack is repeatable: after disconnect, the attacker reconnects and repeats indefinitely.
- The noise handshake must be completed before sending protocol messages, but this is a low bar for any unprivileged peer.

## Recommendation
Validate snappy stream integrity **before** allocating the output buffer:

1. **Ratio guard (minimal change):** Reject frames where `decompressed_bytes_len` exceeds the compressed input size by more than a safe expansion ratio (snappy's maximum is ~5×). If `decompressed_bytes_len > compressed_input.len() * 5`, return `InvalidData` without allocating.
2. **Streaming decoder:** Replace `decompress_len` + pre-allocation with `snap::read::FrameDecoder` (streaming), which never pre-allocates based on an unvalidated header claim.
3. **Tighter cap:** Lower `MAX_UNCOMPRESSED_LEN` to match the protocol's own `max_frame_length` (e.g., 4MB for RelayV3), so the pre-allocation is bounded by the same limit as the compressed input.

## Proof of Concept

```python
import socket, struct

# Snappy varint encoding of 8388607 (~8MB): 0xFF 0xFF 0xFF 0x03
snappy_varint = b'\xff\xff\xff\x03'
garbage_body  = b'\xde\xad\xbe\xef' * 4  # invalid snappy body

# compress flag byte (0x80) + snappy payload
payload = b'\x80' + snappy_varint + garbage_body  # ~13 bytes total

# 4-byte big-endian length prefix (LengthDelimitedCodec default)
frame = struct.pack('>I', len(payload)) + payload

# Repeat with 1024 connections after noise handshake on port 8115
# Each connection triggers BytesMut::zeroed(~8MB) at compress.rs:242
# before Err(InvalidData) is returned — ~8GB peak allocation with 1024 conns
for _ in range(1024):
    s = socket.create_connection(('target-node', 8115))
    # complete noise handshake here
    s.sendall(frame)
```

The allocation at `compress.rs:242` (`BytesMut::zeroed(decompressed_bytes_len)`) occurs before the error path at lines 245–248, confirming the pre-validation allocation. [10](#0-9)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L72-88)
```rust
            match decompress_len(&self.inner[1..]) {
                Ok(decompressed_bytes_len) => {
                    if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                        debug!(
                            "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                            MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                        );
                        Err(io::ErrorKind::InvalidData.into())
                    } else {
                        let mut buf = vec![0; decompressed_bytes_len];
                        match SnapDecoder::new().decompress(&self.inner[1..], &mut buf) {
                            Ok(_) => Ok(buf.into()),
                            Err(e) => {
                                debug!("snappy decompress error: {:?}", e);
                                Err(io::ErrorKind::InvalidData.into())
                            }
                        }
```

**File:** network/src/compress.rs (L226-227)
```rust
        match self.length_delimited.decode(src)? {
            Some(mut data) => {
```

**File:** network/src/compress.rs (L233-234)
```rust
                    match decompress_len(&data[1..]) {
                        Ok(decompressed_bytes_len) => {
```

**File:** network/src/compress.rs (L235-240)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
```

**File:** network/src/compress.rs (L242-249)
```rust
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
                            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                                Ok(_) => Ok(Some(buf)),
                                Err(e) => {
                                    debug!("snappy decompress error: {:?}", e);
                                    Err(io::ErrorKind::InvalidData.into())
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

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```
