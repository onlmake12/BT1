### Title
Missing DAO Cell Type Validation in `calculate_dao_maximum_withdraw` Allows Arbitrary Cell Type Confusion — (`rpc/src/module/experiment.rs`)

---

### Summary

The `calculate_dao_maximum_withdraw` RPC handler in `ExperimentRpcImpl` accepts caller-supplied `out_point` and `withdrawing_out_point` parameters but never validates that either cell actually carries the DAO type script. Any RPC caller can supply two arbitrary committed cell out-points — neither of which is a DAO cell — and receive a fabricated "maximum withdraw" capacity value computed from those cells' capacities and the accumulation-rate (`ar`) fields of their respective block headers. This is the direct CKB analog of the Splitter/Pair address-type confusion: the function is designed to operate on one kind of object (DAO cells) but silently accepts and processes a completely different kind (any cell).

---

### Finding Description

`ExperimentRpcImpl::calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` (lines 235–299) has two branches keyed on `DaoWithdrawingCalculationKind`.

**`WithdrawingHeaderHash` branch (lines 246–267):**
The handler fetches the transaction at `out_point.tx_hash()`, extracts the output at `out_point.index()`, and immediately calls `calculator.calculate_maximum_withdraw(&output, …)`. There is no check that `output.type_()` is the DAO type script.

**`WithdrawingOutPoint` branch (lines 269–297):**
The handler fetches `deposit_header_hash` from `out_point.tx_hash()` (the deposit cell's block), then fetches `withdrawing_tx` and `withdrawing_header_hash` from `withdrawing_out_point.tx_hash()`, extracts the output at `withdrawing_out_point.index()`, and calls `calculator.calculate_maximum_withdraw(&output, …)`. Neither `out_point` nor `withdrawing_out_point` is checked to carry the DAO type script, and there is no check that `withdrawing_out_point` is actually a phase-1 withdrawal of `out_point`.

The low-level function `DaoCalculator::calculate_maximum_withdraw` in `util/dao/src/lib.rs` (lines 127–159) is a pure arithmetic function: it takes any `CellOutput` and computes:

```
withdraw_capacity = output.capacity * withdrawing_ar / deposit_ar + occupied_capacity
```

It does not inspect the cell's type script at all.

**Contrast with the correct path:** `transaction_maximum_withdraw` in `util/dao/src/lib.rs` (lines 38–124) — the path used during actual block verification — explicitly checks `is_dao_output` (lines 48–57) before applying any DAO interest logic. The RPC handler bypasses this guard entirely by calling `calculate_maximum_withdraw` directly.

---

### Impact Explanation

An RPC caller supplies two arbitrary committed cell out-points. The function returns:

```
output.capacity × (withdrawing_ar / deposit_ar) + occupied_capacity
```

where `output` is any cell at `withdrawing_out_point` and the `ar` values come from the block headers of whichever blocks committed those two transactions. Because `withdrawing_ar > deposit_ar` always holds for any two blocks in chronological order, the returned value is always strictly greater than the cell's actual capacity — an inflated, fabricated number.

Concrete consequences:
1. **Wallet/dApp transaction construction failure**: Any wallet or dApp that calls this RPC to determine how much capacity to claim in a DAO withdrawal transaction, and receives a result for a non-DAO cell, will construct a transaction that the on-chain DAO type script rejects. The user's transaction fails and fees are wasted.
2. **Misleading capacity oracle**: Automated tools or light clients that use this RPC as a trusted oracle for DAO yield calculations receive incorrect data. An attacker who controls which out-points are queried (e.g., by front-running a wallet's cell selection) can cause the wallet to overestimate withdrawable capacity.
3. **No direct fund loss on-chain**: The DAO type script enforces correct withdrawal amounts during actual transaction validation, so no CKB can be stolen via this path alone. The impact is confined to incorrect RPC output and the downstream failures it causes.

---

### Likelihood Explanation

The `calculate_dao_maximum_withdraw` RPC is publicly accessible to any RPC caller (no authentication required). The only prerequisite is that the attacker knows two committed transaction out-points — trivially satisfied by querying any block on the chain. The `WithdrawingOutPoint` branch is particularly easy to exploit because the two out-points need not be related at all.

---

### Recommendation

1. **Validate DAO type script on `out_point`**: Before calling `calculate_maximum_withdraw`, check that the cell at `out_point` has `type_script.hash_type == Type` and `type_script.code_hash == consensus.dao_type_hash()`. Return `RPCError::invalid_params` if not.

2. **Validate DAO type script on `withdrawing_out_point`** (for the `WithdrawingOutPoint` branch): Apply the same check to the cell at `withdrawing_out_point`.

3. **Validate phase relationship**: For the `WithdrawingOutPoint` branch, verify that the cell at `withdrawing_out_point` is a phase-1 withdrawal of `out_point` (i.e., its 8-byte cell data encodes the block number of the block that committed `out_point`'s transaction).

The `is_dao_type_script` closure already implemented in `transaction_maximum_withdraw` (lines 48–52 of `util/dao/src/lib.rs`) can be extracted and reused in the RPC handler.

---

### Proof of Concept

```
// 1. Find any two committed transactions on-chain:
//    tx_A committed in block B_early (low ar value)
//    tx_B committed in block B_late  (high ar value)
//    Neither tx_A nor tx_B involves a DAO cell.

// 2. Call the RPC:
POST /rpc
{
  "jsonrpc": "2.0",
  "method": "calculate_dao_maximum_withdraw",
  "params": [
    { "tx_hash": "<tx_A_hash>", "index": "0x0" },   // out_point: any non-DAO cell
    { "tx_hash": "<tx_B_hash>", "index": "0x0" }    // withdrawing_out_point: any non-DAO cell
  ],
  "id": 1
}

// 3. The node returns:
//    capacity = cell_B.capacity * (ar_B_late / ar_A_early) + occupied_capacity
//    This value is > cell_B.capacity and has no relation to any DAO deposit.

// 4. A wallet that trusts this result constructs a DAO withdrawal transaction
//    claiming this inflated capacity. The transaction is rejected on-chain by
//    the DAO type script, wasting the user's transaction fee.
```

The root cause is at `rpc/src/module/experiment.rs` lines 246–297 (both branches of `calculate_dao_maximum_withdraw`), where `calculator.calculate_maximum_withdraw` is called without first asserting that the supplied out-points reference cells carrying the DAO type script — the same missing-type-validation pattern as the Splitter/Pair address confusion in the reference report. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** rpc/src/module/experiment.rs (L269-297)
```rust
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
```

**File:** util/dao/src/lib.rs (L48-57)
```rust
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
```

**File:** util/dao/src/lib.rs (L127-159)
```rust
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
    }
```

**File:** util/jsonrpc-types/src/experiment.rs (L18-25)
```rust
#[derive(Clone, Serialize, Deserialize, PartialEq, Eq, Debug, JsonSchema)]
#[serde(untagged)]
pub enum DaoWithdrawingCalculationKind {
    /// the assumed reference block hash for withdrawing phase 1 transaction
    WithdrawingHeaderHash(H256),
    /// the out point of the withdrawing phase 1 transaction
    WithdrawingOutPoint(OutPoint),
}
```
