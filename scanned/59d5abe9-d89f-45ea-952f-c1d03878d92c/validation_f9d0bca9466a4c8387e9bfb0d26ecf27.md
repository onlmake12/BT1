### Title
`DaoScriptSizeVerifier` Bypassed in tx-pool Cache-Hit Verification Path — (`tx-pool/src/util.rs`)

---

### Summary

The `DaoScriptSizeVerifier` invariant (enforced by RFC-0044) is applied in the tx-pool's two non-cache verification branches but is **completely absent** from the cache-hit branch of `verify_rtx`. The block verifier, by contrast, always runs `DaoScriptSizeVerifier` on every transaction (cache hit or miss) once RFC-0044 is active. This asymmetric enforcement is the direct CKB analog of the DYAD H-06 finding: a critical invariant check is enforced in one code path but silently skipped in an equivalent path that achieves the same admission outcome.

---

### Finding Description

In `tx-pool/src/util.rs`, `verify_rtx` has three branches:

**Branch 1 — cache hit (lines 96–100):**
```rust
if let Some(completed) = cache_entry {
    TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
        .verify()
        .map(|_| *completed)
        .map_err(Reject::Verification)
    // ← DaoScriptSizeVerifier is NEVER called here
}
```

**Branch 2 — cache miss, async/pauseable (lines 101–115):**
```rust
ContextualTransactionVerifier::new(...)
    .verify_with_pause(max_tx_verify_cycles, command_rx)
    .await
    .and_then(|result| {
        DaoScriptSizeVerifier::new(rtx, ...).verify()?;  // ← enforced
        Ok(result)
    })
```

**Branch 3 — cache miss, sync (lines 116–131):**
```rust
ContextualTransactionVerifier::new(...).verify(max_tx_verify_cycles, false)
    .and_then(|result| {
        DaoScriptSizeVerifier::new(rtx, ...).verify()?;  // ← enforced
        Ok(result)
    })
```

All three branches admit a transaction into the tx-pool on success. Only branches 2 and 3 enforce `DaoScriptSizeVerifier`. Branch 1 (cache hit) silently skips it.

In `verification/contextual/src/contextual_block_verifier.rs` (lines 444–451), the block verifier applies `DaoScriptSizeVerifier` to **both** cache-hit and cache-miss transactions once `rfc0044_active`:

```rust
}.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(Arc::clone(tx), ...).verify()?;
    }
    Ok(result)
})
```

The tx-pool cache-hit path has no equivalent `rfc0044_active` guard and no `DaoScriptSizeVerifier` call at all.

---

### Impact Explanation

A DAO withdrawal transaction that violates the RFC-0044 lock-script-size invariant (deposit cell and withdrawing cell must use identically-sized lock scripts) can be admitted to the tx-pool via the cache-hit branch of `verify_rtx`. Once in the pool, it is eligible for inclusion in a block template. Any miner who includes it will produce a block that the block verifier rejects (because the block verifier always enforces `DaoScriptSizeVerifier` when RFC-0044 is active), causing the miner to lose their block reward. Additionally, the tx-pool's internal state becomes inconsistent: it holds a transaction that is invalid per current consensus rules, undermining the pool's role as a pre-filter for block assembly.

---

### Likelihood Explanation

The concrete trigger path is the RFC-0044 activation boundary combined with a chain reorg:

1. Before RFC-0044 activation, a DAO withdrawal transaction `T` with mismatched lock script sizes is valid per consensus (the block verifier does not yet run `DaoScriptSizeVerifier`). `T` is included in a block and its `witness_hash` is written into the shared `TxVerificationCache` by the block verifier.
2. A chain reorg detaches the block containing `T`. `update_tx_pool_for_reorg` is called; `readd_detached_tx` fetches the cached entry for `T` and calls `verify_rtx` with it.
3. `verify_rtx` takes the cache-hit branch (Branch 1), skipping `DaoScriptSizeVerifier`. `T` is re-admitted to the tx-pool.
4. RFC-0044 activates. `T` is now in the tx-pool but invalid per current consensus.
5. A miner assembles a block template containing `T`. The block verifier rejects the block.

This scenario is realistic during any hardfork activation window and requires no privileged access — only the ability to submit a DAO withdrawal transaction and wait for a natural reorg.

---

### Recommendation

Add `DaoScriptSizeVerifier` to the cache-hit branch of `verify_rtx`, gated by `rfc0044_active` to match the block verifier's behavior:

```rust
if let Some(completed) = cache_entry {
    let result = TimeRelativeTransactionVerifier::new(
        Arc::clone(&rtx), Arc::clone(&consensus), data_loader.clone(), Arc::clone(&tx_env),
    )
    .verify()
    .map(|_| *completed)
    .map_err(Reject::Verification)?;

    // Mirror the block verifier: enforce DaoScriptSizeVerifier on cache hits too
    if consensus.rfc0044_active(/* current epoch */) {
        DaoScriptSizeVerifier::new(rtx, consensus, snapshot.as_data_loader())
            .verify()
            .map_err(Reject::Verification)?;
    }
    Ok(result)
}
```

This makes all three branches of `verify_rtx` symmetric with respect to the RFC-0044 invariant, eliminating the gap between tx-pool admission and block verification.

---

### Proof of Concept

**Root cause — asymmetric branches in `verify_rtx`:** [1](#0-0) 

Cache-hit branch: only `TimeRelativeTransactionVerifier`, no `DaoScriptSizeVerifier`. [2](#0-1) 

Cache-miss async branch: `DaoScriptSizeVerifier` enforced. [3](#0-2) 

Cache-miss sync branch: `DaoScriptSizeVerifier` enforced.

**Block verifier enforces `DaoScriptSizeVerifier` on both cache-hit and cache-miss paths:** [4](#0-3) 

**Reorg re-admission path that triggers the cache-hit branch:** [5](#0-4) 

`readd_detached_tx` fetches the pre-existing cache entry for detached transactions and passes it to `verify_rtx`, causing the cache-hit branch to fire and `DaoScriptSizeVerifier` to be skipped for any transaction that was cached before RFC-0044 activation.

### Citations

**File:** tx-pool/src/util.rs (L96-100)
```rust
    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
```

**File:** tx-pool/src/util.rs (L110-115)
```rust
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
```

**File:** tx-pool/src/util.rs (L120-128)
```rust
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-453)
```rust
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

**File:** tx-pool/src/process.rs (L878-912)
```rust
    async fn readd_detached_tx(
        &self,
        tx_pool: &mut TxPool,
        txs: Vec<TransactionView>,
        fetched_cache: HashMap<Byte32, CacheEntry>,
    ) {
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
```
