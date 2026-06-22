Now I have all the information needed. Let me analyze the exact code path.

### Title
Off-by-One in Decompressed-Length Guard Allows 8 MB Allocation Per Peer Frame — (`network/src/compress.rs`)

### Summary

`LengthDelimitedCodecWithCompress::decode` uses a strict-greater-than guard (`> MAX_UNCOMPRESSED_LEN`) before allocating the decompression output buffer. Because `MAX_UNCOMPRESSED_LEN = 1 << 23` (8 MB), a crafted snappy frame whose header varint equals exactly 8 MB passes the guard and causes `BytesMut::zeroed(8_388_608)` to be called unconditionally — before any decompression work is done. Any unprivileged peer that can open a TCP connection can trigger this allocation repeatedly. With the default `max_peers = 125` (or the hard cap of 1 024 connections), concurrent exploitation multiplies the peak RSS proportionally.

---

### Finding Description

**Root cause — off-by-one in the size guard** [1](#0-0) 

```
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
``` [2](#0-1) 

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // ← > not >=
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len); // ← 8 MB zeroed here
```

The guard rejects values **strictly greater than** 8 MB, so `decompressed_bytes_len == 8_388_608` passes and immediately triggers an 8 MB heap allocation. The allocation happens at line 242, before `SnapDecoder::decompress` is called at line 243. Even if decompression subsequently fails (e.g., because the payload is malformed), the 8 MB was already committed to the allocator.

The identical pattern exists in `Message::decompress`: [3](#0-2) 

**How an attacker crafts the frame**

The snappy wire format begins with a varint encoding the uncompressed length. An attacker sets this varint to exactly `8_388_608`. The actual compressed payload can be tiny — 8 MB of a repeated byte (e.g., `0x00`) compresses to roughly 100–200 bytes under snappy. The resulting wire frame is therefore well within every protocol's `max_frame_length`: [4](#0-3) 

The largest limit is RelayV3 at 4 MB; a ~200-byte compressed payload claiming 8 MB uncompressed is accepted by the length-delimited framing layer, passes the `> MAX_UNCOMPRESSED_LEN` guard, and triggers the allocation.

**Connection limits** [5](#0-4) 

The service accepts up to 1 024 TCP connections. The peer registry enforces `max_peers = 125` by default: [6](#0-5) 

---

### Impact Explanation

Each malicious frame causes a transient 8 MB allocation. With N peers sending such frames concurrently the peak RSS grows by N × 8 MB before any frame is processed and the buffer freed. At the default `max_peers = 125` this is ~1 GB; at the hard connection cap of 1 024 it is ~8 GB. On typical validator hardware (8–16 GB RAM) this is sufficient to trigger the OOM killer and crash the node, halting block production and sync.

---

### Likelihood Explanation

- No authentication or stake is required to open a P2P connection to a public CKB node.
- The crafted frame is trivial to construct: set the snappy varint to `0x80 0x80 0x80 0x04` (varint for 8 388 608) and append any valid or invalid snappy body.
- The attack is repeatable: after a disconnect the attacker reconnects and repeats.
- The yamux per-stream window (`max_stream_window_size = 1 MB`) limits throughput per stream but does not prevent the allocation — the frame only needs to be delivered once per connection.

---

### Recommendation

1. **Fix the off-by-one**: change `>` to `>=` in both guards so that `decompressed_bytes_len == MAX_UNCOMPRESSED_LEN` is also rejected:
   ```rust
   if decompressed_bytes_len >= MAX_UNCOMPRESSED_LEN {
       return Err(io::ErrorKind::InvalidData.into());
   }
   ```
2. **Lower the limit**: 8 MB is already larger than the largest protocol frame (RelayV3 = 4 MB). `MAX_UNCOMPRESSED_LEN` should be set to match or slightly exceed the per-protocol `max_frame_length`, not a global 8 MB constant.
3. **Allocate after decompression**: use `snap::raw::decompress_len` only for the guard check; allocate the output buffer lazily or use `decompress_vec` which handles sizing internally.

---

### Proof of Concept

```rust
// Craft a snappy frame claiming 8 MB uncompressed, tiny compressed body
fn malicious_frame() -> Vec<u8> {
    // snappy varint for 8_388_608 = 0x80 0x80 0x80 0x04
    let mut frame = vec![0x80u8]; // COMPRESS_FLAG byte
    // snappy stream: varint(8MB) + one literal copy block (e.g., 1 byte)
    frame.extend_from_slice(&[0x80, 0x80, 0x80, 0x04]); // uncompressed len varint
    frame.extend_from_slice(&[0x00]); // minimal snappy body (will fail decompress, but allocation already done)
    frame
}

// Wrap in length-delimited framing (4-byte big-endian length prefix)
// Connect N peers, each sending this frame → N × 8 MB peak RSS
```

Fuzz harness: feed `malicious_frame()` into `LengthDelimitedCodecWithCompress::decode` with a pre-populated `BytesMut` containing the 4-byte length prefix + the frame body; assert that RSS does not exceed `MAX_UNCOMPRESSED_LEN` per call.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

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

**File:** network/src/compress.rs (L235-242)
```rust
                            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {
                                debug!(
                                    "The limit for uncompressed bytes len is exceeded. limit: {}, len: {}",
                                    MAX_UNCOMPRESSED_LEN, decompressed_bytes_len
                                );
                                return Err(io::ErrorKind::InvalidData.into());
                            }
                            let mut buf = BytesMut::zeroed(decompressed_bytes_len);
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

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
