Audit Report

## Title
Snappy Decompression Allocation Bypasses `max_frame_length` Bound in `LengthDelimitedCodecWithCompress::decode()` — (File: network/src/compress.rs)

## Summary
`LengthDelimitedCodecWithCompress::decode()` enforces `max_frame_length` only on the compressed wire bytes, but allocates a buffer sized by the snappy stream header's declared decompressed length (up to `MAX_UNCOMPRESSED_LEN` = 8 MB), which is independent of and larger than `max_frame_length`. Any unprivileged remote peer can trigger up to 4× memory amplification per frame on the Sync protocol by crafting a snappy stream whose header declares a large output size while keeping the compressed payload within the wire limit.

## Finding Description
In `network/src/compress.rs`, `MAX_UNCOMPRESSED_LEN` is hardcoded to `1 << 23` (8 MB) with no relationship to `max_frame_length`. [1](#0-0) 

`decode()` first calls `self.length_delimited.decode(src)`, which enforces `max_frame_length` on the wire frame (2 MB for Sync, 4 MB for RelayV3). [2](#0-1) [3](#0-2) 

When the compress flag is set, the code calls `decompress_len(&data[1..])` to read the snappy stream header's declared output size — a value the attacker fully controls — and only rejects if it exceeds `MAX_UNCOMPRESSED_LEN` (8 MB). [4](#0-3) 

It then unconditionally allocates `BytesMut::zeroed(decompressed_bytes_len)` — up to 8 MB — before any actual decompression occurs and before any check against `max_frame_length`. [5](#0-4) 

An attacker crafts a snappy stream where the compressed payload is just under `max_frame_length` (passes the wire check) and the snappy header declares a decompressed size of `MAX_UNCOMPRESSED_LEN - 1` (passes the `> MAX_UNCOMPRESSED_LEN` guard). The `BytesMut::zeroed(~8 MB)` call executes regardless. The `max_frame_length` invariant is completely bypassed for the decompressed allocation.

## Impact Explanation
With N simultaneous peers each sending crafted compressed frames on Sync, the node allocates N × 8 MB instead of the intended N × 2 MB — a 4× amplification. For a node with 100 inbound peers, this is 800 MB of peak allocation versus the expected 200 MB. On nodes with 1–2 GB RAM, sustained adversarial traffic across many connections can exhaust available memory and crash the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Any unprivileged peer that can establish a P2P connection can trigger this. No proof-of-work, no key, and no special role is required. The Sync and RelayV3 protocols are open to all peers by default. The attack is repeatable and sustained: a peer can continuously stream crafted frames, each triggering an 8 MB allocation. An attacker controlling or coordinating a moderate number of peers (e.g., 50–100) can sustain the memory pressure needed to crash a target node.

## Recommendation
In `decode()`, after reading `decompressed_bytes_len` from the snappy header, add a second guard rejecting frames where `decompressed_bytes_len > self.length_delimited.max_frame_length()`. [6](#0-5) 

This ensures the decompressed allocation is also bounded by `max_frame_length`, restoring the intended invariant. The same fix should be applied to `Message::decompress()` in the same file, which has an identical pattern. [7](#0-6) 

## Proof of Concept
1. Construct a snappy stream using the `snap` crate with a header declaring `MAX_UNCOMPRESSED_LEN - 1` (7,999,999) bytes of uncompressed output, with a minimal compressed payload (a few hundred bytes — well under the 2 MB Sync wire limit).
2. Wrap it in a length-delimited frame and feed it to `LengthDelimitedCodecWithCompress::decode()` configured with `max_frame_length = 2 * 1024 * 1024` (Sync).
3. Observe that `BytesMut::zeroed(7_999_999)` is called — allocating ~8 MB — despite the wire frame being only a few hundred bytes.
4. Assert `allocated_bytes > max_frame_length` — the invariant is broken.
5. Repeat across N concurrent connections to observe N × 8 MB RSS growth, confirming the amplification.

### Citations

**File:** network/src/compress.rs (L13-13)
```rust
const MAX_UNCOMPRESSED_LEN: usize = 1 << 23; // 8MB
```

**File:** network/src/compress.rs (L72-81)
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
```

**File:** network/src/compress.rs (L226-226)
```rust
        match self.length_delimited.decode(src)? {
```

**File:** network/src/compress.rs (L233-242)
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
```

**File:** network/src/protocols/support_protocols.rs (L129-130)
```rust
            SupportProtocols::Sync => 2 * 1024 * 1024,        // 2   MB
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```
