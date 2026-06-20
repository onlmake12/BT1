### Title
`CapacityVerifier` Bypasses `OutputsSumOverflow` Check for DAO Transactions, Delegating to Incomplete DAO Type Script Enforcement — (File: verification/src/transaction_verifier.rs)

---

### Summary

`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction containing at least one DAO-typed input. The inline comment claims the DAO type script handles this, but the DAO type script only enforces per-cell DAO accounting — it does not verify the total capacity balance across all inputs and outputs. The actual total-balance enforcement is performed by `DaoCalculator::transaction_fee()` in the tx-pool admission path, not in the block verification path that uses `CapacityVerifier`. This split creates a non-standard, delegated accounting path analogous to the Balancer `managePoolBalance()` issue: the standard invariant check is bypassed and replaced by a secondary mechanism that does not fully cover all cells in the transaction.

---

### Finding Description

In `CapacityVerifier::verify()`, the `OutputsSumOverflow` guard is gated on a single boolean:

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    // ... OutputsSumOverflow check ...
}
```

`valid_dao_withdraw_transaction()` returns `true` if **any** input carries the DAO type script:

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

When this fires, the entire `OutputsSumOverflow` check is skipped for **all** outputs — including non-DAO outputs that are not touched by the DAO type script at all.

The comment reads: *"DAO withdraw transaction is verified via the type script of DAO cells."* The DAO type script (`dao.c`) verifies only the DAO-specific cells: it checks that each DAO output capacity equals the deposited capacity plus accrued interest. It does **not** verify the total capacity balance of the transaction, and it does not run for non-DAO outputs.

The actual total-balance enforcement for DAO transactions lives in `DaoCalculator::transaction_fee()`:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))
        .map_err(Into::into)
}
```

`transaction_maximum_withdraw()` folds over all inputs: for DAO inputs it computes the interest-adjusted maximum; for non-DAO inputs it returns the raw cell capacity. This is called in the tx-pool admission path via `check_tx_fee()` in `tx-pool/src/util.rs`. Whether the contextual block verifier independently invokes an equivalent check is not confirmed from the code read here.

---

### Impact Explanation

If the contextual block verifier does not independently invoke `DaoCalculator::transaction_fee()` (or an equivalent total-balance check), a miner can construct a block containing a DAO transaction where non-DAO outputs exceed non-DAO inputs. The `OutputsSumOverflow` guard is skipped by `CapacityVerifier`, the DAO type script only validates the DAO cells, and the non-DAO capacity inflation goes unchecked. This would allow a miner to create CKB capacity out of thin air — a consensus-level capacity inflation.

Even if the contextual block verifier does run the check today, the design creates a fragile dependency: the correctness of the capacity invariant for DAO transactions is silently delegated to a secondary mechanism (`DaoCalculator`) rather than enforced by the primary guard (`CapacityVerifier`). The misleading comment ("verified via the type script") obscures this dependency and makes the invariant invisible to future maintainers.

---

### Likelihood Explanation

Normal users cannot exploit this via the tx-pool because `check_tx_fee()` calls `DaoCalculator::transaction_fee()` and rejects transactions where outputs exceed the maximum withdraw. A miner, however, can bypass the tx-pool entirely and assemble a block template directly. The attacker profile (miner/block-template caller) is explicitly in scope per the prompt. Likelihood is medium: it requires a miner, but no privileged key or majority hashpower.

---

### Recommendation

1. Remove the blanket bypass. Instead of skipping `OutputsSumOverflow` entirely for DAO transactions, compute the DAO-adjusted input sum (using `DaoCalculator::transaction_maximum_withdraw`) and compare it against `outputs_capacity()` directly inside `CapacityVerifier::verify()`. This