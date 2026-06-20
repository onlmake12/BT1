I have all the code I need. Let me trace the full exploit path precisely.

### Title
Assume-Valid Cache Poisoning Allows Cycle-Limit Bypass and Consensus Deviation — (`verification/contextual/src/contextual_block_verifier.rs`, `verification/src/transaction_verifier.rs`)

---

### Summary

When `assume_valid_target` is active, every block processed by `verify_block` receives `Switch::DISABLE_SCRIPT`. `BlockTxsVerifier::verify` passes `skip_script_verify = true` to `ContextualTransactionVerifier::verify`, which unconditionally returns `Completed { cycles: 0, fee }`. That zero-cycle entry is written into the shared `TxVerificationCache` keyed by `witness_hash`. Later, when a block containing the same transaction is processed after the assume-valid period ends, `fetched_cache` returns the poisoned entry, the cache-hit branch skips all script execution, and the cycle sum check passes with `sum = 0`. A node that never saw the poisoned cache entry performs full script execution and rejects the same block, producing a consensus split.

---

### Finding Description

**Step 1 — `Switch::DISABLE_SCRIPT` is applied to every block while targets remain.** [1](#0-0) 

All blocks submitted to the chain service while `assume_valid_targets` is non-empty — including fork/orphan candidates relayed over P2P — receive `Switch::DISABLE_SCRIPT`.

**Step 2 — `skip_script_verify = true` causes `cycles = 0` to be returned and cached.** [2](#0-1) 

The code comment itself states: *"skip script verify will result in the return value cycle always is zero."* The returned `Completed { cycles: 0, fee }` is not marked as provisional.

**Step 3 — The result is unconditionally written to the shared cache.** [3](#0-2) 

`update_cache` is called regardless of whether `skip_script_verify` was true. There is no flag, TTL, or invalidation marker distinguishing a zero-cycle entry produced by script-skip from a genuine zero-cycle transaction.

**Step 4 — On the next block containing the same transaction, the cache hit branch skips all script execution.** [4](#0-3) 

When `fetched_cache.get(&wtx_hash)` returns `Some(completed)`, only `TimeRelativeTransactionVerifier` runs. The cached `completed` (with `cycles: 0`) is returned verbatim. No script is executed.

**Step 5 — The cycle-sum check passes trivially.** [5](#0-4) 

`sum = 0`, so `sum > max_block_cycles` is false. The block is accepted even if the transaction's real execution cost exceeds `max_block_cycles`.

---

### Impact Explanation

A node whose cache was poisoned during the assume-valid IBD phase accepts a block whose total script cycles genuinely exceed `max_block_cycles`. A node that never processed the poisoning fork block performs full script execution, measures the real cycle count, and rejects the same block with `ExceededMaximumCycles`. The two nodes permanently diverge on chain tip, violating the invariant that every committed block must be independently verifiable as `<= max_block_cycles` on all honest nodes.

---

### Likelihood Explanation

The attacker is an unprivileged miner. The attack requires mining exactly **two** valid PoW blocks — not a majority of hashpower:

1. **B1 (fork block, during IBD):** Attacker mines a valid block containing transaction T (whose script costs `> max_block_cycles`) on a fork. Attacker relays B1 to the victim node while `assume_valid_targets` is still active. The node processes B1 with `DISABLE_SCRIPT`, writes `cycles: 0` for T into the cache, then orphans B1 (the attacker's fork is shorter). T's inputs remain unspent.
2. **B2 (post-IBD block):** Attacker mines a valid block containing T after the assume-valid period ends. The victim node hits the poisoned cache entry, accepts B2. A fresh node rejects B2.

This does not require a 51% attack, leaked keys, or privileged access. The default mainnet/testnet configuration enables `assume_valid_targets` for all new nodes during IBD, making the attack surface broad.

---

### Recommendation

1. **Do not cache entries produced under `skip_script_verify = true`.** The simplest fix is to skip the `update_cache` call when `skip_script_verify` is true:

   In `BlockTxsVerifier::verify`, guard the cache update:
   ```rust
   if !ret.is_empty() && !skip_script_verify {
       self.update_cache(ret);
   }
   ```

2. **Alternatively**, store a `skip_script` boolean in `CacheEntry`/`Completed` and refuse to use entries with `skip_script = true` during full-verification block processing.

3. **Audit** whether the tx-pool's `verify_rtx` path (which also consults the same cache) can be reached with a poisoned entry and whether the same bypass applies there.

---

### Proof of Concept

```
Node A (victim, default mainnet config, fresh IBD):
  assume_valid_targets = [... T_last_target ...]

Attacker:
  1. Owns UTXO U (unspent on canonical chain).
  2. Constructs tx T: spends U, lock script loops for max_block_cycles + 1 cycles.
  3. Mines fork block B1 (valid PoW) at height H < T_last_target height, containing T.
  4. Relays B1 to Node A via P2P during IBD.
     → Node A: verify_block(B1, switch=DISABLE_SCRIPT)
     → ContextualTransactionVerifier::verify(T, skip=true) → Completed{cycles:0}
     → update_cache(T.witness_hash() → Completed{cycles:0})
     → B1 orphaned (canonical chain is longer); U still unspent.
  5. IBD completes; assume_valid_targets cleared; Node A now does full verification.
  6. Attacker mines B2 (valid PoW) containing T.
  7. Relays B2 to Node A and Node B (fresh node, no poisoned cache).

Node A: fetched_cache hit → cycles=0 → sum=0 ≤ max_block_cycles → ACCEPT B2
Node B: no cache hit → full script → cycles > max_block_cycles → REJECT B2

→ Consensus deviation confirmed.
```

### Citations

**File:** chain/src/verify.rs (L215-238)
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
        });
```

**File:** verification/src/transaction_verifier.rs (L159-172)
```rust
    /// Perform context-dependent verification, return a `Result` to `CacheEntry`
    ///
    /// skip script verify will result in the return value cycle always is zero
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L458-472)
```rust
        let sum: Cycle = ret.iter().map(|(_, cache_entry)| cache_entry.cycles).sum();
        let cache_entires = ret
            .iter()
            .map(|(_, completed)| completed)
            .cloned()
            .collect();
        if !ret.is_empty() {
            self.update_cache(ret);
        }

        if sum > self.context.consensus.max_block_cycles() {
            Err(BlockErrorKind::ExceededMaximumCycles.into())
        } else {
            Ok((sum, cache_entires))
        }
```
