Audit Report

## Title
Unbounded OrphanBlockPool Growth via PoW-Free Block Flooding — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/chain_service.rs`)

## Summary
An unprivileged remote peer can send `SendBlock` sync messages containing syntactically valid blocks with random unknown parent hashes. These blocks pass `non_contextual_verify` without any PoW work, are written to RocksDB via `insert_block`, and are inserted into `OrphanBlockPool` which has no enforced size cap. Sustained flooding causes unbounded heap and disk growth, ultimately crashing the node via OOM kill or disk exhaustion.

## Finding Description

**Root cause 1 — No PoW in `non_contextual_verify`.**

`ChainService::non_contextual_verify` invokes only `BlockVerifier` and `NonContextualBlockTxsVerifier`. `BlockVerifier::verify` runs only structural checks (`BlockProposalsLimitVerifier`, `BlockBytesVerifier`, `CellbaseVerifier`, `DuplicateVerifier`, `MerkleRootVerifier`) — no `PowVerifier`. PoW is only checked inside `HeaderVerifier`, which is a contextual verifier requiring the parent to be known and is called in the relay path (`CompactBlockProcess`) but not in the sync `SendBlock` path.

**Root cause 2 — No hard size limit in `OrphanBlockPool`.**

`ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW = 8192` is passed to `HashMap::with_capacity`, which is an advisory pre-allocation hint, not a cap. `InnerPool::insert` performs zero size checks before inserting into `blocks`, `parents`, and `leaders`.

**Root cause 3 — Expiry cleanup is ineffective against a live attacker.**

`clean_expired_orphans` fires every 60 seconds and only removes blocks where `epoch_number + EXPIRED_EPOCH (6) < tip_epoch`. An attacker crafting blocks with the current epoch number will not be evicted for approximately 24 hours on mainnet.

**Reachable attack path:**

```
Peer sends SendBlock (sync protocol)
  → Synchronizer::try_process (only check_data() structural check)
  → BlockProcess::execute
  → shared.new_block_received (dedup by hash — unique per crafted block)
  → chain.asynchronous_process_remote_block
  → process_block_tx channel (bounded 24 — throttles rate, not total)
  → ChainService::asynchronous_process_block
  → non_contextual_verify  ← passes without PoW
  → insert_block           ← writes to RocksDB (disk exhaustion)
  → orphan_broker.process_lonely_block
  → orphan_blocks_broker.insert()  ← no size limit, unbounded heap growth
```

The `SendBlock` path in `sync/src/synchronizer/block_process.rs` does not invoke `HeaderVerifier` (which contains `PowVerifier`). The compact block relay path does call `contextual_check` → `HeaderVerifier` → `PowVerifier`, but the sync `SendBlock` path has no equivalent guard.

## Impact Explanation

Each inserted orphan block allocates entries in three `HashMap` structures (`blocks`, `parents`, `leaders`). With no eviction policy and no hard cap, a sustained flood of crafted blocks causes unbounded RSS growth, ultimately triggering an OOM kill of the node process. Concurrently, `insert_block` writes each block to RocksDB before orphan insertion, exhausting disk space in parallel. This constitutes **High impact: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires no PoW computation, no privileged access, and no Sybil capability. A single peer connection over the sync protocol is sufficient. Crafting a valid block (correct cellbase structure, valid merkle root, within size limits, random parent hash) is computationally trivial. The `process_block_tx` channel (capacity 24) throttles throughput slightly but does not bound total insertions. The attacker simply sends blocks at the rate the chain service consumes them, which is fast since `non_contextual_verify` is intentionally lightweight.

## Recommendation

1. **Enforce a hard size limit in `InnerPool::insert`**: reject or evict the oldest leader's subtree when `parents.len() >= capacity` before inserting a new block.
2. **Add `PowVerifier` to `non_contextual_verify`** (or to `BlockVerifier`): blocks without valid Eaglesong PoW should be rejected before reaching the orphan pool or the database.
3. **Do not call `insert_block` before the block's parent is known to be reachable**: defer the DB write until the block is promoted out of the orphan pool.
4. **Add per-peer rate limiting** on `SendBlock` submissions at the sync layer.

## Proof of Concept

```rust
// Connect to target node via sync protocol
// Craft N blocks each with a unique random parent hash
for _ in 0..N {
    let random_parent = Byte32::from(rand::random::<[u8; 32]>());
    let block = BlockBuilder::default()
        .parent_hash(random_parent)
        .number(1u64.pack())
        .epoch(EpochNumberWithFraction::new(current_epoch, 0, 1000).pack())
        .timestamp(unix_time_as_millis().pack())
        // valid cellbase + correct merkle root — no PoW needed
        .build_unchecked();
    // Send via SyncMessage::SendBlock
    peer.send_sync_message(SendBlock::new_builder().block(block.data()).build());
}
// Assert: orphan_blocks_broker.len() grows to N with no eviction
// Assert: node RSS grows proportionally; OOM kill at large N
// Assert: RocksDB data directory grows proportionally
```

Sending `BLOCK_DOWNLOAD_WINDOW * 10 = 81920` blocks is sufficient to demonstrate unbounded growth well past the nominal capacity hint of 8192.