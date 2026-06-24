Audit Report

## Title
Attacker-Controlled Snappy Varint Triggers Up-to-8MB Pre-Allocation Per Frame Before Decompression Validation — (`network/src/compress.rs`)

## Summary

`LengthDelimitedCodecWithCompress::decode` and `Message::decompress` allocate a buffer sized from the attacker-supplied snappy uncompressed-length varint before attempting decompression. A strict `>` comparison against `MAX_UNCOMPRESSED_LEN` (8,388,608) allows a value of exactly 8,388,608 to pass, causing an 8 MB heap allocation per frame. An attacker can send a minimal wire frame on any protocol and force this allocation, which is freed only after decompression fails. With up to 117 concurrent inbound sessions, this yields ~936 MB of simultaneous peak allocation at negligible bandwidth cost, sufficient to OOM-crash nodes with 1–2 GB RAM.

## Finding Description

**Root cause — strict `>` off-by-one:**

`MAX_UNCOMPRESSED_LEN = 1 << 23 = 8,388,608` is defined at `network/src/compress.rs` line 13. In `LengthDelimitedCodecWithCompress::decode` (lines 235–242):

```rust
if decompressed_bytes_len > MAX_UNCOMPRESSED_LEN {   // strict >
    return Err(io::ErrorKind::InvalidData.into());
}
let mut buf = BytesMut::zeroed(decompressed_bytes_len); // ← 8 MB allocation
```

`8388608 > 8388608` evaluates to `false`, so execution falls through to `BytesMut::zeroed(8388608)`. The identical flaw exists in `Message::decompress` at line 81 (`vec![0; decompressed_bytes_len]`).

**No proportionality guard:**

`self.length_delimited.decode(src)?` at line 226 enforces `max_frame_length` on wire bytes only. For Ping this is 1 KB; for RelayV3 it is 4 MB. None of these bounds constrain the claimed decompressed size read from the attacker-controlled varint. A 6-byte wire frame on any protocol can claim 8 MB decompressed.

**`enable_compress` not checked in decode:**

The `decode` method checks `(data[0] & COMPRESS_FLAG) != 0` to enter the decompression path, without consulting `self.enable_compress`. The vulnerability is present regardless of the compression configuration.

**Exploit flow:**

1. Attacker opens an inbound TCP session (no authentication required).
2. Sends wire frame: `[4-byte length prefix][0x80 = COMPRESS_FLAG][0x80 0x80 0x80 0x04 = varint(8388608)][garbage]`.
3. `decompress_len` returns 8,388,608; guard passes (`8388608 > 8388608` is false).
4. `BytesMut::zeroed(8388608)` allocates 8 MB.
5. `SnapDecoder::decompress` fails on garbage payload; `Err` is returned and the session is dropped.
6. The 8 MB allocation is freed — but only after it was made.
7. Attacker immediately reconnects and repeats.

**No ban on malformed frames:**

`peer_registry.rs` `accept_peer` checks `peer_store.is_addr_banned` only at connection time (line 109). Sending a malformed compressed frame causes a session disconnect, not a ban. The attacker can cycle connections at TCP setup rate indefinitely.

**`LengthDelimitedCodecWithCompress` is the active codec** for all CKB protocols via `CKBProtocol::build()` at `network/src/protocols/mod.rs` lines 280–288, wrapping every protocol including Ping (1 KB wire limit), making the amplification ratio up to ~8,000× on the smallest protocol.

## Impact Explanation

**High severity** — *"Vulnerabilities which could easily crash a CKB node."*

With default config (`max_peers=125`, `max_outbound_peers=8`), `max_inbound_peers = 117` (confirmed at `util/app-config/src/configs/network.rs` lines 355–357). With 117 concurrent inbound sessions each sending one crafted frame simultaneously, peak heap allocation reaches **117 × 8 MB ≈ 936 MB**. On nodes with 1–2 GB RAM (common for validators and light nodes), this is sufficient to trigger OOM and crash the node. The attack is repeatable at TCP connection rate, sustaining memory pressure and stalling the async I/O loop.

## Likelihood Explanation

- No authentication, PoW, or stake required — any TCP peer can open an inbound session.
- The crafted frame is trivially constructable: set `data[0] = 0x80`, encode `8388608` as snappy varint `0x80 0x80 0x80 0x04`.
- The attack is repeatable at the rate of TCP connection setup.
- No existing guard checks that `decompressed_bytes_len` is proportional to the actual wire payload size.
- The off-by-one (`>` instead of `>=`) means the maximum allowed value is also the maximum allocation size.

## Recommendation

1. **Fix the off-by-one**: change `>` to `>=` in both `LengthDelimitedCodecWithCompress::decode` (line 235) and `Message::decompress` (line 74) so that `MAX_UNCOMPRESSED_LEN` itself is rejected.
2. **Add a proportionality guard** before allocating. Snappy's maximum compression ratio is ~8×:
   ```rust
   let max_plausible = (data.len() - 1).saturating_mul(8);
   if decompressed_bytes_len > max_plausible.min(MAX_UNCOMPRESSED_LEN) {
       return Err(io::ErrorKind::InvalidData.into());
   }
   ```
3. Apply the same fix to `Message::decompress` (lines 74–81 of `compress.rs`).
4. Consider banning peers that send malformed compressed frames rather than only disconnecting them.

## Proof of Concept

```rust
// Craft a minimal wire frame claiming 8MB decompressed size
// Snappy varint for 8388608 = 0x80 0x80 0x80 0x04
use p2p::bytes::BytesMut;
use tokio_util::codec::{Decoder, length_delimited};
use network::compress::LengthDelimitedCodecWithCompress;

let mut frame = BytesMut::new();
// COMPRESS_FLAG (0x80) + varint(8388608) + garbage payload
let payload: &[u8] = &[0x80u8, 0x80, 0x80, 0x80, 0x04, 0xFF, 0xFF];
frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
frame.extend_from_slice(payload);

let mut codec = LengthDelimitedCodecWithCompress::new(
    true,
    length_delimited::Builder::new()
        .max_frame_length(4 * 1024 * 1024)
        .new_codec(),
    101.into(), // RelayV3
);

// This call allocates BytesMut::zeroed(8388608) before returning Err
let result = codec.decode(&mut frame);
assert!(result.is_err()); // session dropped, but 8MB was allocated

// Repeat across 117 concurrent inbound sessions → ~936MB peak RSS
```

**Varint verification:** `0x80 0x80 0x80 0x04` decodes as `0 + (0<<7) + (0<<14) + (4<<21) = 8,388,608`, which equals `MAX_UNCOMPRESSED_LEN` exactly, confirming the off-by-one bypass.