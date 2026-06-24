Audit Report

## Title
Unbounded Nested Loop in `get_state` Causes O(epoch_number × epoch_length) DB Reads on Cold Cache — (File: spec/src/versionbits/mod.rs)

## Summary
`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a backward-walk loop that calls `ancestor_epoch` (itself an unbounded `while` loop) and, for each uncached `Started`-state period, an inner nested loop that performs `2 × period × epoch_length` RocksDB reads. On a cold disk cache (first run or post-reorg), total work scales as O(epoch_number × epoch_length). The function is reachable by any unprivileged caller via the `get_deployments_info` RPC and by miners via `get_block_template`, causing temporary but significant stalls.

## Finding Description
**Root cause 1 — `ancestor_epoch` unbounded while loop** (`spec/src/versionbits/mod.rs`, lines 109–115):

`ancestor_epoch` walks backward one epoch per iteration, performing 3 DB reads per step (`block_header` + `block_epoch_index` + `epoch_ext`). There is no depth limit or circuit-breaker. The outer backward-walk loop in `get_state` (lines 291–305) calls `ancestor_epoch` once per uncached period, stepping back `period` epochs each time, until a cached entry or genesis is found. With `epoch_number / period` uncached periods, this costs `3 × epoch_number` DB reads.

**Root cause 2 — inner counting loop** (`spec/src/versionbits/mod.rs`, lines 326–340):

For each uncached period in `ThresholdState::Started`, `get_state` runs a nested loop: outer iterates `period` times, inner iterates `current_epoch_length` times. Each inner iteration calls `self.condition(&header, indexer)` → `indexer.cellbase()` (1 DB read) and `indexer.block_header(&header.parent_hash())` (1 DB read). This is `2 × period × epoch_length` DB reads per uncached `Started` period, totalling `2 × epoch_number × epoch_length` reads across all uncached periods.

**Combined worst-case (cold cache):** O(epoch_number × epoch_length). On mainnet (~1000 epochs, ~1800 blocks/epoch, `period = 10`): ~1.8 million DB reads per `get_state` call. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles this.

**Why existing guards are insufficient:**

The `Cache` is a disk-based `cacache` store (lines 154–164) keyed by `epoch_ext.last_block_hash_in_previous_epoch()`. It survives normal restarts but is fully invalidated after any reorg, because the `last_block_hash_in_previous_epoch` values on the new chain differ from those on the old chain. On first run there is no cache at all. There is no upper bound on `to_compute` size, no depth limit on `ancestor_epoch`, and no circuit-breaker on the inner counting loop.

**Reachable paths:**

1. `get_deployments_info` RPC (`rpc/src/module/stats.rs`, lines 153–185) calls `consensus.versionbits_state(pos, tip_header, snapshot)` for every deployment, directly invoking `get_state`.
2. `BlockAssembler::build_cellbase_witness` (`tx-pool/src/block_assembler/mod.rs`, line 503) calls `snapshot.compute_versionbits(tip)`, which calls `get_state` for every deployment on every block-template build.

## Impact Explanation
The impact is a significant but temporary performance degradation: the RPC handler thread stalls for the duration of the O(epoch_number × epoch_length) computation, blocking responses to all concurrent callers sharing the same thread pool, and delaying block-template generation for miners. The node does not crash and no consensus state is corrupted. This matches the allowed impact: **Low (501–2000 points) — any other important performance improvements for CKB**.

## Likelihood Explanation
The expensive path is triggered on first run (no cache) and after any reorg that invalidates cached epoch hashes. First-run cost is a one-time event. Reorg-triggered cost requires an adversary to cause a reorg, which is not a zero-cost operation. Repeated triggering by a purely unprivileged caller (e.g., spamming `get_deployments_info`) is not possible because the cache is populated after the first call and subsequent calls are fast. The scenario is realistic for node operators on first startup or after a deep reorg, but not a freely repeatable remote attack.

## Recommendation
1. **Bound `to_compute` depth**: Add a hard upper limit on the number of entries pushed into `to_compute` and return an error if exceeded, preventing unbounded backward walks.
2. **Precompute eagerly**: Update the versionbits cache when a new block is committed to the chain tip, so `get_state` always finds a warm cache at query time.
3. **Store epoch number in cache**: Record the epoch number of the last cached state so `get_state` can skip directly to it without re-walking `ancestor_epoch` one period at a time.
4. **Bound `ancestor_epoch`**: Add a maximum iteration count to the `while` loop in `ancestor_epoch` (lines 109–115) and surface an error rather than walking unboundedly.

## Proof of Concept
1. Run a CKB node on mainnet or a long-running testnet with a softfork deployment in `Started` state and allow the `cacache` to populate normally.
2. Simulate a deep reorg (or delete the `softfork/` cache directory under the data dir) to cold the cache.
3. Call `get_deployments_info` via RPC (or trigger `get_block_template` as a miner).
4. Observe: `get_state` walks backward from the current epoch to epoch 0, performing `period × epoch_length` DB reads for each uncached period in `Started` state. With `period = 10` and `epoch_length = 1800`, each period costs ~18,000 DB reads; 100 uncached periods cost ~1.8 million reads. The RPC call blocks for the duration of this computation.
5. Confirm by adding instrumentation to count `block_header` and `cellbase` calls inside `get_state` and verifying the count matches the O(epoch_number × epoch_length) prediction.