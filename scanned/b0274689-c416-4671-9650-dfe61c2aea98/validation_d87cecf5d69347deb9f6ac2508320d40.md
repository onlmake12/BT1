### Title
Non-DAO Input Capacity Unprotected When Mixed Into DAO Withdraw Transaction — (`File: verification/src/transaction_verifier.rs`)

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to detect whether a transaction contains *any* DAO-type input. When it returns `true`, the entire `OutputsSumOverflow` guard — the only node-level check that enforces `inputs_capacity >= outputs_capacity` — is skipped for **all** inputs in the transaction, including non-DAO inputs. The DAO type script (on-chain) only enforces the withdrawal amount for DAO cells; it does not enforce the total transaction balance. This creates a gap where the "initial capacity" of non-DAO inputs mixed into a DAO withdraw transaction is unaccounted for at the consensus layer.

---

### Finding Description

In `CapacityVerifier::verify()`:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(TransactionError::OutputsSumOverflow { ... }.into());
    }
}
```

The guard is skipped whenever `valid_dao_withdraw_transaction()` returns `true`:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

The `.any()` predicate fires on a single DAO-typed input, regardless of how many non-DAO inputs are also present. The code comment reads:

> `// DAO withdraw transaction is verified via the type script of DAO cells`

This delegates the entire balance check to the DAO type script. But the DAO type script (RFC-0023) only verifies that each DAO output equals `calculate_maximum_withdraw()` for its corresponding DAO input. It does not verify the total transaction balance across all inputs and outputs.

`DaoCalculator::transaction_maximum_withdraw()` does include non-DAO inputs at face value:

```rust
} else {
    Ok(output.capacity().into())   // non-DAO input: face value only
}
```

And `transaction_fee()` computes `maximum_withdraw - outputs_capacity`, which would catch an overflow. However, based on the search results, `transaction_fee` is referenced within `verification/src/transaction_verifier.rs` but **not** in the chain block-processing path (`chain/**/*.rs`). Whether this check is enforced at consensus-commit time (not just tx-pool admission) is uncertain from the available code.

The structural gap is identical to the LiFi pattern: the "starting balance" of non-DAO inputs is not independently protected before the DAO interest is added to outputs. Just as LiFi's `startingBalance` only tracked the transferred asset and left intermediate token balances unguarded, CKB's `CapacityVerifier` only guards non-DAO transactions and leaves non-DAO inputs inside DAO transactions unguarded at the node layer.

---

### Impact Explanation

A transaction sender can craft a phase-2 DAO withdrawal transaction that also consumes one or more non-DAO inputs and sets total outputs to exceed total inputs. If the DAO type script does not independently enforce the global capacity balance (which RFC-0023 does not require it to do), and if `DaoCalculator::transaction_fee()` is not enforced on the consensus commit path, the transaction inflates the total CKB supply. Even if the tx-pool rejects it, a colluding miner can include it directly in a block template.

---

### Likelihood Explanation

Any unprivileged transaction sender can construct such a transaction. The entry path is the standard `send_transaction` RPC or direct block-template injection by a miner. No privileged access is required. The precondition is owning at least one DAO prepare cell (phase 2) and at least one non-DAO UTXO.

---

### Recommendation

Change `valid_dao_withdraw_transaction()` to only exempt the capacity check when **all** inputs are DAO cells, or — more precisely — perform a split check: enforce `non_dao_inputs_capacity >= non_dao_outputs_capacity` independently of the DAO interest calculation. Alternatively, ensure `DaoCalculator::transaction_fee()` (which correctly accounts for non-DAO inputs at face value) is called and its result validated (non-negative) on the consensus block-commit path, not only in the tx-pool.

---

### Proof of Concept

Construct a transaction:
- **Input 0**: DAO prepare cell, 100 CKB, max withdraw = 110 CKB (with accrued interest)
- **Input 1**: Non-DAO cell, 50 CKB
- **Output 0**: 110 CKB (DAO withdrawal — passes DAO type script check)
- **Output 1**: 60 CKB (non-DAO output — 10 CKB more than the non-DAO input)

Total inputs: 150 CKB. Total outputs: 170 CKB.

`valid_dao_withdraw_transaction()` returns `true` (Input 0 has DAO type script). The `OutputsSumOverflow` check is skipped entirely. The DAO type script verifies Output 0 = 110 CKB and passes. Output 1 = 60 CKB is never checked against Input 1 = 50 CKB at the node layer. The 10 CKB surplus is unaccounted for.

---

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** util/dao/src/lib.rs (L114-121)
```rust
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
```
