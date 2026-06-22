The code is fully readable. Let me analyze the exact execution path and all relevant guards.

**Key facts from the code:**

**1. The allocation in `decode` (line 242):** [1](#0-0) 

The flow is:
- `decompress_len(&data[1..])` reads the snappy varint-encoded uncompressed length from the **attacker-controlled** stream header
- If `decompressed_bytes_len <= MAX_UNCOMPRESSED_LEN` (8MB), `BytesMut::zeroed(decompressed_bytes_len)` is allocated
- Only then is actual decompression attempted

An attacker can craft a byte sequence where the snappy varint header claims 7.9MB but the payload is garbage (a few bytes total). `decompress_len` trusts the varint; the allocation happens; decompression fails; connection is dropped. The allocation and free both occur, but the peak RSS spike is real.

**2. The `max_frame_length` check happens on the compressed wire frame, not the claimed decompressed size:** [2](#0-1) 

A crafted frame of ~11 bytes (COMPRESS_FLAG + snappy varint for 7.9MB + garbage) passes the Sync 2MB / RelayV3 4MB frame limit trivially.

**3. No codec-layer ban or rate limit:** [3](#0-2) 

When `decode` returns `Err`, tentacle closes the connection. The ban mechanism (`ban_peer`) lives at the application layer and is never reached for codec-level errors. The attacker can reconnect immediately.

**4. Max inbound peers = 117 (default: `max_peers=125`, `max_outbound_peers=8`):** [4](#0-3) 

With 117 simultaneous connections each sending one crafted frame: 117 × 7.9MB ≈ **924MB peak simultaneous allocation**. After disconnect, the attacker reconnects and repeats.

---

### Title
Snappy Varint Header Allows Attacker-Controlled 8MB Heap Allocation Before Decompression Verification — (`network/src/compress.rs`)

### Summary
`LengthDelimitedCodecWithCompress::decode` allocates `BytesMut::zeroed(decompressed_bytes_len)` (up to 8MB) based solely on the attacker-controlled snappy varint header, before verifying that the compressed payload is valid. An unprivileged remote peer can trigger this allocation with an ~11-byte frame.

### Finding Description
In `network/src/compress.rs` line 242, the decoder calls `decompress_len(&data[1..])` which reads the uncompressed length from the snappy stream's varint header — a value entirely under attacker control. If the value is ≤ `MAX_UNCOMPRESSED_LEN` (8MB, line 13), `BytesMut::zeroed(decompressed_bytes_len)` is allocated unconditionally. The actual decompression at line 243 may then fail (returning `Err` and closing the connection), but the allocation has already occurred.

A minimal attack frame: `[0x80, <varint for 7,864,320>, <garbage bytes>]`. The `length_delimited` codec's `max_frame_length` guard (2MB for Sync, 4MB for RelayV3) operates on the compressed wire size and is trivially satisfied by this tiny frame. [5](#0-4) 

### Impact Explanation
- **Per-connection**: one ~11-byte frame → 7.9MB allocation → decompression error → disconnect → 7.9MB freed. Peak RSS spike per connection.
- **Simultaneous**: with 117 default max inbound connections all sending simultaneously: ~924MB peak RSS.
- **Sustained**: no codec-layer ban is applied on `decode` error; the attacker reconnects immediately and repeats, causing continuous allocator churn and RSS spikes.
- **Effect**: allocator stalls, increased GC pressure, degraded P2P message processing throughput across all protocols (Sync, RelayV3, LightClient, Filter).

### Likelihood Explanation
The attack requires only a TCP connection to port 8115 (default P2P port, publicly exposed). No authentication, no PoW, no key material. The crafted frame is trivial to construct. The default `max_peers = 125` provides no meaningful barrier. [6](#0-5) 

### Recommendation
Move the allocation **after** a lightweight structural validation of the compressed payload, or replace `BytesMut::zeroed(decompressed_bytes_len)` with a lazy/streaming decompressor that does not pre-allocate the full claimed size. At minimum, add a codec-layer peer ban (or exponential backoff) when `decode` returns `Err` for a compressed frame, to prevent immediate reconnect-and-repeat.

### Proof of Concept
```python
import socket, struct

# Snappy varint encoding of 7,864,320 (7.9 MB)
# 7864320 = 0x780000
varint = bytes([0x80 | (7864320 & 0x7F),
                0x80 | ((7864320 >> 7) & 0x7F),
                0x80 | ((7864320 >> 14) & 0x7F),
                (7864320 >> 21) & 0x7F])

# Frame: COMPRESS_FLAG=0x80, then snappy stream = varint + garbage
payload = bytes([0x80]) + varint + b'\xff\xff\xff'  # ~8 bytes total

# LengthDelimited framing: 4-byte big-endian length prefix
frame = struct.pack('>I', len(payload)) + payload

# After tentacle handshake, send frame on Sync/Relay protocol channel
# Each send triggers BytesMut::zeroed(7,864,320) on the target node
# Connection is then dropped; reconnect and repeat with 117 connections
```

### Citations

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

**File:** network/src/protocols/mod.rs (L280-298)
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
            .support_versions(supported_versions)
            .service_handle(move || {
                ProtocolHandle::Callback(Box::new(CKBHandler {
                    proto_id: self.id,
                    network_state: Arc::clone(&self.network_state),
                    handler: self.handler,
                }))
            })
            .build()
    }
```

**File:** network/src/peer_registry.rs (L113-122)
```rust
            let connection_status = self.connection_status();
            // check peers connection limitation
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
```
