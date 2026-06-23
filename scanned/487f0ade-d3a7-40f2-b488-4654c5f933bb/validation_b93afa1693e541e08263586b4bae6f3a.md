### Title
Panic in Multi-Step Block Verification Flow Caught by `catch_unwind` Leaves Node in Inconsistent In-Memory State Without Error Recovery — (`chain/src/verify.rs`)

---

### Summary

`ConsumeUnverifiedBlocks::start()` wraps the entire `consume_unverified_blocks` call in `catch_unwind(AssertUnwindSafe(...))`. When `verify_block` panics (e.g., from a divide-by-zero in DAO field computation or a `to_rational()` call on a zero-length epoch), the panic is caught but the structured error-recovery branch inside `consume_unverified_blocks` is never executed. This leaves the node's in-memory state inconsistent with the database: the unverified tip is not reset, the block is not marked `BLOCK_INVALID`, and the unverified block data is not cleaned up.

---

### Finding Description

**Step 1 — The multi-step flow in `verify_block`:**

`verify_block` performs a sequence of state-mutating steps:

1. Writes to a `RocksDBTransaction` (`db_txn`): epoch index, epoch ext, fork rollback, `reconcile_main_chain` (which calls DAO field computation and script verification).
2. Commits the DB transaction atomically (`db_txn.commit()`).
3. Updates in-memory shared state: `update_proposal_table`, `new_snapshot`, `store_snapshot`, `update_tx_pool_for_reorg`. [1](#0-0) 

**Step 2 — The `catch_unwind` handler:**

The outer loop catches any panic from `consume_unverified_blocks` but only removes the block from `is_pending_verify`: [2](#0-1) 

**Step 3 — The error-recovery code that is skipped on panic:**

When `verify_block` returns `Err(...)`, the `Err` branch in `consume_unverified_blocks` executes three critical recovery actions: [3](#0-2) 

When `verify_block` **panics** instead of returning `Err`, the panic unwinds through `consume_unverified_blocks` before any of these recovery actions execute. The `catch_unwind` handler at the outer level only removes the block from `is_pending_verify` — none of the three recovery actions run.

**Step 4 — Concrete panic triggers in the verification path:**

**(a) Divide-by-zero in DAO field computation:**

`dao_field_with_current_epoch` performs integer division by `parent_c` (total system capacity extracted from the parent block's DAO field) without a zero-check: [4](#0-3) [5](#0-4) 

Similarly in `secondary_block_reward`: [6](#0-5) 

And in `calculate_maximum_withdraw`, division by `deposit_ar` (the accumulation rate at deposit time) with no zero-check: [7](#0-6) 

**(b) Division-by-zero panic in `EpochNumberWithFraction::to_rational()`:**

The function is explicitly documented to panic when `length == 0` for non-genesis epochs: [8](#0-7) 

This is called during transaction verification in `MaturityVerifier::verify()`: [9](#0-8) 

And in `SinceVerifier::verify_relative_lock()` on `info.block_epoch` (the stored epoch of the input cell's containing block): [10](#0-9) 

`RationalU256::new()` enforces the panic explicitly: [11](#0-10) 

The `from_full_value_unchecked` constructor (used when deserializing block headers from the wire) does **not** normalize the epoch, leaving a zero-length epoch intact in stored `TransactionInfo`: [12](#0-11) [13](#0-12) 

---

### Impact Explanation

When `verify_block` panics mid-execution:

| Recovery action | Normal `Err` path | Panic path |
|---|---|---|
| `shared.set_unverified_tip()` reset to DB tip | ✅ executed | ❌ skipped |
| `delete_unverified_block()` | ✅ executed | ❌ skipped |
| `insert_block_status(BLOCK_INVALID)` | ✅ executed | ❌ skipped |

The result is that the node's in-memory `unverified_tip` continues to point to the panicked block, the block is not marked invalid (so it can be re-submitted and re-panicked), and the unverified block data persists in the DB. If the panic occurs after `db_txn.commit()` (line 359) but before `store_snapshot()` (line 383), the committed DB state and the in-memory snapshot diverge, causing incorrect fork-choice decisions for all subsequent blocks. [14](#0-13) 

---

### Likelihood Explanation

- Triggering `to_rational()` via a stored block with a zero-length epoch requires a block with valid PoW, making it computationally expensive but not impossible for a miner or a node that accepted a crafted block.
- Triggering the DAO divide-by-zero requires `parent_c == 0`, which is practically impossible under normal genesis configuration.
- Any other unexpected panic (memory pressure, stack overflow in deep recursion, future code changes) would also trigger this structural gap.
- The use of `AssertUnwindSafe` bypasses Rust's unwind-safety type system, meaning the shared mutable state (`Shared`) is not guaranteed to be in a valid state after the panic is caught. [2](#0-1) 

---

### Recommendation

1. The `catch_unwind` handler should replicate the `Err` branch recovery: reset `unverified_tip` to the actual DB tip, delete the unverified block data, and mark the block as `BLOCK_INVALID`.
2. Replace bare integer division in `dao_field_with_current_epoch` and `calculate_maximum_withdraw` with explicit zero-checks that return `DaoError::ZeroC` (which already exists in the error enum) instead of panicking.
3. Validate that `EpochNumberWithFraction` deserialized from block headers has a non-zero length before storing `TransactionInfo`, or call `.normalize()` before any `to_rational()` invocation on stored epoch values. [15](#0-14) 

---

### Proof of Concept

1. A block relayer sends a syntactically valid block (with valid PoW) whose header encodes a non-genesis `EpochNumberWithFraction` with `length == 0` using `from_full_value_unchecked`.
2. The node stores the block and its transactions. `TransactionInfo` for cells in this block records `block_epoch` with `length == 0`.
3. A subsequent transaction spends one of those cells with a non-zero relative `since` (epoch metric). `SinceVerifier::verify_relative_lock()` calls `info.block_epoch.to_rational()`, which calls `RationalU256::new(index.into(), 0u64.into())`, triggering the documented panic: `"denominator == 0"`.
4. The panic propagates through `reconcile_main_chain` → `verify_block` → `consume_unverified_blocks` and is caught by `catch_unwind` in `ConsumeUnverifiedBlocks::start()`.
5. The handler logs the panic and removes the block from `is_pending_verify`. The unverified tip is **not** reset, the block is **not** marked `BLOCK_INVALID`, and the unverified block data is **not** deleted.
6. The node's in-memory state is now inconsistent: subsequent blocks are evaluated against a stale snapshot, and the panicking block can be re-submitted indefinitely without being rejected.

### Citations

**File:** chain/src/verify.rs (L86-96)
```rust
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
```

**File:** chain/src/verify.rs (L153-190)
```rust
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
```

**File:** chain/src/verify.rs (L318-383)
```rust
        let db_txn = Arc::new(self.shared.store().begin_transaction());
        let txn_snapshot = db_txn.get_snapshot();
        let _snapshot_tip_hash = db_txn.get_update_for_tip_hash(&txn_snapshot);

        db_txn.insert_block_epoch_index(
            &block.header().hash(),
            &epoch.last_block_hash_in_previous_epoch(),
        )?;
        if new_epoch {
            db_txn.insert_epoch_ext(&epoch.last_block_hash_in_previous_epoch(), &epoch)?;
        }

        let in_ibd = self.shared.is_initial_block_download();

        if new_best_block {
            info!(
                "[verify block] new best block found: {} => {:#x}, difficulty diff = {:#x}, unverified_tip: {}",
                block.header().number(),
                block.header().hash(),
                &cannon_total_difficulty - &current_total_difficulty,
                self.shared.get_unverified_tip().number(),
            );
            self.find_fork(&mut fork, current_tip_header.number(), block, ext);
            self.rollback(&fork, &db_txn)?;

            // update and verify chain root
            // MUST update index before reconcile_main_chain
            let begin_reconcile_main_chain = std::time::Instant::now();
            self.reconcile_main_chain(Arc::clone(&db_txn), &mut fork, switch)?;
            trace!(
                "reconcile_main_chain cost {:?}",
                begin_reconcile_main_chain.elapsed()
            );

            db_txn.insert_tip_header(&block.header())?;
            if new_epoch || fork.has_detached() {
                db_txn.insert_current_epoch_ext(&epoch)?;
            }
        } else {
            db_txn.insert_block_ext(&block.header().hash(), &ext)?;
        }
        db_txn.commit()?;

        if new_best_block {
            let tip_header = block.header();
            info!(
                "block: {}, hash: {:#x}, epoch: {:#}, total_diff: {:#x}, txs: {}, proposals: {}",
                tip_header.number(),
                tip_header.hash(),
                tip_header.epoch(),
                cannon_total_difficulty,
                block.transactions().len(),
                block.data().proposals().len()
            );

            self.update_proposal_table(&fork);
            let (detached_proposal_id, new_proposals) = self
                .proposal_table
                .finalize(origin_proposals, tip_header.number());
            fork.detached_proposal_id = detached_proposal_id;

            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));
```

**File:** util/dao/src/lib.rs (L152-154)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
```

**File:** util/dao/src/lib.rs (L202-203)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
```

**File:** util/dao/src/lib.rs (L242-243)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
```

**File:** util/dao/src/lib.rs (L256-257)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
```

**File:** util/types/src/core/extras.rs (L472-480)
```rust
    /// Converts from an unsigned 64 bits number without checks.
    ///
    /// # Notice
    ///
    /// The `EpochNumberWithFraction` constructed by this method has a potential risk that when
    /// call `self.to_rational()` may lead to a panic if the user specifies a zero epoch length.
    pub fn from_full_value_unchecked(value: u64) -> Self {
        Self(value)
    }
```

**File:** util/types/src/core/extras.rs (L491-502)
```rust
    /// Converts the epoch to an unsigned 256 bits rational.
    ///
    /// # Panics
    ///
    /// Only genesis epoch's length could be zero, otherwise causes a division-by-zero panic.
    pub fn to_rational(self) -> RationalU256 {
        if self.0 == 0 {
            RationalU256::zero()
        } else {
            RationalU256::new(self.index().into(), self.length().into()) + U256::from(self.number())
        }
    }
```

**File:** verification/src/transaction_verifier.rs (L389-393)
```rust
                        let threshold =
                            self.cellbase_maturity.to_rational() + info.block_epoch.to_rational();
                        let current = self.epoch.to_rational();
                        current < threshold
                    }
```

**File:** verification/src/transaction_verifier.rs (L581-584)
```rust
            //0b0010_0000
            0x2000_0000_0000_0000 => Some(SinceMetric::EpochNumberWithFraction(
                EpochNumberWithFraction::from_full_value_unchecked(value),
            )),
```

**File:** verification/src/transaction_verifier.rs (L692-694)
```rust
                    let a = self.tx_env.epoch().to_rational();
                    let b = info.block_epoch.to_rational()
                        + epoch_number_with_fraction.normalize().to_rational();
```

**File:** util/rational/src/lib.rs (L34-41)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
        let mut ret = RationalU256::new_raw(numer, denom);
        ret.reduce();
        ret
    }
```

**File:** util/dao/utils/src/error.rs (L39-41)
```rust
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
