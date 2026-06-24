Audit Report

## Title
Decompression Bypasses Per-Protocol `max_frame_length` Bound, Allowing Up to 4× Memory Amplification per Frame — (`network/src/compress.rs`)

## Summary

`LengthDelimitedCodecWithCompress::decode` enforces `max_frame_length` on the compressed wire bytes via the inner `length_delimited.decode()`, but after detecting `COMPRESS_FLAG` it bounds the decompressed allocation only by the global `MAX_UNCOMPRESSED_LEN` (8 MB). The per-protocol `max_frame_length` is never consulted during decompression. Any unprivileged peer can send compressed frames whose wire size is within the per-protocol limit but whose decompressed size reaches 8 MB, causing allocations up to 4× the intended per-protocol ceiling.

## Finding Description

`MAX_UNCOMPRESSED_LEN` is `1 << 23` = 8,388,608 bytes. [1](#0-0) 

`CKBProtocol::build()` wires `LengthDelimitedCodecWithCompress` as the codec for all protocols, passing `max_frame_length` into the inner `LengthDelimitedCodec`: [2](#0-1) 

`SupportProtocols::Sync` sets `max_frame_length` = 2 MB; `RelayV3` sets it to 4 MB: [3](#0-2) 

In `decode()`, after `self.length_delimited.decode(src)` accepts the compressed frame (enforcing `max_frame_length` on wire bytes), the only guard on the decompressed size is the global constant:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN { … return Err(…); }
let mut buf = BytesMut::zeroed(decompressed_bytes_len);
``` [4](#0-3) 

There is no check `decompressed_bytes_len > self.length_delimited.max_frame_length()`. The encoder path (`process()`) already performs exactly this check on the outgoing wire size: [5](#0-4) 

but the symmetric guard is absent in the decoder path.

**Amplification ratios per protocol:**

| Protocol | `max_frame_length` | Max decompressed | Amplification |
|---|---|---|---|
| Sync | 2 MB | 8 MB | **4×** |
| RelayV3 | 4 MB | 8 MB | **2×** |
| LightClient/Filter | 2 MB | 8 MB | **4×** |

## Impact Explanation

A sustained stream of crafted compressed frames — each within the per-protocol wire limit but claiming ~8 MB decompressed — causes the receiving node to allocate up to 4× the intended per-frame budget per frame received. With multiple concurrent peers (the default peer limit is in the tens) pipelining such frames on Sync or RelayV3, the node's heap can be exhausted, crashing the process. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

Exploitation requires only a TCP connection and the ability to craft a valid snappy-compressed payload. No proof-of-work, no keys, no privileged role. The snappy format is public; `decompress_len` reads an attacker-controlled varint from the stream header. The path is reachable on both `Sync` and `RelayV3`, the two highest-traffic protocols, and `compress: true` is the default for all `CKBProtocol` instances created via `new_with_support_protocol`. [6](#0-5) 

## Recommendation

In `decode()`, after reading `decompressed_bytes_len`, add a check against the per-protocol limit before allocating:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This mirrors the guard already present in `process()` and closes the gap between the wire-level and decompressed-level frame limits. [7](#0-6) 

## Proof of Concept

1. Construct ~8 MB of zero bytes.
2. Snappy-compress them — snappy compresses repetitive data to a few KB, well under any `max_frame_length`.
3. Prepend `COMPRESS_FLAG` (0x80) and frame it with a 4-byte length prefix (wire size ≪ 4 MB for RelayV3, passes `length_delimited` check).
4. Send to a CKB node on the RelayV3 or Sync protocol stream.
5. On the receiver, `decompress_len` returns ~8 MB; the `> MAX_UNCOMPRESSED_LEN` check passes (8 MB − 1 < 8 MB); `BytesMut::zeroed(~8 MB)` is allocated.
6. Repeat across multiple connections or in a tight loop to exhaust node memory.

Unit test to confirm: construct a `LengthDelimitedCodecWithCompress` with `max_frame_length = 4 MB`, encode a payload whose compressed size < 4 MB but whose decompressed size > 4 MB, call `decode()`, and assert it returns `Err` — currently it returns `Ok` with an ~8 MB buffer. [8](#0-7)

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L142-148)
```rust
    fn process(&self, data: &[u8], flag: u8, dst: &mut BytesMut) -> Result<(), io::Error> {
        let len = data.len() + 1;
        if len > self.length_delimited.max_frame_length() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "data too large",
            ));
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

**File:** network/src/protocols/support_protocols.rs (L129-130)
```rust
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```

**File:** network/src/tests/compress.rs (L54-82)
```rust
#[test]
fn test_length_delimited_codec_with_compress() {
    let mut codec_with_compress =
        LengthDelimitedCodecWithCompress::new(true, LengthDelimitedCodec::new(), 1.into());
    let mut codec = LengthDelimitedCodec::new();

    let raw_data = Bytes::from(vec![1; COMPRESSION_SIZE_THRESHOLD + 1]);
    let mut buf = BytesMut::new();
    codec_with_compress
        .encode(raw_data.clone(), &mut buf)
        .unwrap();

    let cmp_data = compress(raw_data);
    let mut buf_cmp = {
        let mut buf = BytesMut::new();
        codec.encode(cmp_data, &mut buf).unwrap();
        buf
    };

    assert_eq!(buf_cmp, buf);

    let decoded = codec_with_compress.decode(&mut buf).unwrap().unwrap();

    let decoded_cmp = codec.decode(&mut buf_cmp).unwrap().unwrap();

    let decoded_cmp = decompress(decoded_cmp).unwrap();

    assert_eq!(decoded, decoded_cmp);
}
```
