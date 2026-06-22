### Title
Attacker-Controlled Snappy Varint Triggers Up-to-8MB Pre-Allocation Per Frame Before Decompression Validation — (`network/src/compress.rs`)

---

### Summary

`LengthDelimitedCodecWithCompress::decode` allocates a `BytesMut` buffer sized from the attacker-supplied snappy uncompressed-length varint **before** attempting decompression. The guard rejects values **strictly greater than** `MAX_UNCOMPRESSED_LEN` (8 MB), so a value of exactly 8,388,608 passes and causes an 8 MB zero-allocation per frame. Because the wire frame can be as small as ~6 bytes, the amplification ratio is ~1,400,000×. With up to 117 concurrent inbound sessions (default `max_peers=125`, `max_outbound_peers=8`), a single attacker can force ~936 MB of simultaneous heap allocation with negligible bandwidth.

---

### Finding Description

In `LengthDelimitedCodecWithCompress::decode`:

```
// network/src/compress.rs, lines 232–248
if (data[0] & COMPRESS_FLAG) != 0 {
    match decompress_len(&data[1..]) {
        Ok(decompressed_bytes_len) => {
            if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // strict >
                return Err(io::ErrorKind::InvalidData.into());
            }
            let mut buf = BytesMut::zeroed(decompressed_bytes_len); // ← allocation
            match SnapDecoder::new().decompress(&data[1..], &mut buf) {
                Ok(_) => Ok(Some(buf)),
                Err(e) => {
                    debug!("snappy decompress error: {:?}", e);
                    Err(io::ErrorKind::InvalidData.into())         // ← session drop
                }
            }
        }
``` [1](#0-0) 

The snappy format encodes the uncompressed length as a varint in the first 1–5 bytes of the compressed payload. An attacker can craft a wire frame of:

```
[4-byte length prefix = 6][0x80 = COMPRESS_FLAG][varint encoding 8388608 = 0x80 0x80 0x80 0x04]
```

Total wire bytes: ~10. `decompress_len` reads the varint and returns 8,388,608. The check `8388608 > 8388608` is **false**, so execution falls through to `BytesMut::zeroed(8388608)` — an 8 MB allocation — before `decompress` is even called. `decompress` then fails on the garbage payload, the error is returned, and the session is dropped. The allocation is freed, but only after it was made.

The same pattern exists in `Message::decompress` (the `vec![0; decompressed_bytes_len]` path): [2](#0-1) 

The `LengthDelimitedCodec` enforces `max_frame_length` on **wire bytes** only: [3](#0-2) 

For `RelayV3` the wire limit is 4 MB, for `Sync`/`LightClient`/`Filter` it is 2 MB — but none of these bounds the claimed decompressed size, which is read from the attacker-controlled varint. [4](#0-3) 

---

### Impact Explanation

Default config: `max_peers = 125`, `max_outbound_peers = 8`, so `max_inbound_peers = 117`. [5](#0-4) 

With 117 concurrent inbound sessions each sending one crafted frame simultaneously:

- **Per-frame allocation**: 8 MB
- **Simultaneous peak**: 117 × 8 MB ≈ **936 MB**
- **Wire cost per frame**: ~10 bytes
- **Amplification**: ~800,000×

Each session is dropped after the failed decompression, but the attacker can immediately reconnect. The peer registry's eviction logic does not ban peers for sending malformed compressed frames — it only disconnects them: [6](#0-5) 

This means the attacker can cycle connections at high frequency, sustaining memory pressure, stalling the async I/O loop, and potentially triggering OOM on nodes with limited RAM.

---

### Likelihood Explanation

- No authentication or PoW required — any TCP peer can open an inbound session.
- The crafted frame is trivially constructable: set `data[0] = 0x80`, encode `8388608` as a snappy varint in `data[1..]`.
- The attack is repeatable at the rate of TCP connection setup, which is fast.
- No existing guard checks that `decompressed_bytes_len` is proportional to actual wire payload size.

---

### Recommendation

Replace the pre-allocation with a size-proportional bound before allocating. Snappy's maximum compression ratio is approximately 1:8, so a safe pre-check would be:

```rust
let max_plausible = (data.len() - 1).saturating_mul(8);
if decompressed_bytes_len > max_plausible.min(MAX_UNCOMPRESSED_LEN) {
    return Err(io::ErrorKind::InvalidData.into());
}
```

Alternatively, use a fixed-size scratch buffer and let the snappy decoder fail if output exceeds it, or use a streaming decompressor that avoids upfront allocation entirely. The same fix must be applied to `Message::decompress`. [7](#0-6) 

---

### Proof of Concept

```rust
// Craft a minimal wire frame that claims 8MB decompressed size
// Snappy varint for 8388608 = 0x80 0x80 0x80 0x04
let mut frame = BytesMut::new();
let payload: &[u8] = &[0x80u8, 0x80, 0x80, 0x80, 0x04, 0xFF, 0xFF]; // COMPRESS_FLAG + varint(8MB) + garbage
// 4-byte big-endian length prefix
frame.put_u32(payload.len() as u32);
frame.put_slice(payload);

let mut codec = LengthDelimitedCodecWithCompress::new(
    true,
    length_delimited::Builder::new().max_frame_length(4 * 1024 * 1024).new_codec(),
    101.into(), // RelayV3
);

// This call allocates BytesMut::zeroed(8388608) before returning Err
let result = codec.decode(&mut frame);
assert!(result.is_err()); // session dropped, but 8MB was allocated

// Repeat N times concurrently across max_inbound sessions → N×8MB peak RSS
```

### Citations

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

**File:** util/app-config/src/configs/network.rs (L355-357)
```rust
    pub fn max_inbound_peers(&self) -> u32 {
        self.max_peers.saturating_sub(self.max_outbound_peers)
    }
```

**File:** network/src/peer_registry.rs (L86-121)
```rust
    pub(crate) fn accept_peer(
        &mut self,
        remote_addr: Multiaddr,
        session_id: SessionId,
        raw_session_type: RawSessionType,
        peer_store: &mut PeerStore,
    ) -> Result<Option<Peer>, Error> {
        if self.peers.contains_key(&session_id) {
            return Err(PeerError::SessionExists(session_id).into());
        }
        let peer_id = extract_peer_id(&remote_addr).expect("opened session should have peer id");
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }

        let is_whitelist = self.whitelist_peers.contains(&peer_id);
        let mut evicted_peer: Option<Peer> = None;

        let mut session_type: SessionType = raw_session_type.into();
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }

            let connection_status = self.connection_status();
            // check peers connection limitation
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```
