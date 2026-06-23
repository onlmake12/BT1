Let me trace through the relevant code to verify this claim.

I have all the code I need. Let me trace the exact path.

### Title
Cache Poisoning via `assume_valid` Skip Allows Post-assume_valid Script Execution Bypass — (`verification/contextual/src/contextual_block_verifier.rs` + `verification/src/transaction_verifier.rs`)

---

### Summary

When `assume_valid` is active, `ContextualTransactionVerifier::verify` is called with `skip_script_verify=true`, returning `Completed{cycles:0, fee:F}`. This result is unconditionally written into `txs_verify_cache` keyed by `witness_hash`. After assume_valid ends, if the same transaction (identical witness, same `witness_hash`) appears in a block, `BlockTxsVerifier::fetched_cache` returns the poisoned entry and only `TimeRelativeTransactionVerifier::verify` runs — no CKB-VM script execution occurs. A transaction with a script that always fails is permanently accepted into the canonical chain.

---

### Finding Description

**Step 1 — assume_valid sets `Switch::DISABLE_SCRIPT`.**

In `chain/src/verify.rs`, `verify_block` computes the switch per block: [1](#0-0) 

All blocks before the assume_valid target receive `Switch::DISABLE_SCRIPT`. This is passed down to `ContextualBlockVerifier::verify` and then to `BlockTxsVerifier::verify` as `skip_script_verify=true`: [2](#0-1) 

**Step 2 — `ContextualTransactionVerifier::verify` returns `Completed{cycles:0}` and it is cached.**

With `skip_script_verify=true`, the script verifier is never called; cycles is hardcoded to `0`: [3](#0-2) 

Back in `BlockTxsVerifier::verify`, the result is collected and `update_cache` is called **unconditionally**, regardless of whether `skip_script_verify` was true: [4](#0-3) 

The cache entry `Completed{cycles:0, fee:F}` is now stored in `TxVerificationCache` (an LRU of 30,000 entries) keyed by `witness_hash`: [5](#0-4) 

**Step 3 — Post-assume_valid block with the same transaction hits the poisoned cache.**

When the same transaction T (same `witness_hash` W) appears in a later block processed with `Switch::NONE`, `fetched_cache` is called first and returns the poisoned entry: [6](#0-5) 

The cache-hit branch runs only `TimeRelativeTransactionVerifier` and returns the cached `Completed{cycles:0}` — no `ScriptVerifier` / CKB-VM execution: [7](#0-6) 

There is no flag in `CacheEntry` / `Completed` to indicate the entry was produced under `skip_script_verify=true`: [8](#0-7) 

---

### Impact Explanation

A transaction whose lock or type script unconditionally fails is accepted into the canonical chain without any CKB-VM execution. This constitutes incorrect CKB-VM behavior: the invariant that every committed transaction must pass script verification after assume_valid ends is violated. Concretely, an attacker can permanently commit a transaction that steals funds protected by a lock script, or activates a malicious type script, with no on-chain enforcement.

---

### Likelihood Explanation

The precondition is that the node runs with `assume_valid` configured (a documented production feature for IBD). The attacker must:

1. Mine a fork block B1 (before the assume_valid target) containing malicious transaction T — this requires PoW proportional to historical difficulty, not majority hashpower.
2. Relay B1 to the victim node during IBD via P2P; the node processes it with `Switch::DISABLE_SCRIPT`, poisoning the cache.
3. B1 is orphaned (the main chain has more cumulative work).
4. Mine a main chain block B2 (after the assume_valid target) containing T — T's inputs are unspent on the main chain because B1 was orphaned.
5. The victim node processes B2 with `Switch::NONE`, hits the cache, skips script execution, and commits T.

This does not require majority hashpower or a 51% attack. It requires mining two blocks (one fork, one main-chain), which is within reach of a well-resourced miner. The entry point is "miner/block-template paths" + "P2P block relay", both listed as valid unprivileged paths.

---

### Recommendation

Tag cache entries with a `script_verified: bool` field. In `BlockTxsVerifier::verify`, only treat a cache entry as a valid hit when `script_verified == true` (or equivalently, when `skip_script_verify == false`). Entries produced under `skip_script_verify=true` should be stored with `script_verified=false` and treated as cache misses when full verification is required.

Alternatively, simply do not call `update_cache` when `skip_script_verify=true`.

---

### Proof of Concept

```
1. Configure node with assume_valid target = block hash H (some future block).
2. Build transaction T whose lock script always aborts (exit code != 0).
3. Mine fork block B1 at height N < H containing T.
   - Node processes B1 with Switch::DISABLE_SCRIPT → ContextualTransactionVerifier::verify(skip=true)
   - Returns Completed{cycles:0, fee:F} → stored in txs_verify_cache[witness_hash(T)]
4. Ensure B1 is orphaned (main chain at height N has more work).
5. Mine main-chain block B2 at height M > H containing T (T's inputs are unspent on main chain).
6. Node processes B2 with Switch::NONE:
   - fetched_cache returns Completed{cycles:0} for witness_hash(T)
   - Only TimeRelativeTransactionVerifier runs; ScriptVerifier is never called
   - B2 is accepted; T is committed to the canonical chain
7. Assert: T's always-failing script was never executed; block accepted without error.
```

### Citations

**File:** chain/src/verify.rs (L215-234)
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
