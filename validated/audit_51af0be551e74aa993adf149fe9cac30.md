### Title
`CapacityVerifier` Skips `OutputsSumOverflow` for DAO Transactions While DAO Type Script Only Enforces Per-Cell Limits, Not Total Balance - (File: verification/src/transaction_verifier.rs)

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check (total inputs ≥ total outputs) for any transaction that has **any** DAO-type input, delegating the protection to the DAO type script. However, the DAO type script only enforces the per-cell maximum withdrawal amount for the DAO cell itself — it does not verify the total transaction balance. This creates a gap structurally identical to the reference report: a two-step protection where the second step (DAO type script) is insufficient for the case it is supposed to cover, leaving the first step (total balance check) unguarded.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` contains:

```rust
// skip OutputsSumOverflow verification for resolved cellbase and DAO
// withdraw transactions.
// cellbase's outputs are verified by RewardVerifier
// DAO withdraw transaction is verified via the type script of DAO cells
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
``` [1](#0-0) 

`valid_dao_withdraw_transaction()` returns `true` if **any** input cell carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
``` [2](#0-1) 

The comment's claim — "DAO withdraw transaction is verified via the type script of DAO cells" — is only partially true. The DAO type script (on-chain) verifies that the **output capacity of the DAO cell** does not exceed the maximum withdrawal amount for that specific cell. It does **not** verify that the sum of all outputs ≤ sum of all inputs.

`DaoCalculator::transaction_maximum_withdraw()` confirms this: for non-DAO inputs it simply returns `output.capacity()` (face value), and for DAO inputs it returns the interest-adjusted maximum. The total is `maximum_withdraw`. The fee check `maximum_withdraw - outputs_capacity` is only enforced in the tx-pool via `check_tx_fee`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
``` [3](#0-2) 

This `check_tx_fee` call lives only in the tx-pool path:

```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(...)
        .transaction_fee(rtx)
        ...
``` [4](#0-3) 

The block-verification path (`ContextualTransactionVerifier` called from `contextual_block_verifier.rs`) does **not** call `check_tx_fee`. It calls `CapacityVerifier` (which skips the check for DAO transactions) and executes the DAO type script (which only checks the DAO cell's per-cell limit). [5](#0-4) 

---

### Impact Explanation

A malicious miner can craft a block containing a transaction with:
- One DAO input (e.g., 100 CKB, max withdrawal = 110 CKB)
- One or more non-DAO inputs (e.g., 50 CKB)
- A DAO output of exactly 110 CKB (passes the DAO type script)
- Additional non-DAO outputs totaling more than 50 CKB (e.g., 90 CKB)

Total inputs = 150 CKB. Total outputs = 200 CKB. The `OutputsSumOverflow` check is skipped because `valid_dao_withdraw_transaction()` returns `true`. The DAO type script passes because the DAO output (110 CKB) ≤ maximum withdrawal. The extra 50 CKB is created out of thin air and committed to the chain.

This is a **capacity inflation / CKB issuance bypass** — the most severe class of consensus-layer vulnerability in a UTXO-based chain.

---

### Likelihood Explanation

Any miner (no majority hashpower required) can produce a single block containing such a transaction. The block passes all verifiers that other full nodes run during block import, because:
1. `CapacityVerifier` skips the total-balance check for DAO transactions.
2. The DAO type script only checks the DAO cell's per-cell limit.
3. `check_tx_fee` (the only code that computes `maximum_withdraw − outputs_capacity`) is not invoked during block verification.

The attacker needs only to be a miner and to construct the transaction correctly. No social engineering, no key leakage, no majority hashpower.

---

### Recommendation

The `OutputsSumOverflow` check should not be skipped wholesale for DAO transactions. Instead, the verifier should compute the DAO-adjusted maximum withdraw for all inputs (exactly as `DaoCalculator::transaction_maximum_withdraw` does) and assert that `outputs_capacity ≤ maximum_withdraw`. This is the same check already performed in the tx-pool via `check_tx_fee`, and it should be promoted into `CapacityVerifier` (or a new `ContextualCapacityVerifier`) so it is enforced during block verification as well.

Alternatively, the existing `DaoCalculator::transaction_fee()` call should be added to the block-verification pipeline (e.g., inside `ContextualTransactionVerifier`) so that the total-balance invariant is enforced regardless of whether the transaction entered via the tx-pool or was directly included in a mined block.

---

### Proof of Concept

Construct a transaction:
- **Input 0**: a DAO phase-2 (withdrawing) cell with 100 CKB, deposited at block `D`, withdrawn at block `W`, such that `calculate_maximum_withdraw` returns 110 CKB.
- **Input 1**: a normal cell with 50 CKB.
- **Output 0**: a normal cell with 110 CKB (DAO withdrawal proceeds, lock matches deposit cell — DAO type script passes).
- **Output 1**: a normal cell with 90 CKB (50 CKB face value + 40 CKB created out of thin air).
- **Witnesses**: valid DAO witness pointing to deposit header.

Step-by-step verification trace:
1. `CapacityVerifier::verify()` — `valid_dao_withdraw_transaction()` returns `true` (Input 0 has DAO type script) → `OutputsSumOverflow` check **skipped**. [6](#0-5) 
2. DAO type script execution — verifies Output 0 capacity (110 CKB) ≤ `calculate_maximum_withdraw(Input 0)` (110 CKB) → **passes**.
3. `check_tx_fee` — **not called** in block verification path. [4](#0-3) 
4. Block is accepted. 40 CKB has been created out of thin air.

### Citations

**File:** verification/src/transaction_verifier.rs (L478-494)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        // skip OutputsSumOverflow verification for resolved cellbase and DAO
        // withdraw transactions.
        // cellbase's outputs are verified by RewardVerifier
        // DAO withdraw transaction is verified via the type script of DAO cells
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

**File:** verification/src/transaction_verifier.rs (L517-522)
```rust
    fn valid_dao_withdraw_transaction(&self) -> bool {
        self.resolved_transaction
            .resolved_inputs
            .iter()
            .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
    }
```

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L426-453)
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
