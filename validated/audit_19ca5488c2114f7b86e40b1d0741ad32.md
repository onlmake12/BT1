Audit Report

## Title
Unsolicited `SendBlock` Full Deserialization Before Inflight Check Enables CPU Exhaustion — (`sync/src/synchronizer/block_process.rs`)

## Summary

`BlockProcess::execute()` unconditionally deserializes the full block payload via `to_entity().into_view()` at line 35 before checking at line 43 whether the block was ever solicited via `new_block_received`. The `Synchronizer` has no rate limiter for `SendBlock` messages, unlike the `Relayer` which enforces 30 req/s per peer. Any peer can flood the node with valid-molecule `SendBlock` messages for unsolicited blocks, consuming CPU proportional to block size per message with no throttle.

## Finding Description

In `BlockProcess::execute()`, full deserialization is unconditional:

```rust
// sync/src/synchronizer/block_process.rs, line 35
let block = Arc::new(self.message.block().to_entity().into_view());
// ...
// line 43 — inflight check happens AFTER all CPU work
if shared.new_block_received(&block) {
```

`to_entity()` copies all raw bytes into owned types; `into_view()` computes Blake2b hashes for the block header and every transaction. Only after this work does `new_block_received` call `write_inflight_blocks().remove_by_block(...)`. If the block hash was never inserted into `inflight_states`, `remove_by_block` returns `false` (via `.is_some()` on a `None` removal result), and `new_block_received` returns `false` immediately — the block is silently dropped, but all CPU work has already been completed.

The `Synchronizer` struct has no rate limiter field:

```rust
// sync/src/synchronizer/mod.rs, lines 357-362
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
```

The `try_process` dispatch for `SendBlock` applies only a structural check before invoking `BlockProcess`, with no rate check:

```rust
// sync/src/synchronizer/mod.rs, lines 412-418
packed::SyncMessageUnionReader::SendBlock(reader) => {
    if reader.check_data() {
        BlockProcess::new(reader, self, peer, nc).execute()
    } else {
        StatusCode::ProtocolMessageIsMalformed.with_context("SendBlock is invalid")
    }
}
```

This contrasts with the `Relayer`, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and enforces 30 req/s per peer per message type before any processing.

## Impact Explanation

An attacker with a single P2P connection can send a continuous stream of valid-molecule `SendBlock` messages for blocks not in the local inflight set. Each message triggers: (1) `check_data()` molecule structural validation, (2) `to_entity()` full byte copy of the block payload, (3) `into_view()` Blake2b hash computation over header and all transactions, (4) `Arc::new` allocation, (5) `remove_by_block` lookup returning false. For a block near the consensus size limit (~597 KB), this is significant CPU work per message. With no rate limiting, the attacker can saturate CPU resources, degrading sync throughput for all honest peers. This matches the **High** impact: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The attack requires only a valid P2P handshake — no special privileges, no valid PoW. Only valid molecule encoding is needed, which is trivially constructable from the public molecule schema. A single connection is sufficient. The attack is continuously repeatable with minimal bandwidth relative to the CPU cost imposed on the victim (amplification proportional to block size). The asymmetry between attacker cost (sending bytes) and victim cost (full deserialization + hashing) makes this a practical and low-effort attack.

## Recommendation

1. **Move the inflight check before deserialization**: Use the zero-copy `SendBlockReader` to extract the block hash from the raw molecule bytes and check `inflight_states` before calling `to_entity().into_view()`. Molecule readers are zero-copy and do not compute hashes.
2. **Add a rate limiter to `Synchronizer`** for `SendBlock` messages, mirroring the existing `Relayer` rate limiter (30 req/s per peer per message type) at the `try_process` dispatch level.

## Proof of Concept

1. Connect to a CKB node as a peer via the Sync protocol (complete the P2P handshake).
2. Construct a valid-molecule `SendBlock` message containing a block with a hash not present in the node's inflight set. The block does not need valid PoW — only valid molecule structure.
3. Send the message in a tight loop from a single connection.
4. Observe CPU usage on the victim node: each message triggers full deserialization (`to_entity().into_view()`) with no throttle, while the block is silently dropped after the inflight check fails.
5. Measure CPU time per message vs. bytes sent to confirm amplification proportional to block size with no rate limiting applied.