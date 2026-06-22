### Title
Fee Calculation Performed After Expensive Script Execution in Synchronous Verification Path — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

In `ContextualTransactionVerifier::verify` and `complete`, the expensive CKB-VM script execution (`self.script.verify(max_cycles)`) runs before the fee calculation (`self.fee_calculator.transaction_fee()`). The async sibling method `verify_with_pause` correctly inverts this order — placing the fee check before script execution — demonstrating that the developers recognized the correct ordering but did not apply it to the synchronous paths. A block relayer can exploit this to force a verifying node to perform full CKB-VM script execution on transactions that would subsequently fail the fee calculation step.

---

### Finding Description

`ContextualTransactionVerifier` exposes three verification entry points. Their check ordering differs:

**`verify` (sync) — used in block verification:** [1](#0-0) 

```
time_relative.verify()   ← cheap
capacity.verify()        ← cheap
script.verify()          ← EXPENSIVE (CKB-VM, up to max_block_cycles)
fee_calculator.transaction_fee()  ← cheap, but comes LAST
```

**`complete` (sync) — used in resumable tx-pool verification:** [2](#0-1) 

Same ordering flaw: `script.complete()` runs before `fee_calculator.transaction_fee()`.

**`verify_with_pause` (async) — correctly ordered:** [3](#0-2) 

```
time_relative.verify()
capacity.verify()
fee_calculator.transaction_fee()  ← cheap check BEFORE script
script.resumable_verify_with_signal()  ← EXPENSIVE
```

The async path correctly places the fee check before script execution. The two synchronous paths do not.

`BlockTxsVerifier::verify` in the contextual block verifier calls `ContextualTransactionVerifier::verify` (sync) directly for every non-cached transaction in a block: [4](#0-3) 

This means the block relay/sync path inherits the misordered check sequence.

Note: the tx-pool submission path does perform `check_tx_fee` before `verify_rtx` in `pre_check`: [5](#0-4) 

So the tx-pool path is partially mitigated. The block verification path is not.

---

### Impact Explanation

When a verifying node processes a relayed block, `BlockTxsVerifier::verify` calls `ContextualTransactionVerifier::verify` for each non-cached transaction. For each such transaction, full CKB-VM script execution (up to `max_block_cycles` per transaction, bounded by the block cycle limit) completes before the fee calculation is attempted. If the fee calculation subsequently fails — for example, for DAO-withdraw transactions where `capacity.verify()` is intentionally skipped and `fee_calculator.transaction_fee()` may fail — all script execution cycles consumed up to that point are wasted. The `CapacityVerifier` explicitly skips the `inputs_sum >= outputs_sum` check for DAO-withdraw transactions: [6](#0-5) 

This creates a window where expensive CKB-VM work is performed before a cheap arithmetic check that could have short-circuited it.

---

### Likelihood Explanation

Any peer that can relay a block — i.e., any connected sync peer or block relayer — can trigger this path. No special privilege is required beyond being a connected network peer. The block need not be valid on the main chain; the verifying node processes transactions in the block before determining chain validity. The attacker constructs a block containing transactions with maximally expensive scripts (up to the block cycle limit) that are structured to fail the fee calculation step, causing the verifying node to burn CPU on script execution before the cheap fee check terminates processing.

---

### Recommendation

Move `self.fee_calculator.transaction_fee()` to before `self.script.verify(max_cycles)` in both `ContextualTransactionVerifier::verify` and `ContextualTransactionVerifier::complete`, matching the ordering already used in `verify_with_pause`:

```rust
pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
    self.time_relative.verify()?;
    self.capacity.verify()?;
    let fee = self.fee_calculator.transaction_fee()?;  // ← move here
    let cycles = if skip_script_verify {
        0
    } else {
        self.script.verify(max_cycles)?
    };
    Ok(Completed { cycles, fee })
}
```

Apply the same reordering to `complete`.

---

### Proof of Concept

1. Craft a block containing a DAO-withdraw transaction whose scripts are maximally expensive (consuming close to `max_block_cycles`) but whose fee calculation fails (outputs capacity exceeds inputs capacity — normally caught by `capacity.verify()`, but skipped for DAO-withdraw transactions per the `valid_dao_withdraw_transaction()` guard).
2. Relay this block to a target node via the sync/block-relay P2P path.
3. The target node calls `ContextualBlockVerifier::verify` → `BlockTxsVerifier::verify` → `ContextualTransactionVerifier::verify`.
4. `capacity.verify()` passes (DAO-withdraw skips the overflow check).
5. `script.verify(max_block_cycles)` runs to completion, consuming full CPU budget.
6. `fee_calculator.transaction_fee()` then fails.
7. The node has performed full CKB-VM execution unnecessarily; moving the fee check before step 5 would have terminated processing immediately after step 4.

### Citations

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

**File:** verification/src/transaction_verifier.rs (L177-190)
```rust
    pub async fn verify_with_pause(
        &self,
        max_cycles: Cycle,
        command_rx: &mut tokio::sync::watch::Receiver<ChunkCommand>,
    ) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let fee = self.fee_calculator.transaction_fee()?;
        let cycles = self
            .script
            .resumable_verify_with_signal(max_cycles, command_rx)
            .await?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/transaction_verifier.rs (L195-210)
```rust
    pub fn complete(
        &self,
        max_cycles: Cycle,
        skip_script_verify: bool,
        state: &TransactionState,
    ) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.complete(state, max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** verification/src/transaction_verifier.rs (L483-494)
```rust
        if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
            let inputs_sum = self.resolved_transaction.inputs_capacity()?;
            let outputs_sum = self.resolved_transaction.outputs_capacity()?;

            if inputs_sum < outputs_sum {
                return Err((TransactionError::OutputsSumOverflow {
                    inputs_sum,
                    outputs_sum,
                })
                .into());
            }
        }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L426-443)
```rust
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
```

**File:** tx-pool/src/process.rs (L286-294)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```
