### Title
`DaoScriptSizeVerifier` Bypassed for First Block of RFC0044 Activation Epoch Due to Wrong Epoch Parameter — (`File: verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

In `BlockTxsVerifier::verify`, the hardfork activation guard for `DaoScriptSizeVerifier` is evaluated using the **parent** block's epoch number instead of the **current** block's epoch number. At the exact epoch boundary where RFC0044 activates, this off-by-one causes the size check to be silently skipped for every transaction in the first block of the activation epoch. A miner can exploit this window to include a transaction with an oversized DAO script that would otherwise be rejected, potentially causing a consensus split.

---

### Finding Description

In `BlockTxsVerifier::verify`, after each transaction is verified, the code conditionally applies `DaoScriptSizeVerifier`:

```rust
.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(
            Arc::clone(tx),
            Arc::clone(&self.context.consensus),
            self.context.store.as_data_loader(),
        ).verify()?;
    }
    Ok(result)
})
``` [1](#0-0) 

The guard calls `rfc0044_active` with `self.parent.epoch().number()` — the epoch number of the **parent** block — rather than `self.header.epoch().number()`, which is the epoch number of the **block currently being verified**. [2](#0-1) 

`self.header` is the current block's header and `self.parent` is the parent block's header, both stored as distinct fields in `BlockTxsVerifier`.

Suppose RFC0044 activates at epoch `N`. For the **first block** of epoch `N`:
- Its parent is the last block of epoch `N-1`, so `self.parent.epoch().number()` = `N-1`.
- `rfc0044_active(N-1)` returns `false`.
- `DaoScriptSizeVerifier` is **skipped** for every transaction in that block.

For every subsequent block of epoch `N`, the parent is also in epoch `N`, so `rfc0044_active(N)` = `true` and the check runs normally. The bug is confined to exactly one block per activation epoch.

The analogous correct call would be:
```rust
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
```

---

### Impact Explanation

`DaoScriptSizeVerifier` enforces a maximum script size for DAO-type cells. If it is skipped for the first block of the activation epoch, a miner can include a transaction whose DAO output script exceeds the RFC0044 size limit. Nodes running this code would accept the block; any node running a corrected build (or a future release that fixes the off-by-one) would reject it. This produces a **consensus split** at the activation epoch boundary.

The tx-pool applies `DaoScriptSizeVerifier` unconditionally (without the `rfc0044_active` guard):

```rust
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
``` [3](#0-2) 

This means the oversized transaction cannot enter the pool through normal submission. The attacker must bypass the tx-pool entirely by assembling the block manually and submitting it via the `submit_block` RPC.

---

### Likelihood Explanation

The attack window is narrow — exactly one block per RFC0044 activation epoch — but it is deterministic and predictable. A miner who knows the activation epoch can prepare the crafted transaction in advance and submit the block at the right moment. The `submit_block` RPC is a standard, documented interface available to any miner operating a node. [4](#0-3) 

The likelihood is **low** in practice (requires a deliberately malicious miner acting at a precise epoch boundary), but the root cause is a straightforward wrong-parameter bug with no additional preconditions.

---

### Recommendation

Replace `self.parent.epoch().number()` with `self.header.epoch().number()` in the `rfc0044_active` guard inside `BlockTxsVerifier::verify`:

```rust
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
``` [1](#0-0) 

Additionally, add a unit test that verifies `DaoScriptSizeVerifier` is enforced for the **first** block of the RFC0044 activation epoch, not just subsequent blocks.

---

### Proof of Concept

1. Determine the RFC0044 activation epoch `N` from the chain spec.
2. Craft a transaction `T` whose DAO-type output script exceeds the RFC0044 size limit. Attempting to submit `T` via `send_transaction` RPC will be rejected by the tx-pool's unconditional `DaoScriptSizeVerifier`.
3. Manually assemble a block at height `H` where `H` is the first block of epoch `N` (parent block is the last block of epoch `N-1`).
4. Include `T` directly in the block's transaction list, bypassing the tx-pool.
5. Submit the block via `submit_block` RPC.
6. The node accepts the block because `rfc0044_active(self.parent.epoch().number())` = `rfc0044_active(N-1)` = `false`, so `DaoScriptSizeVerifier` is never called.
7. A node running a corrected build evaluates `rfc0044_active(self.header.epoch().number())` = `rfc0044_active(N)` = `true`, runs `DaoScriptSizeVerifier`, and rejects the block — producing a consensus split. [5](#0-4)

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L323-346)
```rust
struct BlockTxsVerifier<'a, 'b, CS> {
    context: VerifyContext<CS>,
    header: HeaderView,
    handle: &'a Handle,
    txs_verify_cache: &'a Arc<RwLock<TxVerificationCache>>,
    parent: &'b HeaderView,
}

impl<'a, 'b, CS: ChainStore + VersionbitsIndexer + 'static> BlockTxsVerifier<'a, 'b, CS> {
    pub fn new(
        context: VerifyContext<CS>,
        header: HeaderView,
        handle: &'a Handle,
        txs_verify_cache: &'a Arc<RwLock<TxVerificationCache>>,
        parent: &'b HeaderView,
    ) -> Self {
        BlockTxsVerifier {
            context,
            header,
            handle,
            txs_verify_cache,
            parent,
        }
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L387-456)
```rust
    pub fn verify(
        &self,
        resolved: &'a [Arc<ResolvedTransaction>],
        skip_script_verify: bool,
    ) -> Result<(Cycle, Vec<Completed>), Error> {
        // We should skip updating tx_verify_cache about the cellbase tx,
        // putting it in cache that will never be used until lru cache expires.
        let fetched_cache = if resolved.len() > 1 {
            self.fetched_cache(resolved)
        } else {
            HashMap::new()
        };

        let tx_env = Arc::new(TxVerifyEnv::new_commit(&self.header));

        // make verifiers orthogonal
        let ret = resolved
            .par_iter()
            .enumerate()
            .map(|(index, tx)| {
                let wtx_hash = tx.transaction.witness_hash();

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
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
```

**File:** tx-pool/src/util.rs (L117-131)
```rust
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

**File:** rpc/src/module/chain.rs (L1813-1822)
```rust
    fn get_epoch_by_number(&self, epoch_number: EpochNumber) -> Result<Option<EpochView>> {
        let snapshot = self.shared.snapshot();
        Ok(snapshot
            .get_epoch_index(epoch_number.into())
            .and_then(|hash| {
                snapshot
                    .get_epoch_ext(&hash)
                    .map(|ext| EpochView::from_ext(ext.into()))
            }))
    }
```
