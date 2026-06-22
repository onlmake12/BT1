### Title
Mixed-Input DAO Transaction Bypasses Capacity Overflow Check, Enabling CKB Minting — (`File: verification/src/transaction_verifier.rs`)

---

### Summary

`CapacityVerifier::valid_dao_withdraw_transaction()` uses `.any()` to detect whether a transaction qualifies as a DAO withdrawal. If **any** input carries the DAO type script, the entire `OutputsSumOverflow` check is skipped for the whole transaction. Because the DAO type script only verifies the DAO-specific cells, the capacity balance of non-DAO inputs against all outputs is never enforced. An attacker who holds a valid DAO withdrawal cell can craft a mixed-input transaction that mints CKB out of thin air.

---

### Finding Description

`CapacityVerifier::verify()` in `verification/src/transaction_verifier.rs` contains the following guard:

```rust
// verification/src/transaction_verifier.rs:478-494
pub fn verify(&self) -> Result<(), Error> {
    if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
        let inputs_sum = self.resolved_transaction.inputs_capacity()?;
        let outputs_sum = self.resolved_transaction.outputs_capacity()?;
        if inputs_sum < outputs_sum {
            return Err((TransactionError::OutputsSumOverflow { ... }).into());
        }
    }
    ...
}
```

The predicate that triggers the bypass is:

```rust
// verification/src/transaction_verifier.rs:517-522
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

`.any()` returns `true` the moment **one** input carries the DAO type script. When it does, the `OutputsSumOverflow` check is skipped for **all** inputs and **all** outputs in the transaction — including non-DAO inputs whose capacity is entirely unrelated to the DAO withdrawal.

The comment at line 482 states the rationale:

```
// DAO withdraw transaction is verified via the type script of DAO cells
```

However, the DAO type script (RFC-0023) only verifies the DAO-specific cells using `Source::Group`, which iterates only over cells sharing the same type script. It verifies that the DAO output capacity equals the maximum withdrawal amount for that specific DAO cell. It does **not** verify the global transaction balance (total inputs ≥ total outputs). Non-DAO inputs and outputs are invisible to it.

This creates a cross-attribution accounting gap directly analogous to H-04: the capacity check is applied to the transaction as a whole or not at all, without attributing which capacity belongs to DAO inputs versus non-DAO inputs.

**Concrete attack scenario:**

| Cell | Type | Capacity |
|------|------|----------|
| Input 0 | DAO withdrawal (phase 2) | 100 CKB → withdrawable as 110 CKB |
| Input 1 | Regular cell | 50 CKB |
| Output 0 | DAO withdrawal output | 110 CKB ✓ (verified by DAO script) |
| Output 1 | Attacker's regular cell | 100 CKB ✗ (never checked) |

- Total inputs: 150 CKB  
- Total outputs: 210 CKB  
- **Net minted: 60 CKB**

`valid_dao_withdraw_transaction()` returns `true` because Input 0 is a DAO cell. The `OutputsSumOverflow` check is skipped. The DAO type script verifies only Output 0. Output 1's 50 CKB excess over Input 1 is never caught by any verifier.

---

### Impact Explanation

**Impact: High.** An attacker can mint arbitrary CKB by including one DAO withdrawal input alongside regular inputs and creating outputs that exceed the total input capacity. This violates the fundamental conservation invariant of the CKB cell model. Minted CKB can be spent immediately in subsequent transactions, inflating the total supply and stealing value from all holders.

---

### Likelihood Explanation

**Likelihood: Medium.** The attacker must possess a valid DAO phase-2 withdrawal cell (i.e., they must have previously deposited into the DAO and submitted a phase-1 withdrawal transaction). This is a realistic precondition for any DAO participant. No privileged access, leaked keys, or majority hashpower is required. The transaction can be submitted directly to the tx-pool via the standard RPC interface and will be accepted by any miner.

---

### Recommendation

Replace the all-or-nothing bypass with a per-input attribution check. The `OutputsSumOverflow` guard should compare:

- **Non-DAO inputs capacity** against **non-DAO outputs capacity** (enforced by `CapacityVerifier`)
- **DAO inputs capacity** against **DAO outputs capacity** (enforced by the DAO type script, as today)

Concretely, `valid_dao_withdraw_transaction()` should not be used to skip the entire overflow check. Instead, the verifier should sum only the non-DAO input capacities and compare them against the non-DAO output capacities, allowing the DAO type script to remain responsible only for the DAO-attributed portion.

---

### Proof of Concept

The root cause is directly visible in the production source:

`CapacityVerifier::verify()` skips the overflow check when `valid_dao_withdraw_transaction()` is true: [1](#0-0) 

`valid_dao_withdraw_transaction()` uses `.any()` — one DAO input disables the check for the entire transaction: [2](#0-1) 

`CapacityVerifier` is instantiated and called unconditionally inside `ContextualTransactionVerifier::verify()`, which is the path used for all block transaction validation: [3](#0-2) 

`ContextualTransactionVerifier` is invoked for every non-cellbase transaction during block verification in `BlockTxsVerifier`: [4](#0-3) 

The DAO type script only verifies DAO-specific cells per RFC-0023; the non-DAO capacity balance is verified nowhere when the bypass is active: [5](#0-4)

### Citations

**File:** verification/src/transaction_verifier.rs (L154-164)
```rust
            capacity: CapacityVerifier::new(Arc::clone(&rtx), consensus.dao_type_hash()),
            fee_calculator: FeeCalculator::new(rtx, consensus, data_loader),
        }
    }

    /// Perform context-dependent verification, return a `Result` to `CacheEntry`
    ///
    /// skip script verify will result in the return value cycle always is zero
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
```

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

**File:** util/dao/src/lib.rs (L38-124)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
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
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
    }
```
