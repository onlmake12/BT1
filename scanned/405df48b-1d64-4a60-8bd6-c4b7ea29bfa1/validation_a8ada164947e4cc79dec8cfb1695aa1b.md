The code is fully readable. Let me trace the exact decode path carefully.

### Title
Decompressed Frame Size Bypasses Per-Protocol `max_frame_length` Limit — (`network/src/compress.rs`)

---

### Summary

`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` only on the **compressed wire frame**, but returns the **decompressed** `BytesMut` to the protocol handler with no check against `max_frame_length`. The only upper bound on the returned buffer is `MAX_UNCOMPRESSED_LEN` (8 MB). Any unprivileged remote peer can send a compressed P2P frame within the wire limit that decompresses to up to 8 MB, causing the node to allocate and deliver an oversized buffer to every protocol handler — including those with a 1 KB wire limit such as Ping, Feeler, DisconnectMessage, and Time.

---

### Finding Description

In `LengthDelimitedCodecWithCompress::decode`:

**Step 1** — wire-level enforcement (correct): [1](#0-0) 
`self.length_delimited.decode(src)?` rejects any frame whose compressed wire size exceeds `max_frame_length`. For Ping this is 1024 bytes.

**Step 2** — decompressed-size check (incomplete): [2](#0-1) 
After passing the wire check, the decoder reads the snappy-declared decompressed length, rejects only if it exceeds `MAX_UNCOMPRESSED_LEN` (8 MB), then allocates `BytesMut::zeroed(decompressed_bytes_len)` and returns it. There is **no check** that `decompressed_bytes_len <= self.length_delimited.max_frame_length()`.

The per-protocol limits are: [3](#0-2) 

The encoder's `process()` does enforce `max_frame_length` before writing, but that guard is on the **outbound** path only: [4](#0-3) 

The codec is wired into every protocol via `CKBProtocol::build()`: [5](#0-4) 

---

### Impact Explanation

| Protocol | `max_frame_length` | Max decompressed delivered | Amplification |
|---|---|---|---|
| Ping | 1 KB | 8 MB | ×8192 |
| Feeler | 1 KB | 8 MB | ×8192 |
| DisconnectMessage | 1 KB | 8 MB | ×8192 |
| Time | 1 KB | 8 MB | ×8192 |
| Identify | 2 KB | 8 MB | ×4096 |
| Alert | 128 KB | 8 MB | ×64 |
| Discovery / HolePunching | 512 KB | 8 MB | ×16 |
| Sync / LightClient / Filter | 2 MB | 8 MB | ×4 |
| RelayV3 | 4 MB | 8 MB | ×2 |

Each inbound compressed frame causes an 8 MB heap allocation before the protocol handler even inspects the message. With many peers (or a single peer sending frames in a tight loop), this creates amplified memory pressure proportional to the number of concurrent connections and message rate. Protocol handlers also receive messages far exceeding their intended size bounds, which can trigger unexpected behavior in handlers that assume `max_frame_length` is respected.

---

### Likelihood Explanation

The attacker needs only a standard P2P connection — no authentication, no PoW, no privileged role. Snappy frames with a large declared decompressed length but small compressed body are trivially constructable (snappy's framing format encodes the uncompressed length in the stream header). The path is reachable on mainnet from any peer.

---

### Recommendation

In `LengthDelimitedCodecWithCompress::decode`, after computing `decompressed_bytes_len` and before allocating, add:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This mirrors the existing `MAX_UNCOMPRESSED_LEN` guard at line 235 and closes the gap between the wire-level and application-level size invariants. [6](#0-5) 

---

### Proof of Concept

```
1. Connect to a CKB node as an unprivileged peer on the Ping protocol
   (max_frame_length = 1024).

2. Construct a snappy-compressed payload:
   - Raw input: 8 MB of zeros (compresses to ~8 KB with snappy).
   - But for maximum effect, use a snappy stream that declares
     decompressed_len = 8_388_608 (MAX_UNCOMPRESSED_LEN) in its header
     while keeping the compressed body ≤ 1023 bytes
     (achievable with highly repetitive data, e.g., all-zero bytes).

3. Prepend the CKB compress flag byte (0x80) and a 4-byte length prefix
   encoding the total compressed frame size (≤ 1024).

4. Send over the P2P connection on the Ping protocol stream.

5. Observe: LengthDelimitedCodecWithCompress::decode passes the wire-size
   check (≤ 1024), reads decompressed_bytes_len = 8_388_608, passes the
   MAX_UNCOMPRESSED_LEN check, allocates BytesMut::zeroed(8_388_608),
   and returns Ok(Some(buf)) where buf.len() == 8_388_608.

6. The Ping handler receives an 8 MB buffer despite max_frame_length = 1024.
   Repeat across N connections to amplify heap pressure by N × 8 MB per
   message round.
```

### Citations

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

**File:** network/src/compress.rs (L226-226)
```rust
        match self.length_delimited.decode(src)? {
```

**File:** network/src/compress.rs (L233-244)
```rust
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
