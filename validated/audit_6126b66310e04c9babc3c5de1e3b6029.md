### Title
Unchecked `Option` Return Values in `RewardCalculator` Cause Node Panic on Store Lookup Failure — (File: `util/reward-calculator/src/lib.rs`)

---

### Summary

`RewardCalculator::block_reward_to_finalize` and `block_reward_for_target` call `get_block_hash()` and `get_cellbase()` — both of which return `Option<T>` — and immediately chain `.expect()` on the result without propagating a proper error. If the store returns `None` (e.g., due to a chain-index inconsistency, a reorg edge case, or a missing cellbase), the node panics unconditionally. Both functions are reachable via externally-callable RPC endpoints (`get_block_template` and `get_block_economic_state`), making this a realistic node-crash (DoS) vector for an unprivileged RPC caller or miner.

---

### Finding Description

**Root cause — `block_reward_to_finalize` (lines 54–58):**

```rust
let target = self
    .store
    .get_block_hash(target_number)
    .and_then(|hash| self.store.get_block_header(&hash))
    .expect("block hash checked before involving get_ancestor");
```

The comment "block hash checked before involving get_ancestor" is misleading: there is **no guard** in this function that verifies `target_number` is actually present in the store before calling `.expect()`. If `get_block_hash` returns `None`, the thread panics. [1](#0-0) 

**Root cause — `block_reward_for_target` (lines 72–76):**

```rust
let parent = self
    .store
    .get_block_hash(finalization_parent_number)
    .and_then(|hash| self.store.get_block_header(&hash))
    .expect("block hash checked before involving get_ancestor");
```

Same pattern. `finalization_parent_number` is derived from `target.number() + finalization_delay_length - 1`. The caller (`get_block_economic_state`) only checks `tip_number >= finalized_at_number`; it does **not** verify that the block at `finalization_parent_number` is actually indexed in the store before delegating to this function. [2](#0-1) 

**Root cause — `block_reward_internal` (lines 90–101):**

```rust
let target_lock = CellbaseWitness::from_slice(
    &self
        .store
        .get_cellbase(&target.hash())
        .expect("target cellbase exist")
        .witnesses()
        .get(0)
        .expect("target witness exist")
        .raw_data(),
)
.expect("cellbase loaded from store should has non-empty witness")
.lock();
```

Three chained `.expect()` calls on `Option`/`Result` values. The function's own doc comment acknowledges this: *"Panics if the target cellbase does not exist or if the target witness does not exist, or if the cellbase loaded from store has an empty witness."* This panic is not caught or converted to a `DaoError` — it propagates as a thread panic. [3](#0-2) 

**Root cause — `proposal_reward` (lines 247–250):**

```rust
let previous_ids = store
    .get_block_hash(competing_proposal_start)
    .map(|hash| self.get_proposal_ids_by_hash(&hash))
    .expect("finalize target exist");
```

`competing_proposal_start` is computed dynamically as `max(index.number().saturating_sub(proposal_window.farthest()), 1)`. If the block at that number is absent from the store, the node panics. [4](#0-3) 

**Caller chain — `build_cellbase` (block assembler):**

`build_cellbase` calls `block_reward_to_finalize` unconditionally when `candidate_number > finalization_delay_length`. This is invoked on every `get_block_template` RPC call. [5](#0-4) 

**Caller chain — `get_block_economic_state` (RPC):**

The RPC checks `tip_number >= finalized_at_number` but then delegates to `block_reward_for_target` which re-derives `finalization_parent_number` and calls `.expect()` without a direct store-presence guard. [6](#0-5) 

---

### Impact Explanation

If any of the `.expect()` calls in `RewardCalculator` encounter `None` — due to a chain-index inconsistency, a partially-applied reorg, a missing cellbase, or a bug in the store layer — the Rust thread panics. Because `build_cellbase` is called synchronously inside the block-assembler's async task, and `block_reward_for_target` is called inside the RPC handler, an unrecovered panic crashes the relevant async task or the entire node process (depending on the panic handler configuration). This constitutes a **remote DoS**: an unprivileged RPC caller or miner can trigger the panic by repeatedly calling `get_block_template` or `get_block_economic_state` at a moment when the store is in an inconsistent state.

---

### Likelihood Explanation

The store inconsistency required to trigger the panic is not the common case, but it is realistic in the following scenarios:

- A reorg is in progress and the snapshot used by the RPC handler captures a tip that references blocks not yet fully written to the column families queried by `get_block_hash` / `get_cellbase`.
- A background migration or freezer operation (`freeze` in `shared/src/shared.rs`) moves blocks to cold storage between the tip-number check and the actual store lookup.
- A bug in block attachment/detachment leaves the epoch index or cellbase column inconsistent with the block-number index.

The entry point (`get_block_template` and `get_block_economic_state`) is accessible to any local or remote RPC caller without authentication. [7](#0-6) 

---

### Recommendation

Replace every `.expect()` on a store-lookup `Option` inside `RewardCalculator` with `ok_or(DaoError::...)` so that the error is propagated as a `Result` to the caller. Both `block_reward_to_finalize` and `block_reward_for_target` already return `Result<_, DaoError>`, so the propagation path exists. Example:

```rust
// Before
let target = self.store
    .get_block_hash(target_number)
    .and_then(|hash| self.store.get_block_header(&hash))
    .expect("block hash checked before involving get_ancestor");

// After
let target = self.store
    .get_block_hash(target_number)
    .and_then(|hash| self.store.get_block_header(&hash))
    .ok_or(DaoError::InvalidHeader)?;
```

Apply the same pattern to `get_cellbase`, `get_block_ext`, and `get_block_hash` inside `proposal_reward`. This converts a hard panic into a recoverable `Err` that the RPC layer can translate into a JSON-RPC error response, following the "fail early and loudly" principle without crashing the node.

---

### Proof of Concept

1. Start a CKB node on a dev chain.
2. Mine enough blocks so that `tip_number >= finalization_delay_length` (so `build_cellbase` enters the reward-calculation branch).
3. Simulate a store inconsistency by deleting or corrupting the `COLUMN_INDEX` entry for `target_number` using a RocksDB tool while the node is running (or reproduce via a crafted integration test that calls `get_block_template` immediately after a truncate/reorg that leaves the index partially updated).
4. Call `get_block_template` via RPC.
5. Observe the node panic with message `"block hash checked before involving get_ancestor"` or `"target cellbase exist"`.

The same panic is reachable via `get_block_economic_state` by supplying a `block_hash` for a block whose `finalization_parent_number` block is absent from the store at the moment the snapshot is taken.

### Citations

**File:** util/reward-calculator/src/lib.rs (L54-58)
```rust
        let target = self
            .store
            .get_block_hash(target_number)
            .and_then(|hash| self.store.get_block_header(&hash))
            .expect("block hash checked before involving get_ancestor");
```

**File:** util/reward-calculator/src/lib.rs (L72-76)
```rust
        let parent = self
            .store
            .get_block_hash(finalization_parent_number)
            .and_then(|hash| self.store.get_block_header(&hash))
            .expect("block hash checked before involving get_ancestor");
```

**File:** util/reward-calculator/src/lib.rs (L85-101)
```rust
    fn block_reward_internal(
        &self,
        target: &HeaderView,
        parent: &HeaderView,
    ) -> Result<(Script, BlockReward), DaoError> {
        let target_lock = CellbaseWitness::from_slice(
            &self
                .store
                .get_cellbase(&target.hash())
                .expect("target cellbase exist")
                .witnesses()
                .get(0)
                .expect("target witness exist")
                .raw_data(),
        )
        .expect("cellbase loaded from store should has non-empty witness")
        .lock();
```

**File:** util/reward-calculator/src/lib.rs (L247-250)
```rust
            let previous_ids = store
                .get_block_hash(competing_proposal_start)
                .map(|hash| self.get_proposal_ids_by_hash(&hash))
                .expect("finalize target exist");
```

**File:** tx-pool/src/block_assembler/mod.rs (L536-559)
```rust
        let tx = {
            let (target_lock, block_reward) = block_in_place(|| {
                RewardCalculator::new(snapshot.consensus(), snapshot).block_reward_to_finalize(tip)
            })?;
            let input = CellInput::new_cellbase_input(candidate_number);
            let output = CellOutput::new_builder()
                .capacity(block_reward.total)
                .lock(target_lock)
                .build();

            let witness = cellbase_witness.as_bytes();
            let no_finalization_target =
                candidate_number <= snapshot.consensus().finalization_delay_length();
            let tx_builder = TransactionBuilder::default().input(input).witness(witness);
            let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;
            if no_finalization_target || insufficient_reward_to_create_cell {
                tx_builder.build()
            } else {
                tx_builder
                    .output(output)
                    .output_data(Bytes::default())
                    .build()
            }
        };
```

**File:** rpc/src/module/chain.rs (L1902-1913)
```rust
        Ok(snapshot.get_block_header(&block_hash).and_then(|header| {
            RewardCalculator::new(snapshot.consensus(), snapshot.as_ref())
                .block_reward_for_target(&header)
                .ok()
                .map(|(_, block_reward)| core::BlockEconomicState {
                    issuance,
                    miner_reward: block_reward.into(),
                    txs_fee,
                    finalized_at,
                })
                .map(Into::into)
        }))
```

**File:** shared/src/shared.rs (L153-202)
```rust
    fn freeze(&self) -> Result<(), Error> {
        let freezer = self.store.freezer().expect("freezer inited");
        let snapshot = self.snapshot();
        let current_epoch = snapshot.epoch_ext().number();

        if self.is_initial_block_download() {
            ckb_logger::trace!("is_initial_block_download freeze skip");
            return Ok(());
        }

        if current_epoch <= THRESHOLD_EPOCH {
            ckb_logger::trace!("Freezer idles");
            return Ok(());
        }

        let limit_block_hash = snapshot
            .get_epoch_index(current_epoch + 1 - THRESHOLD_EPOCH)
            .and_then(|index| snapshot.get_epoch_ext(&index))
            .expect("get_epoch_ext")
            .last_block_hash_in_previous_epoch();

        let frozen_number = freezer.number();

        let threshold = cmp::min(
            snapshot
                .get_block_number(&limit_block_hash)
                .expect("get_block_number"),
            frozen_number + MAX_FREEZE_LIMIT,
        );

        ckb_logger::trace!(
            "Freezer current_epoch {} number {} threshold {}",
            current_epoch,
            frozen_number,
            threshold
        );

        let store = self.store();
        let get_unfrozen_block = |number: BlockNumber| {
            store
                .get_block_hash(number)
                .and_then(|hash| store.get_unfrozen_block(&hash))
        };

        let ret = freezer.freeze(threshold, get_unfrozen_block)?;

        let stopped = freezer.stopped.load(Ordering::SeqCst);

        // Wipe out frozen data
        self.wipe_out_frozen_data(&snapshot, ret, stopped)?;
```
