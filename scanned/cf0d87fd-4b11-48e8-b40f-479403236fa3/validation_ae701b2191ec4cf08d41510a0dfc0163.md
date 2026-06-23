### Title
Nervos DAO Withdrawal Permanently Blocked by Temporary Lock-Script-Size Restriction Pending DAO Script Upgrade — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`DaoScriptSizeVerifier` is explicitly documented as a **temporary solution** pending a DAO script upgrade. It enforces that a DAO withdrawing cell must use a lock script of the same byte-size as the original deposit cell. Any user whose deposit cell and desired withdrawing cell differ in lock script size will have their phase-1 DAO withdrawal transaction permanently rejected — both at the tx-pool admission layer and at block verification — until the DAO script is upgraded. No upgrade path is defined in the current code.

---

### Finding Description

`DaoScriptSizeVerifier` in `verification/src/transaction_verifier.rs` carries an explicit code comment:

> "It provides a temporary solution till Nervos DAO script can be properly upgraded." [1](#0-0) 

The verifier's `verify()` method iterates over all DAO deposit-to-withdrawing cell pairs in a transaction. For any deposit cell committed at or after `starting_block_limiting_dao_withdrawing_lock` (hardcoded to block **10,000,000** on mainnet), it enforces:

```
input_meta.cell_output.lock().total_size() == cell_output.lock().total_size()
```

If the sizes differ, it returns `TransactionError::DaoLockSizeMismatch`. [2](#0-1) 

The constant `STARTING_BLOCK_LIMITING_DAO_WITHDRAWING_LOCK = 10_000_000` is hardcoded in consensus: [3](#0-2) 

This verifier is applied in **two places**:

1. **Tx-pool admission** (`tx-pool/src/util.rs`): applied **unconditionally** (no epoch gate) to every DAO withdrawal transaction submitted to the pool. [4](#0-3) 

2. **Block verification** (`verification/contextual/src/contextual_block_verifier.rs`): applied only when `rfc0044_active(parent.epoch())` is true. [5](#0-4) 

This creates an asymmetry: the tx-pool rejects a transaction that block verification would accept (before RFC0044 activation epoch 8651 on mainnet). More critically, the restriction itself is permanent until the DAO script is upgraded — no such upgrade is defined or scheduled in the current codebase.

---

### Impact Explanation

Any CKB holder who:
- Deposited into the Nervos DAO at block ≥ 10,000,000 on mainnet, and
- Wishes to withdraw (phase 1) using a lock script of a **different byte-size** than the deposit lock script (e.g., migrating from a 20-byte-args single-sig lock to a 32-byte-args multisig lock, or any wallet upgrade that changes lock script args length)

will have their withdrawal transaction permanently rejected with `DaoLockSizeMismatch`. Their deposited CKB remains locked in the DAO indefinitely. The only resolution is either:
- Use a lock script of exactly the same byte-size (may not be possible for all wallet types), or
- Wait for a DAO script upgrade that has no defined timeline. [6](#0-5) 

---

### Likelihood Explanation

This is reachable by any unprivileged transaction sender. The entry path is:

1. User submits a DAO phase-1 withdrawal transaction via RPC (`send_transaction`) or P2P relay.
2. The tx-pool calls `verify_rtx`, which unconditionally invokes `DaoScriptSizeVerifier::verify()`.
3. If the deposit cell (committed at block ≥ 10M) has a lock script of size N and the withdrawing cell has a lock script of size M ≠ N, the transaction is rejected with `Reject::Verification(DaoLockSizeMismatch)`. [7](#0-6) 

This affects any user who changes wallet software, rotates keys to a different script type, or uses a multisig setup with a different args length than their deposit. Given that mainnet has been running past block 10M for years, a significant portion of DAO deposits fall under this restriction.

---

### Recommendation

The code comment itself acknowledges this is a temporary workaround. The long-term fix requires upgrading the Nervos DAO type script to properly handle lock scripts of different sizes during withdrawal. Until then:

1. Document the restriction explicitly in user-facing documentation so depositors are aware before depositing.
2. Define and publish a concrete upgrade timeline for the DAO script.
3. Resolve the asymmetry between tx-pool (unconditional) and block verification (`rfc0044_active`-gated) application of `DaoScriptSizeVerifier` to avoid confusing rejection behavior. [5](#0-4) [4](#0-3) 

---

### Proof of Concept

1. Deposit CKB into the Nervos DAO at block ≥ 10,000,000 using a lock script with 20-byte args (e.g., secp256k1-blake160 single-sig).
2. Construct a phase-1 withdrawal transaction where the withdrawing cell uses a lock script with 32-byte args (e.g., secp256k1-blake160 multisig).
3. Submit via `send_transaction` RPC.
4. The node calls `verify_rtx` → `DaoScriptSizeVerifier::verify()`:
   - `input_meta.cell_output.lock().total_size()` = 53 bytes (20-byte args)
   - `cell_output.lock().total_size()` = 65 bytes (32-byte args)
   - `53 != 65` → returns `Err(TransactionError::DaoLockSizeMismatch { index: i })`
5. Transaction is permanently rejected. The deposited CKB cannot be withdrawn until the DAO script is upgraded. [8](#0-7) [3](#0-2)

### Citations

**File:** verification/src/transaction_verifier.rs (L817-818)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
```

**File:** verification/src/transaction_verifier.rs (L872-887)
```rust
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
```

**File:** spec/src/consensus.rs (L103-105)
```rust
/// The starting block number from which the lock script size of a DAO withdrawing
/// cell shall be limited
pub(crate) const STARTING_BLOCK_LIMITING_DAO_WITHDRAWING_LOCK: u64 = 10_000_000;
```

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

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

**File:** util/types/src/core/error.rs (L203-211)
```rust
    /// Nervos DAO lock size mismatch.
    #[error(
        "The lock script size of deposit cell at index {} does not match the withdrawing cell at the same index",
        index
    )]
    DaoLockSizeMismatch {
        /// The index of mismatched DAO cells.
        index: usize,
    },
```
