#Audit Report

## Title
Unsolicited `SendBlock` Full Deserialization Before Inflight Check Enables CPU Exhaustion — (`sync/src/synchronizer/block_process.rs`)

## Summary

`BlockProcess::execute()` unconditionally deserializes the full block (byte copy + Blake2b hash computation) before checking whether the block was ever solicited via the inflight set. The `Synchronizer` has no rate limiter for `SendBlock` messages, unlike the `Relayer` which enforces 30 req/s per peer. Any peer can flood the node with valid-molecule `SendBlock` messages for unsolicited blocks, consuming CPU proportional to block size per message with no throttle, potentially crashing or severely degrading the node.

## Finding Description

In `BlockProcess::execute()`, line 35 performs full deserialization unconditionally:

```rust
let block = Arc::new(self.message.block().to_entity().into_view());
```

`to_entity()` copies all raw bytes into owned types; `into_view()` computes Blake2b hashes for the block header and all transactions. Only after this work does line 43 call `shared.new_block_received(&block)`.

`new_block_received` calls `remove_by_block` on `inflight_states`. If the block hash was never inserted into `inflight_states` by the local node, `remove_by_block` returns `false` (the `inflight_states.remove(&block)` returns `None`, so `.is_some()` is `false`), and the function returns `false` at line 1206. The block is silently dropped, but all CPU work has already been done.

Critically, the `SendBlock` arm in `try_process` (lines 412–418 of `sync/src/synchronizer/mod.rs`) does **not** wrap the call in `tokio::task::block_in_place`, unlike `GetHeaders`, `SendHeaders`, and `GetBlocks`. This means the deserialization runs directly on the async executor thread, blocking it and potentially starving all other peer connections processed by the same runtime.

The `Synchronizer` struct has no `rate_limiter` field — confirmed by the absence of any `RateLimiter` in `sync/src/synchronizer/mod.rs`. This is in direct contrast to the `Relayer`, which explicitly rate-limits at 30 req/s per peer per message type.

## Impact Explanation

An attacker with a single P2P connection can send a continuous stream of valid-molecule `SendBlock` messages for blocks not in the local inflight set. Each message triggers full byte copy and Blake2b hash computation over the entire block payload (up to ~597 KB), with no throttle. Because the processing runs on the async executor thread without `block_in_place`, it can block the tokio runtime, degrading or crashing the node's ability to process messages from all peers. This maps to **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires only a valid P2P handshake — no special privileges, no valid PoW. Valid molecule encoding is trivially constructable. The attack is reproducible with a single connection and minimal bandwidth relative to the CPU cost imposed on the victim. It is repeatable indefinitely with no per-peer throttle.

## Recommendation

1. **Move the inflight check before deserialization**: Use the zero-copy `SendBlockReader` to extract the block hash from the raw molecule bytes and check `inflight_states` before calling `to_entity().into_view()`.
2. **Add a rate limiter to `Synchronizer`** for `SendBlock` messages, mirroring the existing `Relayer` rate limiter (30 req/s per peer per message type).
3. **Wrap `SendBlock` processing in `tokio::task::block_in_place`**, consistent with the other sync message handlers, to prevent blocking the async executor.

## Proof of Concept

Connect to a CKB node as a peer. Construct valid-molecule `SyncMessage::SendBlock` payloads containing a large block structure (any block hash not in the node's inflight set, no valid PoW required). Send these messages in a tight loop from a single connection. Observe CPU usage on the victim node spike proportionally to block size per message, with no throttling applied. The async executor thread will be blocked during each deserialization, measurably degrading response latency for all other connected peers.