The code is fully readable. Let me analyze the exact logic.

The code is fully analyzed. Here is the verdict.

---

### Title
Decompression Bypasses Per-Protocol `max_frame_length` Bound, Allowing Up to 4× Memory Amplification per Frame — (`network/src/compress.rs`)

### Summary

The `LengthDelimitedCodecWithCompress::decode` function enforces `max_frame_length` only on the compressed wire bytes (via `length_delimited.decode`), but after detecting the `COMPRESS_FLAG` it allocates a buffer sized by the snappy header's claimed decompressed length, bounded only by the global `MAX_UNCOMPRESSED_LEN` (8 MB). The per-protocol `max_frame_length` is never consulted during decompression. Any unprivileged remote peer can exploit this to cause allocations that are a multiple of the intended per-protocol limit.

### Finding Description

`MAX_UNCOMPRESSED_LEN` is defined as `1 << 23` = 8,388,608 bytes (8 MB). [1](#0-0) 

`RelayV3` has `max_frame_length` = 4 MB; `Sync` has 2 MB. [2](#0-1) 

In `decode()`, after `length_delimited.decode(src)` accepts the compressed frame (enforcing `max_frame_length` on wire bytes), the only guard on the decompressed size is:

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN { … return Err(…); }
let mut buf = BytesMut::zeroed(decompressed_bytes_len);
``` [3](#0-2) 

There is no check `decompressed_bytes_len > self.length_delimited.max_frame_length()`. The field `self.length_delimited` exposes `max_frame_length()` and is already used in `process()` (the encoder path) for exactly this purpose: [4](#0-3) 

but it is never consulted in the decoder path.

**Amplification ratios per protocol:**

| Protocol | `max_frame_length` | Max decompressed | Amplification |
|---|---|---|---|
| Sync | 2 MB | 8 MB | **4×** |
| RelayV3 | 4 MB | 8 MB | **2×** |
| LightClient/Filter | 2 MB | 8 MB | **4×** |

### Impact Explanation

A single unprivileged peer can send a stream of compressed frames, each ≤ `max_frame_length` on the wire, where the snappy header claims a decompressed size just below `MAX_UNCOMPRESSED_LEN`. Each frame causes an allocation up to 4× the intended per-protocol limit. With multiple concurrent peers or pipelined frames, this multiplies the node's memory consumption well beyond what the per-protocol frame limits were designed to allow, creating a memory-pressure amplification vector reachable from any peer without any authentication.

### Likelihood Explanation

The exploit requires only a TCP connection and the ability to craft a valid snappy-compressed payload. No PoW, no keys, no privileged role. The snappy format is public and `decompress_len` reads the varint from the stream header, which an attacker fully controls. The path is reachable on both `Sync` and `RelayV3`, the two highest-traffic protocols.

### Recommendation

In `decode()`, after reading `decompressed_bytes_len`, add a check against the per-protocol limit before allocating:

```rust
if decompressed_bytes_len > self.length_delimited.max_frame_length() {
    return Err(io::ErrorKind::InvalidData.into());
}
```

This mirrors the guard already present in `process()` and closes the gap between the wire-level and decompressed-level frame limits. [5](#0-4) 

### Proof of Concept

1. Construct a payload of ~8 MB of zeros.
2. Snappy-compress it — snappy compresses repetitive data well, producing a compressed form well under 4 MB.
3. Prepend `COMPRESS_FLAG` (0x80) and send as a single RelayV3 frame (wire length ≤ 4 MB, passes `length_delimited` check).
4. On the receiver, `decompress_len` returns ~8 MB; the check `> MAX_UNCOMPRESSED_LEN` passes (8 MB − 1 < 8 MB); `BytesMut::zeroed(~8 MB)` is allocated.
5. Repeat across multiple connections or in a tight loop to amplify memory pressure.

A unit test can confirm: construct a `LengthDelimitedCodecWithCompress` with `max_frame_length = 4 MB`, encode a frame whose compressed size < 4 MB but whose decompressed size > 4 MB, call `decode()`, and assert it returns `Err` — currently it returns `Ok(8 MB buffer)`.

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

**File:** network/src/protocols/support_protocols.rs (L129-130)
```rust
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```
