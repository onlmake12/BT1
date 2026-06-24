Audit Report

## Title
Unbounded Nested Loop in `get_state` Causes O(epoch_number × epoch_length) DB Reads on Cold Cache After Reorg — (`File: spec/src/versionbits/mod.rs`)

## Summary
`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a nested loop that, on a cold cache (after a reorg), iterates over every block in every uncached signalling period, performing multiple RocksDB reads per block. The cache is keyed by `last_block_hash_in_previous_epoch`, which changes on the reorged chain, causing all cache misses and forcing full recomputation from epoch 0. This is reachable by any unprivileged RPC caller via `get_deployments_info` and by any miner via `get_block_template`.

## Finding Description

**Inner counting loop (cold `Started` state):**

At lines 326–340 of `spec/src/versionbits/mod.rs`, when the deployment is in `ThresholdState::Started` and the cache is cold, the function executes a confirmed nested loop:

```rust
for _ in 0..period {
    let current_epoch_length = current_epoch_ext.length();
    total += current_epoch_length;
    for _ in 0..current_epoch_length {
        if self.condition(&header, indexer) { count += 1; }  // cellbase() → 1 DB read
        header = indexer.block_header(&header.parent_hash())?;  // 1 DB read
    }
    // 3 more DB reads per outer iteration (lines 335–339)
}
```

Each inner iteration performs 2 DB reads (`cellbase` + `block_header`), and each outer iteration adds 3 more. With `period = 10` and `epoch_length = 1800`, a single uncached `Started` period costs ~36,000 DB reads.

**Outer backward walk:**

Lines 291–305 contain an unbounded `loop` that walks backward through the chain `period` epochs at a time, calling `ancestor_epoch` at each step. `ancestor_epoch` (lines 109–115) is itself an unbounded `while` loop performing 3 DB reads per epoch step:

```rust
while epoch_ext.number() > target {
    let last_block_header_in_previous_epoch =
        self.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;  // 1 DB read
    let previous_epoch_index =
        self.block_epoch_index(&last_block_header_in_previous_epoch.hash())?;  // 1 DB read
    epoch_ext = self.epoch_ext(&previous_epoch_index)?;  // 1 DB read
}
```

There is no upper bound on the number of `to_compute` entries pushed in the outer loop.

**Cache invalidation on reorg:**

The `Cache` is a file-based `cacache` store (lines 154–164, 212–224) keyed by `epoch_ext.last_block_hash_in_previous_epoch()`. The cache persists across node restarts (stored at `data_dir/softfork/<pos>`), so cold-cache on restart is not the primary concern. However, after any reorg, the reorged epochs have different `last_block_hash_in_previous_epoch` values, causing all cache misses on the new chain and forcing full recomputation from epoch 0 to the current tip.

**Reachable paths (confirmed):**

1. `get_deployments_info` RPC — `rpc/src/module/stats.rs` lines 153–185 call `versionbits_state` → `get_state` for every deployment. Any unprivileged caller can trigger this.
2. `get_block_template` miner path — `tx-pool/src/block_assembler/mod.rs` line 503 calls `snapshot.compute_versionbits(tip)` → `get_state` for every active deployment on every block template build.

**`condition` function:** Lines 440–455 read the cellbase for every block in the inner loop. A commented-out alternative at lines 457–460 checks the block version field directly without a DB read, but is not used.

**No existing guard:** There is no upper bound on `to_compute` entries, no limit on DB reads in the inner loop, and no timeout or circuit-breaker on the computation.

## Impact Explanation

**Medium — Suboptimal implementation of CKB state storage mechanism.**

After any reorg (a normal operational event), all versionbits cache entries for the reorged chain are silently invalidated. The next call to `get_deployments_info` or `get_block_template` triggers full O(epoch_number × epoch_length) recomputation. On mainnet with ~1800 blocks/epoch and epoch number ~1000, this is approximately 1.8 million RocksDB reads per deployment per cold call. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles this cost. The RPC call blocks for the duration of this computation, causing significant latency and block template generation delay. The node does not crash and the computation is self-healing (cache warms after first call), so this does not reach the High threshold.

## Likelihood Explanation

Reorgs are normal operational events on CKB mainnet and require no attacker resources to occur naturally. A miner calling `get_block_template` immediately after a reorg (standard mining behavior) triggers the full cold-cache computation. An unprivileged user calling `get_deployments_info` after a reorg also triggers it. On a chain with frequent small reorgs or during a large reorg event, this can be triggered repeatedly. No special privileges or exotic conditions are required.

## Recommendation

1. **Reorg-aware cache invalidation**: Key the cache on both `last_block_hash_in_previous_epoch` and epoch number, so that after a reorg only the affected epochs are invalidated rather than the entire cache.
2. **Precompute eagerly on chain tip advance**: Update the versionbits cache when a new block is committed to the chain (in the chain service), so `get_state` always finds a warm cache at query time.
3. **Bound the backward walk**: Add a hard upper bound on the number of `to_compute` entries and surface an error if exceeded, rather than silently performing unbounded work.
4. **Avoid per-block cellbase reads in `condition`**: Use the commented-out alternative at lines 457–460 that checks the block version field directly, eliminating one DB read per block in the inner loop.

## Proof of Concept

1. Run a CKB node on mainnet or a long-running testnet with a softfork deployment in `Started` state (e.g., `LightClient`).
2. Trigger a reorg of depth ≥ 1 epoch (e.g., via a competing chain tip).
3. Immediately call `get_deployments_info` via RPC or trigger `get_block_template`.
4. Observe: `get_state` walks backward from the current epoch to epoch 0, performing `period × epoch_length` DB reads for each uncached period in `Started` state. With `period = 10` and `epoch_length = 1800`, each period costs ~36,000 DB reads; across 100 periods this is ~3.6 million reads. The RPC call blocks for the duration of this computation.
5. A unit test can be constructed by mocking `VersionbitsIndexer` with a counter on `block_header` and `cellbase` calls, setting `period = 10`, `epoch_length = 1800`, `epoch_number = 1000`, and asserting the call count exceeds 1 million on a cold cache. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** spec/src/versionbits/mod.rs (L291-305)
```rust
        let mut state = loop {
            let epoch_index = epoch_ext.last_block_hash_in_previous_epoch();
            if let Some(value) = cache.get(&epoch_index) {
                break value;
            } else {
                if epoch_ext.is_genesis() || epoch_ext.number() < start {
                    cache.insert(&epoch_index, ThresholdState::Defined);
                    break ThresholdState::Defined;
                }
                let next_epoch_ext = indexer
                    .ancestor_epoch(&epoch_index, epoch_ext.number().saturating_sub(period))?;
                to_compute.push(epoch_ext);
                epoch_ext = next_epoch_ext;
            }
        };
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

**File:** spec/src/versionbits/mod.rs (L440-455)
```rust
    fn condition<I: VersionbitsIndexer>(&self, header: &HeaderView, indexer: &I) -> bool {
        if let Some(cellbase) = indexer.cellbase(&header.hash())
            && let Some(witness) = cellbase.witnesses().get(0)
            && let Ok(reader) = CellbaseWitnessReader::from_slice(&witness.raw_data())
        {
            let message = reader.message().to_entity();
            if message.len() >= 4
                && let Ok(raw) = message.raw_data()[..4].try_into()
            {
                let version = u32::from_le_bytes(raw);
                return ((version & VERSIONBITS_TOP_MASK) == VERSIONBITS_TOP_BITS)
                    && (version & self.mask()) != 0;
            }
        }
        false
    }
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
