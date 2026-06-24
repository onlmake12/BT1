Audit Report

## Title
Unbounded Nested Loop in `get_state` Causes O(epoch_number × epoch_length) DB Reads on Cold Cache — (`File: spec/src/versionbits/mod.rs`)

## Summary
`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a nested loop that, when the on-disk `cacache` is cold (first run or after a reorg), iterates over every block in every uncached signalling period, performing multiple RocksDB reads per block. Total work scales as O(epoch_number × epoch_length). The function is reachable by any unprivileged caller via the `get_deployments_info` RPC and by any miner via `get_block_template`, causing temporary but significant RPC and block-template stalls.

## Finding Description
**Root cause — inner counting loop** (`spec/src/versionbits/mod.rs`, lines 326–340):

When a period's state is `ThresholdState::Started` and its result is not cached, `get_state` runs a nested loop: the outer iterates `period` times (one per epoch in the signalling window); the inner iterates `current_epoch_length` times (one per block). Each inner iteration calls `self.condition(&header, indexer)` — which calls `indexer.cellbase()` (one DB read) — and then `indexer.block_header(&header.parent_hash())` (one more DB read). This is `2 × period × epoch_length` DB reads per uncached `to_compute` entry in `Started` state.

**Root cause — outer backward walk** (`spec/src/versionbits/mod.rs`, lines 291–305):

Before the counting loop, an outer `loop` walks backward `period` epochs at a time by calling `ancestor_epoch`. `ancestor_epoch` itself is an unbounded `while` loop (lines 109–115) that walks back one epoch per iteration, performing 3 DB reads per step (`block_header` + `block_epoch_index` + `epoch_ext`). Each outer-loop iteration therefore costs `3 × period` DB reads, and the outer loop runs `epoch_number / period` times until a cached entry or genesis is found.

**Combined worst-case cost (cold cache):**
- Outer walk: `(epoch_number / period) × 3 × period` = `3 × epoch_number` DB reads
- Inner counting: `(epoch_number / period) × 2 × period × epoch_length` = `2 × epoch_number × epoch_length` DB reads
- Total: O(epoch_number × epoch_length)

On mainnet (~1000 epochs, ~1800 blocks/epoch, `period = 10`): approximately **1.8 million DB reads per `get_state` call**. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles this.

**Why existing guards are insufficient:**

The `Cache` is a file-based `cacache` store keyed by `epoch_ext.last_block_hash_in_previous_epoch()`. It persists across normal restarts (the claim that it is cold after every restart is incorrect — the disk cache survives restarts). However, after any reorg, the `last_block_hash_in_previous_epoch` values on the new chain differ from those on the old chain, causing complete cache misses for all reorged epochs. On first run there is also no cache. There is no upper bound on the size of `to_compute`, no depth limit on `ancestor_epoch`, and no circuit-breaker on the inner counting loop.

**Reachable paths:**

1. `get_deployments_info` RPC (`rpc/src/module/stats.rs`, lines 153–185) calls `consensus.versionbits_state(pos, tip_header, snapshot)` for every deployment — directly invoking `get_state`. Note: `get_blockchain_info` does **not** call `versionbits_state`; the claim on this point is inaccurate.
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
5. Confirm by adding instrumentation to count `block_header` and `cellbase` calls inside `get_state` and verifying the count matches the O(epoch_number × epoch_length) prediction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/block_assembler/mod.rs (L503-506)
```rust
        if let Some(version) = snapshot.compute_versionbits(tip) {
            message.extend_from_slice(&version.to_le_bytes());
            message.extend_from_slice(b" ");
        }
```

**File:** spec/src/consensus.rs (L1015-1032)
```rust
    pub fn compute_versionbits<I: VersionbitsIndexer>(
        &self,
        parent: &HeaderView,
        indexer: &I,
    ) -> Option<Version> {
        let mut version = versionbits::VERSIONBITS_TOP_BITS;
        for pos in self.deployments.keys() {
            let versionbits = Versionbits::new(*pos, self);
            let cache = self.versionbits_caches.cache(pos)?;
            let state = versionbits.get_state(parent, cache, indexer)?;
            if state == versionbits::ThresholdState::LockedIn
                || state == versionbits::ThresholdState::Started
            {
                version |= versionbits.mask();
            }
        }
        Some(version)
    }
```
