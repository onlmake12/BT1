The code confirms the key claims. Let me verify the exact line numbers and structure before rendering the final output.

The nested loop at [1](#0-0)  is confirmed: the outer loop runs `period` times, the inner loop runs `current_epoch_length` times, and each inner iteration calls `self.condition(&header, indexer)` (which calls `indexer.cellbase()` — 1 DB read at [2](#0-1) ) plus `indexer.block_header()` (1 more DB read at [3](#0-2) ), with 3 additional DB reads per outer iteration at [4](#0-3) .

The `ancestor_epoch` unbounded while loop is confirmed at [5](#0-4) .

The cache key is `last_block_hash_in_previous_epoch()` at [6](#0-5)  and [7](#0-6) , using file-based `cacache` at [8](#0-7) .

The `get_deployments_info` RPC path is confirmed at [9](#0-8) , and the `get_block_template` path via `compute_versionbits` is confirmed at [10](#0-9) .

The commented-out cheaper alternative (checking `header.version()` directly without a DB read) exists at [11](#0-10) .

---

Audit Report

## Title
Unbounded Nested Loop in `get_state` Causes Excessive DB Reads on Cache Miss After Reorg — (`File: spec/src/versionbits/mod.rs`)

## Summary
`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a nested loop that, on a cache miss (e.g., after a reorg invalidates affected epoch cache entries), performs O(period × epoch_length) RocksDB reads per uncached signalling period. The cache is keyed by `last_block_hash_in_previous_epoch`, which changes for all epochs at and after the reorg point, forcing full recomputation of those periods. This path is reachable by any unprivileged RPC caller via `get_deployments_info` and by any miner via `get_block_template`.

## Finding Description
**Nested counting loop (cold `Started` state):**

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

Each inner iteration performs 2 DB reads (`cellbase` + `block_header`). With `period = 10` and `epoch_length = 1800`, a single uncached `Started` period costs ~36,000 DB reads.

**`condition` reads cellbase per block:**

The `condition` function at lines 440–455 calls `indexer.cellbase(&header.hash())` for every block to read the cellbase witness. A commented-out alternative at lines 457–460 checks `header.version()` directly with zero DB reads, but is not used.

**Outer backward walk:**

Lines 291–305 contain an unbounded `loop` that walks backward through the chain `period` epochs at a time, calling `ancestor_epoch` at each step. `ancestor_epoch` at lines 109–115 is itself an unbounded `while` loop performing 3 DB reads per epoch step.

**Cache invalidation on reorg:**

The `Cache` is a file-based `cacache` store (lines 154–164) keyed by `epoch_ext.last_block_hash_in_previous_epoch()` (lines 292, 359). The cache persists across node restarts (stored on disk at `data_dir/softfork/<pos>`). However, after any reorg, all epochs at and after the reorg point have different `last_block_hash_in_previous_epoch` values, causing cache misses for those epochs and forcing full recomputation of every affected period in `Started` state.

**Reachable paths:**

1. `get_deployments_info` RPC (`rpc/src/module/stats.rs` lines 153–185) calls `versionbits_state` → `get_state` for every deployment. Any unprivileged caller can trigger this.
2. `get_block_template` miner path — `tx-pool/src/block_assembler/mod.rs` line 503 calls `snapshot.compute_versionbits(tip)` → `get_state` for every active deployment on every block template build.

**No existing guard:** There is no upper bound on the number of `to_compute` entries, no limit on DB reads in the inner loop, and no timeout or circuit-breaker.

## Impact Explanation
**Medium — Suboptimal implementation of CKB state storage mechanism.**

After a reorg (a normal operational event), all versionbits cache entries for affected epochs are silently invalidated. The next call to `get_deployments_info` or `get_block_template` triggers O(period × epoch_length) DB reads per uncached `Started` period. With `period = 10` and `epoch_length = 1800`, each period costs ~36,000 DB reads; a reorg spanning many epochs multiplies this cost. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles the cost. The node does not crash and the cache warms after the first call, so this does not reach the High threshold of "easily crash a CKB node." The impact is concrete, measurable RPC latency and block template generation delay matching the Medium bounty class.

## Likelihood Explanation
Reorgs are normal operational events on CKB mainnet and require no attacker resources to occur naturally. A miner calling `get_block_template` immediately after a reorg (standard mining behavior) triggers the full cold-cache computation for all affected epochs. An unprivileged user calling `get_deployments_info` after a reorg also triggers it. The computation is a one-time cost per reorg per deployment, but on a chain with frequent small reorgs or during a large reorg event, this can be triggered repeatedly. No special privileges or exotic conditions are required.

## Recommendation
1. **Avoid per-block cellbase reads in `condition`**: Use the commented-out alternative at lines 457–460 that checks `header.version()` directly, eliminating the `cellbase` DB read per block entirely.
2. **Precompute eagerly on chain tip advance**: Update the versionbits cache when a new block is committed to the chain (in the chain service), so `get_state` always finds a warm cache at query time.
3. **Reorg-aware cache invalidation**: Store the epoch number alongside the cached state so that after a reorg, only the affected epochs are invalidated rather than all epochs from the reorg point forward.
4. **Bound the backward walk**: Add a hard upper bound on the number of `to_compute` entries and surface an error if exceeded, rather than silently performing unbounded work.

## Proof of Concept
1. Run a CKB node on mainnet or a long-running testnet with a softfork deployment in `Started` state (e.g., `LightClient`).
2. Trigger a reorg of depth ≥ 1 epoch (e.g., via a competing chain tip).
3. Immediately call `get_deployments_info` via RPC or trigger `get_block_template`.
4. Observe: `get_state` walks backward from the current epoch to the reorg point, performing `period × epoch_length` DB reads for each uncached period in `Started` state. With `period = 10` and `epoch_length = 1800`, each period costs ~36,000 DB reads; across many affected periods this scales to millions of reads. The RPC call blocks for the duration of this computation.
5. A unit test can be constructed by mocking `VersionbitsIndexer` with a counter on `block_header` and `cellbase` calls, setting `period = 10`, `epoch_length = 1800`, and a cold cache, then asserting the call count matches the expected O(period × epoch_length) formula per uncached period.

### Citations

**File:** spec/src/versionbits/mod.rs (L109-115)
```rust
        while epoch_ext.number() > target {
            let last_block_header_in_previous_epoch =
                self.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;
            let previous_epoch_index =
                self.block_epoch_index(&last_block_header_in_previous_epoch.hash())?;
            epoch_ext = self.epoch_ext(&previous_epoch_index)?;
        }
```

**File:** spec/src/versionbits/mod.rs (L154-164)
```rust
    #[cfg(not(target_family = "wasm"))]
    pub fn get(&self, key: &Byte32) -> Option<ThresholdState> {
        match cacache::read_sync(&self.path, Self::encode_key(key)) {
            Ok(bytes) => Some(Self::decode_value(bytes)),
            Err(cacache::Error::EntryNotFound(_path, _key)) => None,
            Err(err) => {
                error!("cacache read_sync failed {:?}", err);
                None
            }
        }
    }
```

**File:** spec/src/versionbits/mod.rs (L292-293)
```rust
            let epoch_index = epoch_ext.last_block_hash_in_previous_epoch();
            if let Some(value) = cache.get(&epoch_index) {
```

**File:** spec/src/versionbits/mod.rs (L326-340)
```rust
                    for _ in 0..period {
                        let current_epoch_length = current_epoch_ext.length();
                        total += current_epoch_length;
                        for _ in 0..current_epoch_length {
                            if self.condition(&header, indexer) {
                                count += 1;
                            }
                            header = indexer.block_header(&header.parent_hash())?;
                        }
                        let last_block_header_in_previous_epoch = indexer
                            .block_header(&current_epoch_ext.last_block_hash_in_previous_epoch())?;
                        let previous_epoch_index = indexer
                            .block_epoch_index(&last_block_header_in_previous_epoch.hash())?;
                        current_epoch_ext = indexer.epoch_ext(&previous_epoch_index)?;
                    }
```

**File:** spec/src/versionbits/mod.rs (L359-359)
```rust
            cache.insert(&epoch_ext.last_block_hash_in_previous_epoch(), state);
```

**File:** spec/src/versionbits/mod.rs (L441-441)
```rust
        if let Some(cellbase) = indexer.cellbase(&header.hash())
```

**File:** spec/src/versionbits/mod.rs (L457-460)
```rust
    // fn condition(&self, header: &HeaderView) -> bool {
    //     let version = header.version();
    //     (((version & VERSIONBITS_TOP_MASK) == VERSIONBITS_TOP_BITS) && (version & self.mask()) != 0)
    // }
```

**File:** rpc/src/module/stats.rs (L153-185)
```rust
    fn get_deployments_info(&self) -> Result<DeploymentsInfo> {
        let snapshot = self.shared.snapshot();
        let deployments: BTreeMap<DeploymentPos, DeploymentInfo> = self
            .shared
            .consensus()
            .deployments
            .clone()
            .into_iter()
            .filter_map(|(pos, deployment)| {
                self.shared
                    .consensus()
                    .versionbits_state(pos, snapshot.tip_header(), snapshot.as_ref())
                    .map(|state| {
                        let mut info: DeploymentInfo = deployment.into();
                        info.state = state.into();
                        if let Some(since) = self.shared.consensus().versionbits_state_since_epoch(
                            pos,
                            snapshot.tip_header(),
                            snapshot.as_ref(),
                        ) {
                            info.since = since.into();
                        }
                        (pos.into(), info)
                    })
            })
            .collect();

        Ok(DeploymentsInfo {
            hash: snapshot.tip_hash().into(),
            epoch: snapshot.tip_header().epoch().number().into(),
            deployments,
        })
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L503-503)
```rust
        if let Some(version) = snapshot.compute_versionbits(tip) {
```
