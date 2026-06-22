### Title
Snappy Decompression Attempted Before Any Rate-Limiting or Peer Banning, Enabling Repeated Memory-Allocation DoS - (File: network/src/compress.rs)

### Summary
CKB's P2P codec unconditionally allocates a buffer of up to 8 MB and attempts Snappy decompression for every incoming compressed frame before any protocol-level rate limiting or peer banning is applied. A crafted frame whose Snappy stream header claims a large uncompressed size but contains a corrupted payload causes the node to allocate and zero-fill up to 8 MB, attempt decompression, fail, and drop the connection — without banning the peer. The attacker can reconnect immediately and repeat, causing sustained memory pressure and CPU work at negligible cost.

### Finding Description

`LengthDelimitedCodecWithCompress::decode` in `network/src/compress.rs` is the tokio codec used for every CKB P2P protocol (sync, relay, light-client, discovery, etc.). When a frame arrives with the compress flag set, the codec:

1. Calls `decompress_len(&data[1..])` to read the Snappy stream header varint — this is a pure header read and succeeds even on a crafted payload.
2. Checks the claimed length against `MAX_UNCOMPRESSED_LEN` (8 MB). If within bounds, it proceeds.
3. Allocates `BytesMut::zeroed(decompressed_bytes_len)` — up to 8 MB of zeroed memory.
4. Calls `SnapDecoder::new().decompress(...)` on the (corrupted) payload.
5. On failure, returns `Err(io::ErrorKind::InvalidData)` — which closes the TCP connection. [1](#0-0) 

The error is returned from the codec layer, below the protocol handler layer. Protocol-level peer banning (`nc.ban_peer` / `network_state.ban_session`) is only reachable after a message is successfully decoded and dispatched to a handler. A codec-level `InvalidData` error simply tears down the session; the remote IP is not added to the ban list. [2](#0-1) 

Compression is enabled by default for every protocol built with `CKBProtocol::new_with_support_protocol`. [3](#0-2) 

The `MAX_UNCOMPRESSED_LEN` constant is 8 MB (`1 << 23`). [4](#0-3) 

### Impact Explanation

An attacker who can open a TCP connection to a CKB node's P2P port (default 8115, publicly reachable) can craft a length-delimited frame where:

- Byte 0 is `0x80` (compress flag).
- Bytes 1–N encode a Snappy varint claiming ~8 MB uncompressed size.
- The remaining bytes are garbage.

For each such frame the node allocates and zeroes up to 8 MB, runs the Snappy decompressor (which fails quickly on garbage), then drops the connection. Because no ban is issued, the attacker reconnects immediately. With many concurrent connections or a high reconnect rate, this causes repeated large allocations and CPU work on the victim node, degrading its ability to process legitimate peers, relay transactions, and sync blocks. The impact is a sustained resource-exhaustion DoS against any publicly reachable CKB full node.

### Likelihood Explanation

The P2P port is publicly reachable by design. No authentication, proof-of-work, or fee is required to open a connection. The attack requires only a TCP connection and a handful of crafted bytes per attempt. The absence of a ban on codec-level failures means the attacker is never penalized. This is straightforwardly exploitable by any unprivileged network peer.

### Recommendation

- **Short term:** On a codec-level decompression failure, record the remote peer's address and apply a temporary ban (consistent with `BAD_MESSAGE_BAN_TIME` used at the protocol layer) before closing the connection, so the attacker cannot immediately reconnect.
- **Short term:** Add a per-IP or per-session connection-rate limit at the network layer so that rapid reconnects are throttled regardless of the failure reason.
- **Long term:** Review all other codec-level error paths to ensure that malformed-frame events from a remote peer result in appropriate rate-limiting or banning, not just a silent connection drop.

### Proof of Concept

1. Open a raw TCP connection to a CKB node's P2P port.
2. Complete the tentacle/yamux handshake to open a sub-stream for any protocol (e.g., sync).
3. Send a length-delimited frame:
   - 4-byte big-endian length prefix (e.g., `0x00 0x00 0x00 0x0A` for 10 bytes of payload).
   - Payload byte 0: `0x80` (compress flag).
   - Payload bytes 1–9: Snappy varint encoding `0x7FFFFF` (≈8 MB), followed by garbage bytes.
4. The node reads `decompress_len` → ~8 MB, allocates `BytesMut::zeroed(8MB)`, calls `SnapDecoder::decompress`, fails, returns `InvalidData`, drops the connection.
5. Reconnect immediately (no ban applied) and repeat from step 3.
6. Sustain this loop from multiple source IPs or a single IP with rapid reconnects to exhaust the node's memory allocator and CPU. [1](#0-0) [5](#0-4)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L68-99)
```rust
    pub(crate) fn decompress(mut self) -> Result<Bytes, io::Error> {
        if self.inner.is_empty() {
            Err(io::ErrorKind::InvalidData.into())
        } else if self.compress_flag() {
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
                    }
                }
                Err(e) => {
                    debug!("snappy decompress_len error: {:?}", e);
                    Err(io::ErrorKind::InvalidData.into())
                }
            }
        } else {
            let _ = self.inner.split_to(1);
            Ok(self.inner.freeze())
        }
```

**File:** network/src/compress.rs (L232-255)
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
                        }
                        Err(e) => {
                            debug!("snappy decompress_len error: {:?}", e);
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

**File:** network/src/protocols/mod.rs (L277-298)
```rust
        MetaBuilder::default()
            .id(self.id)
            .name(move |_| protocol_name.clone())
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
