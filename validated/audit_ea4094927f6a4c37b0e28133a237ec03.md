### Title
Unbounded Nested Loop in `VersionbitsConditionChecker::get_state` Causes O(epoch_number × epoch_length) DB Reads on Cold Cache — (`File: spec/src/versionbits/mod.rs`)

---

### Summary

`VersionbitsConditionChecker::get_state` in `spec/src/versionbits/mod.rs` contains a nested loop that, on a cold cache (node restart or deep reorg), iterates over every block in every uncached signalling period. Each iteration performs multiple RocksDB reads. The total work scales as O(epoch\_number × epoch\_length), which on mainnet can reach ~1.8 million DB reads per call. This function is reachable by any unprivileged RPC caller via `get_blockchain_info` and by any miner via `get_block_template`.

---

### Finding Description

`get_state` implements the RFC-0043 versionbits state machine. When the deployment is in `ThresholdState::Started`, it must count signalling blocks across `period` epochs. The inner counting loop is:

```rust
for _ in 0..period {
    let current_epoch_length = current_epoch_ext.length();
    total += current_epoch_length;
    for _ in 0..current_epoch_length {
        if self.condition(&header, indexer) { count += 1; }
        header = indexer.block_header(&header.parent_hash())?;  // DB read
    }
    // 2 more DB reads per outer iteration
    ...
}
``` [1](#0-0) 

Each inner iteration calls `indexer.block_header()` (one RocksDB read) and `self.condition()` which calls `indexer.cellbase()` (another RocksDB read). With `period` epochs and up to `MAX_EPOCH_LENGTH` blocks per epoch, this is `period × epoch_length` DB reads **per uncached period**.

Before reaching this inner loop, an outer `loop` walks backward through the chain `period` epochs at a time, calling `ancestor_epoch` at each step:

```rust
let mut state = loop {
    let epoch_index = epoch_ext.last_block_hash_in_previous_epoch();
    if let Some(value) = cache.get(&epoch_index) { break value; }
    else {
        let next_epoch_ext = indexer
            .ancestor_epoch(&epoch_index, epoch_ext.number().saturating_sub(period))?;
        to_compute.push(epoch_ext);
        epoch_ext = next_epoch_ext;
    }
};
``` [2](#0-1) 

`ancestor_epoch` itself is an unbounded `while` loop that walks back one epoch at a time, performing 3 DB reads per step:

```rust
while epoch_ext.number() > target {
    let last_block_header_in_previous_epoch =
        self.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;
    let previous_epoch_index =
        self.block_epoch_index(&last_block_header_in_previous_epoch.hash())?;
    epoch_ext = self.epoch_ext(&previous_epoch_index)?;
}
``` [3](#0-2) 

The cache (`Cache`) is a file-based `cacache` store keyed by `epoch_ext.last_block_hash_in_previous_epoch()`. It is cold after every node restart and after any reorg (since reorged epochs have different `last_block_hash_in_previous_epoch` values, causing all cache misses on the new chain). [4](#0-3) 

---

### Impact Explanation

**Worst-case cost (cold cache):**

- Backward walk: O(`epoch_number`) DB reads (the outer `loop` + `ancestor_epoch` calls)
- State computation: O(`(epoch_number / period) × period × epoch_length`) = O(`epoch_number × epoch_length`) DB reads

On mainnet with ~1800 blocks/epoch and epoch number ~1000, a single cold `get_state` call performs approximately **1.8 million RocksDB reads**. With two deployments (`Testdummy`, `LightClient`), `compute_versionbits` doubles this. [5](#0-4) 

This can stall the node's RPC thread and block template generation for seconds to tens of seconds, causing:
- Mining interruption (miner cannot get a new template)
- RPC unresponsiveness for all callers sharing the same thread pool
- Repeated triggering after every restart or reorg

---

### Likelihood Explanation

The function is called on two reachable paths:

**1. RPC path** — `rpc/src/module/stats.rs` calls `versionbits_state` (which calls `get_state`) to serve `get_blockchain_info`. Any unprivileged RPC caller can trigger this. After a node restart, the first call is maximally expensive. [6](#0-5) 

**2. Miner/block-template path** — `BlockAssembler::build_cellbase_witness` calls `snapshot.compute_versionbits(tip)` on every block template build:

```rust
if let Some(version) = snapshot.compute_versionbits(tip) {
    message.extend_from_slice(&version.to_le_bytes());
    ...
}
``` [7](#0-6) 

`compute_versionbits` calls `get_state` for every active deployment: [8](#0-7) 

A miner calling `get_block_template` after a restart or reorg triggers the full cold-cache computation. This is a normal operational event, not an exotic attack.

---

### Recommendation

1. **Bound the backward walk**: Store the epoch number of the last cached state so `get_state` can skip directly to it without walking back one period at a time.
2. **Persist cache across restarts correctly**: Ensure the `cacache` entries survive restarts and are invalidated only on reorg, not globally.
3. **Precompute on chain tip advance**: Update the versionbits cache eagerly when a new block is committed to the chain, so `get_state` always finds a warm cache at query time.
4. **Add a hard upper bound** on the number of `to_compute` entries (analogous to the `for i in range(500)` pattern in the original report) and surface an error if exceeded, rather than silently performing unbounded work.

---

### Proof of Concept

1. Start a CKB node on mainnet (or a long-running testnet) with a softfork deployment in `Started` state.
2. Stop the node (clears in-memory state; `cacache` on disk may or may not be warm).
3. Restart the node.
4. Immediately call `get_blockchain_info` via RPC (or trigger `get_block_template`).
5. Observe: `get_state` walks backward from the current epoch (~1000+) to epoch 0, performing `period × epoch_length` DB reads for each uncached period in `Started` state. With `period = 10` and `epoch_length = 1800`, each period costs ~18,000 DB reads; 100 periods costs ~1.8 million reads. The RPC call blocks for the duration of this computation, stalling the node's response to all concurrent callers. [9](#0-8)

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

**File:** spec/src/versionbits/mod.rs (L154-165)
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

**File:** spec/src/versionbits/mod.rs (L265-363)
```rust
    fn get_state<I: VersionbitsIndexer>(
        &self,
        header: &HeaderView,
        cache: &Cache,
        indexer: &I,
    ) -> Option<ThresholdState> {
        let active_mode = self.active_mode();
        let start = self.start();
        let timeout = self.timeout();
        let period = self.period();
        let min_activation_epoch = self.min_activation_epoch();

        if active_mode == ActiveMode::Always {
            return Some(ThresholdState::Active);
        }

        if active_mode == ActiveMode::Never {
            return Some(ThresholdState::Failed);
        }

        let start_index = indexer.block_epoch_index(&header.hash())?;
        let epoch_number = header.epoch().number();
        let target = epoch_number.saturating_sub((epoch_number + 1) % period);

        let mut epoch_ext = indexer.ancestor_epoch(&start_index, target)?;
        let mut to_compute = Vec::new();
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

        while let Some(epoch_ext) = to_compute.pop() {
            let mut next_state = state;

            match state {
                ThresholdState::Defined => {
                    if epoch_ext.number() >= start {
                        next_state = ThresholdState::Started;
                    }
                }
                ThresholdState::Started => {
                    // We need to count
                    debug_assert!(epoch_ext.number() + 1 >= period);

                    let mut count = 0;
                    let mut total = 0;
                    let mut header =
                        indexer.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;

                    let mut current_epoch_ext = epoch_ext.clone();
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

                    let threshold_number = threshold_number(total, self.threshold())?;
                    if count >= threshold_number {
                        next_state = ThresholdState::LockedIn;
                    } else if epoch_ext.number() >= timeout {
                        next_state = ThresholdState::Failed;
                    }
                }
                ThresholdState::LockedIn => {
                    if epoch_ext.number() >= min_activation_epoch {
                        next_state = ThresholdState::Active;
                    }
                }
                ThresholdState::Failed | ThresholdState::Active => {
                    // Nothing happens, these are terminal states.
                }
            }
            state = next_state;
            cache.insert(&epoch_ext.last_block_hash_in_previous_epoch(), state);
        }

        Some(state)
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

**File:** util/snapshot/src/lib.rs (L172-178)
```rust
    /// Returns specified softfork active or not
    pub fn versionbits_active(&self, pos: DeploymentPos) -> bool {
        self.consensus
            .versionbits_state(pos, &self.tip_header, self)
            .map(|state| state == ThresholdState::Active)
            .unwrap_or(false)
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L503-506)
```rust
        if let Some(version) = snapshot.compute_versionbits(tip) {
            message.extend_from_slice(&version.to_le_bytes());
            message.extend_from_slice(b" ");
        }
```
