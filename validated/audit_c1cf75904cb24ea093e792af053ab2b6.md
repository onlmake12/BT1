### Title
`calculate_dao_maximum_withdraw` Option 1 Returns Flawed Withdrawal Amount Due to Hardcoded Output Identity Assumption - (File: `rpc/src/module/experiment.rs`)

---

### Summary

The `calculate_dao_maximum_withdraw` RPC method's option 1 (`WithdrawingHeaderHash`) hardcodes the assumption that the phase-1 (prepare) transaction output is structurally identical to the original deposit output for the purpose of occupied-capacity calculation. When this assumption is violated — which is possible for historical DAO cells committed before the `starting_block_limiting_dao_withdrawing_lock` consensus parameter — the RPC returns an incorrect maximum withdrawal capacity. An unprivileged RPC caller who relies on this estimate to build a withdrawal transaction will either construct an invalid transaction (overestimate) or leave earned interest unclaimed (underestimate).

---

### Finding Description

`calculate_dao_maximum_withdraw` accepts two calculation modes via `DaoWithdrawingCalculationKind`:

- **Option 1 (`WithdrawingHeaderHash`)**: The caller supplies only a block hash. The RPC fetches the **deposit** transaction's output and data, then passes them directly to `DaoCalculator::calculate_maximum_withdraw`.
- **Option 2 (`WithdrawingOutPoint`)**: The caller supplies the actual phase-1 out-point. The RPC fetches the **phase-1** transaction's output and data.

In option 1, the implementation in `rpc/src/module/experiment.rs` lines 246–267 reads the deposit output and passes it as if it were the phase-1 output:

```rust
// Option 1 path — uses deposit tx output, not the actual phase-1 output
let output = tx.outputs().get(out_point.index().into())...;
let output_data = tx.outputs_data().get(out_point.index().into())...;
calculator.calculate_maximum_withdraw(
    &output,                                          // ← deposit output assumed == phase-1 output
    core::Capacity::bytes(output_data.len())...,
    &deposit_header_hash,
    &withdrawing_header_hash.into(),
)
``` [1](#0-0) 

Inside `DaoCalculator::calculate_maximum_withdraw` (`util/dao/src/lib.rs` lines 149–156), the occupied capacity is derived from the passed `output`:

```rust
let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
let output_capacity: Capacity = output.capacity().into();
let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [2](#0-1) 

The occupied capacity is a function of the lock script args length, type script args length, and data length (see `util/gen-types/src/extension/capacity.rs`): [3](#0-2) 

The `DaoScriptSizeVerifier` (`verification/src/transaction_verifier.rs` lines 843–890) was introduced to enforce that the phase-1 output lock script is the same **size** as the deposit output lock script, but it only applies to cells committed at or after `starting_block_limiting_dao_withdrawing_lock`:

```rust
if let Some(info) = &input_meta.transaction_info
    && info.block_number < self.consensus.starting_block_limiting_dao_withdrawing_lock()
{
    continue;   // ← no size check for historical cells
}
``` [4](#0-3) 

For all DAO deposit cells committed **before** `starting_block_limiting_dao_withdrawing_lock`, the phase-1 transaction was permitted to use a lock script of a different size. In those cases, option 1 of `calculate_dao_maximum_withdraw` computes `occupied_capacity` from the deposit output's lock script size rather than the actual phase-1 output's lock script size, producing a wrong result.

The assumption is explicitly documented but treated as always safe:

> "option 1 … the calculation of occupied capacity will be based on the depositing transaction's output, **assuming the output of phase 1 transaction is the same as the depositing transaction's output**." [5](#0-4) 

---

### Impact Explanation

- **Overestimate** (deposit lock script smaller than phase-1 lock script): `occupied_capacity` is understated → `counted_capacity` is overstated → `withdraw_capacity` is inflated. A wallet that builds a withdrawal transaction using this inflated figure will produce a transaction that the on-chain DAO script rejects, because the actual withdrawable amount is lower. The user wastes fees on a failed transaction.
- **Underestimate** (deposit lock script larger than phase-1 lock script): `occupied_capacity` is overstated → `counted_capacity` is understated → `withdraw_capacity` is deflated. The user leaves earned DAO interest unclaimed.

Both outcomes are directly analogous to the original report's `getPriceOfAssetQuotedInUSD` returning inaccurate balances due to a flawed asset-property assumption.

---

### Likelihood Explanation

Any DAO depositor who, between the deposit phase and the prepare phase, changed their lock script to one with a different args length (e.g., migrated from a 20-byte secp256k1 lock to a 32-byte multisig lock, or vice versa) before `starting_block_limiting_dao_withdrawing_lock` is affected. Such historical cells remain live on mainnet. Any RPC caller (wallet, dApp, CLI user) querying option 1 for such a cell receives a wrong answer. The entry path requires no privilege: `calculate_dao_maximum_withdraw` is a public, unauthenticated RPC method.

---

### Recommendation

1. **Deprecate or warn on option 1**: Add an explicit warning in the RPC response or documentation that option 1 may return an incorrect value when the phase-1 lock script differs from the deposit lock script. Encourage callers to use option 2 (`WithdrawingOutPoint`) whenever the phase-1 transaction is already on-chain.
2. **Validate the assumption at call time**: When option 1 is used, attempt to locate the phase-1 transaction on-chain and, if found, fall back to option 2 logic automatically.
3. **Document the exact scope of the assumption**: Clarify that option 1 is only accurate when the phase-1 output is structurally identical to the deposit output (same lock script size, same type script, same data length).

---

### Proof of Concept

1. Create a DAO deposit cell with a 20-byte-args secp256k1 lock script (standard). Commit it before `starting_block_limiting_dao_withdrawing_lock`.
2. Create the phase-1 (prepare) transaction using a 32-byte-args multisig lock script (larger). This was valid before the size verifier was activated.
3. Call `calculate_dao_maximum_withdraw` with option 1 (the deposit out-point + a withdrawing block hash).
4. The RPC computes `occupied_capacity` using the 20-byte-args lock (deposit output), yielding a smaller occupied capacity and a larger `counted_capacity` than the actual phase-1 cell.
5. The returned `withdraw_capacity` is inflated relative to what the on-chain DAO script will actually permit.
6. Constructing a withdrawal transaction that claims the inflated amount results in script verification failure.

The discrepancy in occupied capacity between a 20-byte-args lock and a 32-byte-args lock is `Capacity::bytes(32 - 20) = 1200 shannons`, which scales with the DAO interest multiplier and can be non-trivial for large deposits held over long periods.

### Citations

**File:** rpc/src/module/experiment.rs (L114-115)
```rust
    /// option 1, the assumed reference block hash for withdrawing phase 1 transaction, this block must be in the
    /// [canonical chain](trait.ChainRpc.html#canonical-chain), the calculation of occupied capacity will be based on the depositing transaction's output, assuming the output of phase 1 transaction is the same as the depositing transaction's output.
```

**File:** rpc/src/module/experiment.rs (L246-267)
```rust
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```

**File:** util/dao/src/lib.rs (L149-156)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/gen-types/src/extension/capacity.rs (L14-41)
```rust
    pub fn occupied_capacity(&self) -> CapacityResult<Capacity> {
        Capacity::bytes(self.args().raw_data().len() + 32 + 1)
    }
}

impl packed::CellOutput {
    /// Calculates the occupied capacity of [`CellOutput`].
    ///
    /// Includes [`output_data`] (provided), [`capacity`] (8), [`lock`] (calculated) and [`type`] (calculated).
    ///
    /// [`CellOutput`]: https://github.com/nervosnetwork/ckb/blob/v0.36.0/util/types/schemas/blockchain.mol#L46-L50
    /// [`output_data`]: https://github.com/nervosnetwork/ckb/blob/v0.36.0/util/types/schemas/blockchain.mol#L63
    /// [`capacity`]: #method.capacity
    /// [`lock`]: #method.lock
    /// [`type`]: #method.type_
    pub fn occupied_capacity(&self, data_capacity: Capacity) -> CapacityResult<Capacity> {
        Capacity::bytes(8)
            .and_then(|x| x.safe_add(data_capacity))
            .and_then(|x| self.lock().occupied_capacity().and_then(|y| y.safe_add(x)))
            .and_then(|x| {
                self.type_()
                    .to_opt()
                    .as_ref()
                    .map(packed::Script::occupied_capacity)
                    .transpose()
                    .and_then(|y| y.unwrap_or_else(Capacity::zero).safe_add(x))
            })
    }
```

**File:** verification/src/transaction_verifier.rs (L874-881)
```rust
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }
```
