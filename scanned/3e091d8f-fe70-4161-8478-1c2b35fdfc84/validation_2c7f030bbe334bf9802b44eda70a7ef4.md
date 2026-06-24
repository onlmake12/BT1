Audit Report

## Title
Unconditional Double Block Forwarding in `accept_remote_block` via Relay `BlockTransactions` Path — (`sync/src/types/mod.rs`, `sync/src/relayer/mod.rs`)

## Summary
`SyncShared::accept_remote_block` unconditionally calls `chain.asynchronous_process_remote_block` regardless of whether the `block_status_map` entry was vacant or occupied. `Relayer::accept_block` guards only against `BlockStatus::BLOCK_STORED`, not `BLOCK_RECEIVED`. When a `CompactBlock` with missing transactions is stored in `pending_compact_blocks` and the same block subsequently arrives via the Sync `SendBlock` path (setting `BLOCK_RECEIVED`), the later arrival of `BlockTransactions` causes `accept_block` → `accept_remote_block` to enqueue the block into `ChainService` a second time, producing double non-contextual verification, double DB insertion, and a spurious error callback that can falsely punish the relaying peer.

## Finding Description

**Root cause — `accept_remote_block` does not gate forwarding on the `Vacant` result:**

```rust
// sync/src/types/mod.rs L1075-1087
pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
    {
        let entry = self.shared().block_status_map()
            .entry(remote_block.block.header().hash());
        if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
            entry.insert(BlockStatus::BLOCK_RECEIVED);
        }
        // ↑ Occupied branch is silently ignored
    }
    chain.asynchronous_process_remote_block(remote_block) // ← unconditional
}
```

The `Entry::Vacant` guard prevents a second status write but does **not** prevent the block from being forwarded to `ChainController` a second time.

**Relay path — `accept_block` guards only `BLOCK_STORED`:**

```rust
// sync/src/relayer/mod.rs L281-287
if self.shared().active_chain()
    .contains_block_status(&block.hash(), BlockStatus::BLOCK_STORED) {
    return;
}
// ... proceeds to call accept_remote_block even if BLOCK_RECEIVED
```

**Concrete exploit sequence:**

1. Peer B sends `CompactBlock` for block X. `CompactBlockProcess::contextual_check` sees no `BLOCK_RECEIVED` entry, proceeds, finds missing transactions, stores the compact block in `pending_compact_blocks`, and returns without calling `accept_block`.
2. Peer A sends `SendBlock` for the same block X. `BlockProcess::execute` calls `new_block_received`, which atomically sets `BLOCK_RECEIVED` and returns `true`. `asynchronous_process_remote_block` → `accept_remote_block` → `chain.asynchronous_process_remote_block` is called (first enqueue).
3. Peer B sends `BlockTransactions` for block X. `BlockTransactionsProcess` reconstructs the full block and calls `Relayer::accept_block`. `accept_block` checks only `BLOCK_STORED` (not `BLOCK_RECEIVED`), finds neither, and calls `accept_remote_block`. The `Entry::Vacant` guard finds the entry Occupied but still calls `chain.asynchronous_process_remote_block` (second enqueue).

**`CompactBlockProcess` has a `BLOCK_RECEIVED` guard** (`contextual_check` L256-258), which blocks the direct compact-block path. However, this guard is bypassed when the compact block arrives *before* `BLOCK_RECEIVED` is set and the node is waiting for `BlockTransactions`.

**`new_block_received` guards the sync path** (L1216: `!BlockStatus::HEADER_VALID.eq(&status)` returns `false` for `BLOCK_RECEIVED`), but this does not prevent the relay `BlockTransactions` path from calling `accept_remote_block` a second time.

**`ChainService::asynchronous_process_block` has no deduplication guard:**

```rust
// chain/src/chain_service.rs L133-143
if let Err(err) = self.insert_block(&lonely_block) {
    self.shared.block_status_map().remove(&block_hash); // ← corrupts status
    lonely_block.execute_callback(Err(err));            // ← fires error callback
    return;
}
self.orphan_broker.process_lonely_block(lonely_block.into()); // ← double enqueue
```

If `insert_block` fails (RocksDB optimistic transaction conflict on concurrent writes to the same key), `block_status_map().remove` deletes the `BLOCK_RECEIVED` entry and `execute_callback(Err(err))` fires. The relay callback in `accept_block` then calls `post_sync_process` with `BlockIsInvalid`, banning the honest peer — unless `is_internal_db_error` classifies the error as `InternalErrorKind::Database`, which suppresses punishment. If `insert_block` succeeds (RocksDB `put` overwrites), the block is enqueued for verification twice via `orphan_broker.process_lonely_block`.

**`BlockStatus` bitflag encoding confirms `BLOCK_RECEIVED` ⊃ `HEADER_VALID`:**

```
HEADER_VALID   = 1        (0b0001)
BLOCK_RECEIVED = 3        (0b0011)  // contains HEADER_VALID
BLOCK_STORED   = 7        (0b0111)  // contains BLOCK_RECEIVED
```

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker controlling two peers can repeatedly trigger double non-contextual verification and double DB insertion for the same block by sending `CompactBlock` (with at least one missing transaction) followed by `SendBlock` from a second peer, then `BlockTransactions`. Each triggered race doubles CPU and memory consumption in `ChainService`. Across many connections and blocks this constitutes a low-cost amplification DoS against the node's chain processing pipeline. Additionally, if the second `insert_block` call fails with a non-DB error, the honest relay peer is falsely banned via `post_sync_process(BlockIsInvalid)`, degrading network connectivity.

## Likelihood Explanation

**Medium.** During normal operation — especially near the tip or during catch-up — it is common for the same block to arrive via both Sync (`SendBlock`) and Relay (`CompactBlock`/`BlockTransactions`). The vulnerable window is the interval between `CompactBlock` receipt (with missing transactions) and `BLOCK_STORED` being set, which spans the entire non-contextual verification and DB insertion phase. No special attacker capability is required beyond connecting as two peers and coordinating message timing. The `CompactBlock`-with-missing-transactions scenario is a normal network condition, not an artificial one.

## Recommendation

1. **Fix `accept_remote_block`** to gate forwarding on the `Vacant` result:
```rust
pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
    let is_new = {
        let entry = self.shared().block_status_map()
            .entry(remote_block.block.header().hash());
        matches!(entry, dashmap::mapref::entry::Entry::Vacant(e) if {
            e.insert(BlockStatus::BLOCK_RECEIVED); true
        })
    };
    if is_new {
        chain.asynchronous_process_remote_block(remote_block);
    }
}
```

2. **Fix `Relayer::accept_block`** to guard against `BLOCK_RECEIVED` in addition to `BLOCK_STORED`, consistent with `CompactBlockProcess::contextual_check`.

3. **Fix `ChainService::asynchronous_process_block`** to check whether the block is already stored before calling `insert_block`, and return early without removing status or firing an error callback if so.

## Proof of Concept

**Minimal manual steps:**

1. Connect peer A (Sync protocol) and peer B (Relay protocol) to the target node.
2. Peer B sends `CompactBlock` for block X with at least one short transaction ID not in the node's mempool. The node stores the compact block in `pending_compact_blocks` and sends `GetBlockTransactions` to peer B.
3. Before peer B responds, peer A sends `SendBlock` for block X. The node calls `new_block_received` (sets `BLOCK_RECEIVED`), then `chain.asynchronous_process_remote_block` (first enqueue).
4. Peer B sends `BlockTransactions` for block X. `BlockTransactionsProcess` reconstructs the full block, calls `Relayer::accept_block`, which checks only `BLOCK_STORED` (not set), calls `accept_remote_block`, which finds the entry Occupied but still calls `chain.asynchronous_process_remote_block` (second enqueue).
5. Observe: `ChainService` processes block X twice — double `non_contextual_verify`, double `insert_block`, double `orphan_broker.process_lonely_block`. If the second `insert_block` fails, `block_status_map` entry is removed and the relay callback fires `Err`, potentially banning peer B.

**Invariant test:** Assert that for any block hash, `chain.asynchronous_process_remote_block` is called at most once. This invariant is violated by the above sequence and can be confirmed with a unit test that mocks `ChainController` and counts invocations.