Audit Report

## Title
Cache Poisoning via `assume_valid` Skip Allows Post-assume_valid Script Execution Bypass — (`verification/contextual/src/contextual_block_verifier.rs` + `verification/src/transaction_verifier.rs`)

## Summary

When `assume_valid` is active, `BlockTxsVerifier::verify` is called with `skip_script_verify=true`. For every cache-miss transaction, `ContextualTransactionVerifier::verify` returns `Completed{cycles:0, fee:F}` without executing any CKB-VM scripts. This result is unconditionally written into the shared `TxVerificationCache` keyed by `witness_hash`. The cache is never invalidated on chain rollback. If the same transaction (identical `witness_hash`) later appears in a post-assume_valid block, `fetched_cache` returns the poisoned entry and only `TimeRelativeTransactionVerifier` runs — no script execution occurs. A transaction whose lock or type script unconditionally fails is permanently accepted into the canonical chain.

## Finding Description

**Step 1 — `Switch::DISABLE_SCRIPT` is set during assume_valid.**

In `chain/src/verify.rs`, `verify_block` computes the switch per block. While assume_valid targets remain, every block is processed with `Switch::DISABLE_SCRIPT`: [1](#0-0) 

This switch is passed directly into `reconcile_main_chain` and then into `ContextualBlockVerifier::new`: [2](#0-1) 

**Step 2 — `ContextualBlockVerifier::verify` passes `disable_script()` to `BlockTxsVerifier::verify`.** [3](#0-2) 

`self.switch.disable_script()` returns `true` when `Switch::DISABLE_SCRIPT` is set: [4](#0-3) 

**Step 3 — `ContextualTransactionVerifier::verify` with `skip_script_verify=true` returns `Completed{cycles:0}` without script execution.** [5](#0-4) 

**Step 4 — `update_cache` is called unconditionally, regardless of `skip_script_verify`.**

After collecting all transaction results, `update_cache` is called with no guard on whether scripts were actually verified: [6](#0-5) 

The `Completed` struct stored in `TxVerificationCache` carries no `script_verified` flag: [7](#0-6) 

The cache is a shared LRU of 30,000 entries and is never invalidated on chain rollback: [8](#0-7) 

**Step 5 — Post-assume_valid block with the same transaction hits the poisoned cache.**

When the same transaction T (same `witness_hash` W) appears in a later block processed with `Switch::NONE`, `fetched_cache` returns the poisoned entry: [9](#0-8) 

The cache-hit branch runs only `TimeRelativeTransactionVerifier` and returns the cached `Completed{cycles:0}` — no `ScriptVerifier` / CKB-VM execution: [10](#0-9) 

**Why existing checks fail:** The `fetched_cache` lookup is unconditional on the `skip_script_verify` flag. There is no mechanism to distinguish a cache entry produced under `skip_script_verify=true` from one produced by full verification. The `Completed` struct is opaque with respect to how it was produced.

## Impact Explanation

A transaction whose lock or type script unconditionally fails (exit code ≠ 0) is accepted into the canonical chain without any CKB-VM execution. This violates the invariant that every committed transaction must pass script verification after assume_valid ends. Concretely, an attacker can commit a transaction that spends outputs protected by a lock script without satisfying it, constituting both **incorrect CKB-VM behavior** (High: "Incorrect implementation or behavior of CKB-VM or system scripts") and **economic damage** (Critical: "Vulnerabilities which could easily damage CKB economy"). Consensus deviation also results if nodes with a warm cache diverge from nodes without one.

## Likelihood Explanation

The precondition is that the victim node runs with `assume_valid` configured — a documented production feature for IBD. The attacker must:

1. Mine a fork block B1 at height N < assume_valid target with sufficient total difficulty to temporarily become the node's best chain during IBD. This requires PoW proportional to the difficulty at height N, not majority hashpower.
2. Relay B1 to the victim node during IBD. The node processes B1 as the new best block with `Switch::DISABLE_SCRIPT`, writing `Completed{cycles:0}` for T into `txs_verify_cache`.
3. The main chain accumulates more total difficulty; B1 is rolled back. The cache entry for T is **not** evicted.
4. Mine a main-chain block B2 at height M > assume_valid target containing T (T's inputs are unspent on the main chain since B1 was rolled back).
5. The victim node processes B2 with `Switch::NONE`, hits the poisoned cache entry, skips script execution, and commits T.

This requires mining two blocks and exploiting the P2P block relay path — both unprivileged operations. No majority hashpower is required.

## Recommendation

**Option A (minimal):** Do not call `update_cache` when `skip_script_verify=true`. Add a guard in `BlockTxsVerifier::verify`:

```rust
if !ret.is_empty() && !skip_script_verify {
    self.update_cache(ret);
}
```

**Option B (defense-in-depth):** Add a `script_verified: bool` field to `Completed`. In `BlockTxsVerifier::verify`, only treat a cache entry as a valid hit when `entry.script_verified == true`. Entries produced under `skip_script_verify=true` are stored with `script_verified=false` and treated as cache misses when full verification is required.

Option A is simpler and eliminates the poisoning entirely. Option B preserves caching of fee/cycle data for other uses while preventing script bypass.

## Proof of Concept

```
1. Configure node with assume_valid target = block hash H (some future block).
2. Build transaction T whose lock script always aborts (exit code != 0).
3. Mine fork block B1 at height N < H with total_difficulty > node's current tip:
   - Node processes B1 with Switch::DISABLE_SCRIPT
   - ContextualTransactionVerifier::verify(skip=true) → Completed{cycles:0, fee:F}
   - update_cache writes Completed{cycles:0} to txs_verify_cache[witness_hash(T)]
4. Main chain accumulates more work; B1 is rolled back. Cache entry persists.
5. Mine main-chain block B2 at height M > H containing T
   (T's inputs are unspent on main chain since B1 was rolled back).
6. Node processes B2 with Switch::NONE:
   - fetched_cache returns Completed{cycles:0} for witness_hash(T)
   - Only TimeRelativeTransactionVerifier runs; ScriptVerifier is never called
   - B2 is accepted; T is committed to the canonical chain
7. Assert: T's always-failing script was never executed; block accepted without error.
```

A fork/integration test can be written using the existing `chain/src/tests/` harness: construct a chain with `Switch::DISABLE_SCRIPT` for B1, roll it back, then attach B2 with `Switch::NONE` and assert that the block is accepted despite T's script always failing.

### Citations

**File:** chain/src/verify.rs (L215-237)
```rust
        let switch: Switch = switch.unwrap_or_else(|| {
            let mut assume_valid_targets = self.shared.assume_valid_targets();
            match *assume_valid_targets {
                Some(ref mut targets) => {
                    //
                    let block_hash: H256 = Into::<H256>::into(BlockView::hash(block));
                    if targets.first().eq(&Some(&block_hash)) {
                        targets.remove(0);
                        info!("CKB reached one assume_valid_target: 0x{}", block_hash);
                    }

                    if targets.is_empty() {
                        assume_valid_targets.take();
                        info!(
                            "CKB reached all assume_valid_targets, will do full verification now"
                        );
                        Switch::NONE
                    } else {
                        Switch::DISABLE_SCRIPT
                    }
                }
                None => Switch::NONE,
            }
```

**File:** chain/src/verify.rs (L657-665)
```rust
                                let contextual_block_verifier = ContextualBlockVerifier::new(
                                    verify_context.clone(),
                                    async_handle,
                                    switch,
                                    Arc::clone(&txs_verify_cache),
                                    &mmr,
                                );
                                let log_now = std::time::Instant::now();
                                let verify_result = contextual_block_verifier.verify(&resolved, b);
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L394-398)
```rust
        let fetched_cache = if resolved.len() > 1 {
            self.fetched_cache(resolved)
        } else {
            HashMap::new()
        };
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L409-424)
```rust
                if let Some(completed) = fetched_cache.get(&wtx_hash) {
                    TimeRelativeTransactionVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                            Arc::clone(&tx_env),
                        )
                        .verify()
                        .map_err(|error| {
                            BlockTransactionsError {
                                index: index as u32,
                                error,
                            }
                            .into()
                        })
                        .map(|_| (wtx_hash, *completed))
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L456-466)
```rust
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;

        let sum: Cycle = ret.iter().map(|(_, cache_entry)| cache_entry.cycles).sum();
        let cache_entires = ret
            .iter()
            .map(|(_, completed)| completed)
            .cloned()
            .collect();
        if !ret.is_empty() {
            self.update_cache(ret);
        }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L683-691)
```rust
        let ret = BlockTxsVerifier::new(
            self.context.clone(),
            header,
            self.handle,
            &self.txs_verify_cache,
            &parent,
        )
        .verify(resolved, self.switch.disable_script())?;
        Ok(ret)
```

**File:** verification/traits/src/lib.rs (L99-102)
```rust
    /// Whether script verifier is disabled
    pub fn disable_script(&self) -> bool {
        self.contains(Switch::DISABLE_SCRIPT)
    }
```

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/cache.rs (L11-13)
```rust
pub type TxVerificationCache = lru::LruCache<Byte32, CacheEntry>;

const CACHE_SIZE: usize = 1000 * 30;
```

**File:** verification/src/cache.rs (L32-39)
```rust
/// Completed entry
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Completed {
    /// Cached tx cycles
    pub cycles: Cycle,
    /// Cached tx fee
    pub fee: Capacity,
}
```
