Audit Report

## Title
Unbounded Nested Loop in `get_state` Causes O(epoch_number × epoch_length) DB Reads on Cold Cache — (`File: spec/src/versionbits/mod.rs`)

## Summary
`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a nested loop that, on a cold cache (after a reorg or fresh install), iterates over every block in every uncached signalling period, performing multiple RocksDB reads per block. The total work scales as O(epoch_number × epoch_length), which on mainnet can reach approximately 1.8 million DB reads per call. This function is reachable by any unprivileged RPC caller via `get_deployments_info` and by any miner via `get_block_template`.

## Finding Description

**Inner counting loop (cold `Started` state):**

In `get_state` at lines 326–340 of `spec/src/versionbits/mod.rs`, when the deployment is in `ThresholdState::Started` and the cache is cold, the function executes a nested loop:

```rust
for _ in 0..period {
    let current_epoch_length = current_epoch_ext.length();
    total += current_epoch_length;
    for _ in 0..current_epoch_length {
        if self.condition(&header, indexer) { count += 1; }  // → indexer.cellbase() (1 DB read)
        header = indexer.block_header(&header.parent_hash())?;  // 1 DB read
    }
    // 3 more DB reads per outer iteration (lines 335–339)
}
```

Each inner iteration performs 2 DB reads (`cellbase` + `block_header`), and each outer iteration adds 3 more. With `period = 10` and `epoch_length = 1800`, a single uncached `Started` period costs ~36,000 DB reads.

**Outer backward walk:**

Before reaching the counting loop, lines 291–305 contain an unbounded `loop` that walks backward through the chain `period` epochs at a time, calling `ancestor_epoch` at each step. `ancestor_epoch` (lines 109–115) is itself an unbounded `while` loop performing 3 DB reads per epoch step:

```rust
while epoch_ext.number() > target {
    let last_block_header_in_previous_epoch =
        self.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;  // 1 DB read
    let previous_epoch_index =
        self.block_epoch_index(&last_block_header_in_previous_epoch.hash())?;  // 1 DB read
    epoch_ext = self.epoch_ext(&previous_epoch_index)?;  // 1 DB read
}
```

**Cache invalidation on reorg:**

The `Cache` is a file-based `cacache` store (lines 154–164, 212–224) keyed by `epoch_ext.last_block_hash_in_previous_epoch()`. The cache **does persist across node restarts** (it is stored on disk at `data_dir/softfork/<pos>`), so the "cold after restart" claim in the submission is inaccurate. However, after any reorg, the reorged epochs have different `last_block_hash_in_previous_epoch` values, causing all cache misses on the new chain. This forces a full recomputation from epoch 0 to the current tip.

**Reachable paths:**

1. `get_deployments_info` RPC (not `get_blockchain_info` as the submission incorrectly states) — `rpc/src/module/stats.rs` lines 153–185 call `versionbits_state` → `get_state` for every deployment. Any unprivileged caller can trigger this.
2. `get_block_template` miner path — `tx-pool/src/block_assembler/mod.rs` line 503 calls `snapshot.compute_versionbits(tip)` → `get_state` for every active deployment on every block template build.

**No existing guard:** There is no upper bound on the number of `to_compute` entries pushed in the outer loop, no limit on the number of DB reads in the inner loop, and no timeout or circuit-breaker on the computation.

## Impact Explanation

This is a **Medium** impact finding: **Suboptimal implementation of CKB state storage mechanism**.

The caching strategy is structurally flawed: after any reorg (a normal operational event), all cached versionbits states are silently invalidated because the cache keys are derived from `last_block_hash_in_previous_epoch`, which changes on the new chain. The full O(epoch_number × epoch_length) recomputation is then triggered on the next call to `get_deployments_info` or `get_block_template`, causing significant RPC latency and block template generation delay. On mainnet with ~1800 blocks/epoch and epoch number ~1000, this is approximately 1.8 million RocksDB reads per deployment per cold call. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles this cost.

The node does not crash and the computation is self-healing (cache warms after first call), so this does not reach the High threshold of "easily crash a CKB node." The impact is concrete performance degradation matching the Medium bounty class.

## Likelihood Explanation

Reorgs are normal operational events on CKB mainnet and do not require attacker resources to occur naturally. A miner calling `get_block_template` immediately after a reorg (standard mining behavior) triggers the full cold-cache computation. An unprivileged user calling `get_deployments_info` after a reorg also triggers it. The computation is a one-time cost per reorg per deployment, but on a chain with frequent small reorgs or during a large reorg event, this can be triggered repeatedly. No special privileges or exotic conditions are required.

## Recommendation

1. **Reorg-aware cache invalidation**: Instead of keying the cache solely on `last_block_hash_in_previous_epoch`, store the epoch number alongside the cached state so that after a reorg, only the affected epochs are invalidated rather than the entire cache.
2. **Precompute eagerly on chain tip advance**: Update the versionbits cache when a new block is committed to the chain (in the chain service), so `get_state` always finds a warm cache at query time.
3. **Bound the backward walk**: Add a hard upper bound on the number of `to_compute` entries and surface an error if exceeded, rather than silently performing unbounded work.
4. **Avoid per-block cellbase reads in `condition`**: The `condition` function (lines 440–455) reads the cellbase for every block. Consider caching or batching these reads, or checking the block version field directly as the commented-out alternative (lines 457–460) suggests.

## Proof of Concept

1. Run a CKB node on mainnet or a long-running testnet with a softfork deployment in `Started` state (e.g., `LightClient`).
2. Trigger a reorg of depth ≥ 1 epoch (e.g., via a competing chain tip).
3. Immediately call `get_deployments_info` via RPC or trigger `get_block_template`.
4. Observe: `get_state` walks backward from the current epoch to epoch 0, performing `period × epoch_length` DB reads for each uncached period in `Started` state. With `period = 10` and `epoch_length = 1800`, each period costs ~36,000 DB reads; across 100 periods this is ~3.6 million reads (2 reads/block × 1800 blocks × 10 epochs + 3 reads/epoch × 10 epochs × 100 periods). The RPC call blocks for the duration of this computation.
5. A unit test can be constructed by mocking `VersionbitsIndexer` with a counter on `block_header` and `cellbase` calls, setting `period = 10`, `epoch_length = 1800`, `epoch_number = 1000`, and asserting the call count exceeds 1 million on a cold cache.