### Title
Cache-Hit Path in `verify_rtx` Skips `DaoScriptSizeVerifier`, Allowing Invalid DAO Withdrawal Transactions into the Tx-Pool — (`tx-pool/src/util.rs`)

---

### Summary

The `verify_rtx` function in the CKB tx-pool has three execution branches. The two "full-verification" branches always run `DaoScriptSizeVerifier` after `ContextualTransactionVerifier`. The **cache-hit branch** — taken when a prior verification result exists in the LRU cache — runs only `TimeRelativeTransactionVerifier` and **silently omits `DaoScriptSizeVerifier`**. This is structurally identical to the GMX M-17 bug: a fallback/shortcut path bypasses a critical constraint check that the primary path enforces.

---

### Finding Description

`verify_rtx` in `tx-pool/src/util.rs` dispatches to one of three branches:

```
if let Some(completed) = cache_entry {          // ← cache-hit path
    TimeRelativeTransactionVerifier::new(...)
        .verify()
        .map(|_| *completed)                    // DaoScriptSizeVerifier ABSENT
        .map_err(Reject::Verification)
} else if let Some(command_rx) = command_rx {   // ← async full-verify path
    ContextualTransactionVerifier::new(...)
        .verify_with_pause(...)
        .and_then(|result| {
            DaoScriptSizeVerifier::new(...).verify()?;  // ← enforced
            Ok(result)
        })
} else {                                        // ← sync full-verify path
    block_in_place(|| {
        ContextualTransactionVerifier::new(...)
            .verify(...)
            .and_then(|result| {
                DaoScriptSizeVerifier::new(...).verify()?;  // ← enforced
                Ok(result)
            })
    })
}
``` [1](#0-0) 

`DaoScriptSizeVerifier` enforces RFC-0044: for a Nervos DAO withdrawal, the withdrawing cell's lock script must be the same byte-size as the deposit cell's lock script. Violating this rule is a consensus error (`TransactionError::DaoLockSizeMismatch`). [2](#0-1) 

The block-level verifier (`BlockTxsVerifier::verify`) correctly applies `DaoScriptSizeVerifier` in an `.and_then` that wraps **both** the cache-hit and non-cache-hit branches, so the block verifier is consistent: [3](#0-2) 

The tx-pool's cache-hit branch is not consistent with this.

**How the cache is populated:** `BlockTxsVerifier::update_cache` writes `(witness_hash → Completed)` entries after a block is accepted. Before RFC-0044 activation, `DaoScriptSizeVerifier` is not applied by the block verifier (the `rfc0044_active` guard is false), so a DAO transaction with mismatched lock-script sizes can be committed to a block and its result cached. [4](#0-3) 

After RFC-0044 activates, if that block is reorged and the transaction is re-submitted to the tx-pool, `verify_rtx` finds the cached entry and takes the cache-hit branch — skipping `DaoScriptSizeVerifier` — and admits the transaction into the pending pool. Any miner who then assembles a block containing this transaction will have their block rejected by the block verifier (which now enforces RFC-0044).

---

### Impact Explanation

- A DAO withdrawal transaction that violates RFC-0044 (mismatched lock-script sizes) is admitted into the tx-pool without the required `DaoScriptSizeVerifier` check.
- Miners who include the transaction in a block template will produce an invalid block, wasting PoW work and causing block rejection.
- The invalid transaction occupies tx-pool slots and propagates to peers (who may also accept it via their own cache-hit paths), amplifying resource waste across the network.
- The block verifier does catch it, so no consensus split occurs, but the tx-pool's admission guarantee is broken for this class of transaction.

---

### Likelihood Explanation

The trigger requires:
1. A DAO deposit cell with a lock script whose size differs from the intended withdrawal lock script.
2. A withdrawal transaction spending that cell to be included in a block **before** RFC-0044 activates (so the block verifier caches it without the size check).
3. RFC-0044 to activate (hard fork).
4. A chain reorg that orphans the block, causing the transaction to be re-submitted to the tx-pool.

Steps 3 and 4 are realistic during any hard-fork transition window. An attacker who controls a DAO deposit cell can craft the mismatched withdrawal transaction and time the submission to coincide with the fork boundary and a natural reorg. The LRU cache (`CACHE_SIZE = 30,000` entries) retains entries long enough to survive typical reorg depths. [5](#0-4) 

---

### Recommendation

Apply `DaoScriptSizeVerifier` in the cache-hit branch of `verify_rtx`, mirroring the block verifier's `.and_then` pattern:

```rust
if let Some(completed) = cache_entry {
    TimeRelativeTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
        .verify()
        .and_then(|_| {
            DaoScriptSizeVerifier::new(
                Arc::clone(&rtx),
                snapshot.cloned_consensus(),
                snapshot.as_data_loader(),
            )
            .verify()
        })
        .map(|_| *completed)
        .map_err(Reject::Verification)
```

This makes all three branches of `verify_rtx` consistent with each other and with the block verifier.

---

### Proof of Concept

1. Create a Nervos DAO deposit cell with lock script `L1` (size = 53 bytes).
2. Craft a withdrawal transaction spending that cell with output lock script `L2` (size = 73 bytes, violating RFC-0044).
3. Submit the withdrawal transaction to a node **before** RFC-0044 epoch activates. The block verifier skips `DaoScriptSizeVerifier` (`rfc0044_active` is false), includes the transaction in a block, and caches the result under the transaction's witness hash.
4. Trigger or wait for a reorg that orphans that block.
5. Re-submit the same withdrawal transaction to the tx-pool. `verify_rtx` finds the cached `Completed` entry, takes the cache-hit branch, runs only `TimeRelativeTransactionVerifier`, and admits the transaction — even though RFC-0044 is now active and `DaoScriptSizeVerifier` would reject it.
6. A miner assembles a block containing this transaction. `BlockTxsVerifier::verify` applies `DaoScriptSizeVerifier` (RFC-0044 is active), returns `DaoLockSizeMismatch`, and the block is rejected. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/util.rs (L96-131)
```rust
    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** verification/src/transaction_verifier.rs (L843-890)
```rust
    /// Verifies that for all Nervos DAO transactions, withdrawing cells must use lock scripts
    /// of the same size as corresponding deposit cells
    pub fn verify(&self) -> Result<(), Error> {
        let dao_type_hash = self.dao_type_hash();
        for (i, (input_meta, cell_output)) in self
            .resolved_transaction
            .resolved_inputs
            .iter()
            .zip(self.resolved_transaction.transaction.outputs())
            .enumerate()
        {
            // Both the input and output cell must use Nervos DAO as type script
            if !(cell_uses_dao_type_script(&input_meta.cell_output, &dao_type_hash)
                && cell_uses_dao_type_script(&cell_output, &dao_type_hash))
            {
                continue;
            }

            // A Nervos DAO deposit cell must have input data
            let input_data = match self.data_loader.load_cell_data(input_meta) {
                Some(data) => data,
                None => continue,
            };

            // Only input data with full zeros are counted as deposit cell
            if input_data.into_iter().any(|b| b != 0) {
                continue;
            }

            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
        }
        Ok(())
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L377-385)
```rust
    fn update_cache(&self, ret: Vec<(Byte32, Completed)>) {
        let txs_verify_cache = Arc::clone(self.txs_verify_cache);
        self.handle.spawn(async move {
            let mut guard = txs_verify_cache.write().await;
            for (k, v) in ret {
                guard.put(k, v);
            }
        });
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L409-453)
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
                } else {
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
```

**File:** verification/src/cache.rs (L13-17)
```rust
const CACHE_SIZE: usize = 1000 * 30;

/// Initialize cache
pub fn init_cache() -> TxVerificationCache {
    lru::LruCache::new(CACHE_SIZE)
```
