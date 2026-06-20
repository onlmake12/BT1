### Title
Unbounded Non-DAO Output Capacity in DAO Withdrawal Transactions Allows Unlimited CKB Minting — (File: `verification/src/transaction_verifier.rs`)

---

### Summary
`CapacityVerifier::verify()` unconditionally skips the `OutputsSumOverflow` check for any transaction that contains at least one DAO-type-script input. The DAO type script only verifies the capacity of the DAO cell itself (deposited amount × AR_current / AR_deposited), not the capacity of non-DAO outputs in the same transaction. As a result, a transaction sender can include non-DAO outputs whose total capacity exceeds the total capacity of non-DAO inputs, creating CKB out of thin air with no upper bound.

---

### Finding Description

In `verification/src/transaction_verifier.rs`, `CapacityVerifier::verify()` skips the `OutputsSumOverflow` check whenever `valid_dao_withdraw_transaction()` returns `true`: [1](#0-0) 

```rust
if !(self.resolved_transaction.is_cellbase() || self.valid_dao_withdraw_transaction()) {
    let inputs_sum = self.resolved_transaction.inputs_capacity()?;
    let outputs_sum = self.resolved_transaction.outputs_capacity()?;
    if inputs_sum < outputs_sum {
        return Err(...)
    }
}
```

`valid_dao_withdraw_transaction()` returns `true` if **any** resolved input cell carries the DAO type script: [2](#0-1) 

```rust
fn valid_dao_withdraw_transaction(&self) -> bool {
    self.resolved_transaction
        .resolved_inputs
        .iter()
        .any(|cell_meta| cell_uses_dao_type_script(&cell_meta.cell_output, &self.dao_type_hash))
}
```

The inline comment reads: *"DAO withdraw transaction is verified via the type script of DAO cells."* However, the DAO type script (a system script) only verifies the capacity of the DAO cell itself — specifically that `output_capacity = deposited_capacity × AR_current / AR_deposited`. It does **not** verify non-DAO outputs in the same transaction. [3](#0-2) 

Because the entire `OutputsSumOverflow` check is skipped for the whole transaction (not just the DAO-related portion), non-DAO outputs are subject to **no capacity upper-bound check** whatsoever.

---

### Impact Explanation

A transaction sender can craft a DAO withdrawal transaction that also includes non-DAO inputs and non-DAO outputs where:

```
non_dao_outputs_capacity > non_dao_inputs_capacity
```

The difference is created out of thin air, inflating the CKB supply. There is no limit on the amount that can be minted per transaction — the attacker can set non-DAO outputs to any value, bounded only by the `u64` capacity field. Repeated across multiple DAO deposits, this allows unlimited CKB minting, directly disrupting token supply and value.

---

### Likelihood Explanation

Any user who has made a DAO deposit can exploit this. The entry path is a standard `send_transaction` RPC call or tx-pool submission — no privileged role, leaked key, or majority hashpower is required. The attacker only needs to:
1. Hold a DAO deposit (any amount).
2. Construct a DAO withdrawal transaction with inflated non-DAO outputs.
3. Submit it via the public RPC. [4](#0-3) 

---

### Recommendation

Modify `CapacityVerifier::verify()` to separately verify that non-DAO outputs do not exceed non-DAO inputs, even for DAO withdrawal transactions. The `OutputsSumOverflow` relaxation should apply only to the DAO-related capacity delta (the interest), not to the entire transaction. Concretely:

- Compute `dao_inputs_sum` and `dao_outputs_sum` separately from non-DAO sums.
- Enforce `non_dao_outputs_sum <= non_dao_inputs_sum` unconditionally.
- Allow `total_outputs_sum > total_inputs_sum` only by the amount verified by the DAO type script.

---

### Proof of Concept

1. Create a DAO deposit: lock 100 CKB into the DAO.
2. Wait for the deposit to mature.
3. Construct a DAO withdrawal transaction (phase 2):
   - **DAO input**: 100 CKB (with DAO type script) → triggers `valid_dao_withdraw_transaction() = true`
   - **Non-DAO input**: 10 CKB
   - **DAO output**: 105 CKB (100 + 5 CKB interest — verified by DAO type script ✓)
   - **Non-DAO output**: 1,000,000 CKB (vastly exceeds non-DAO input — **NOT verified** ✗)
4. Submit via `send_transaction` RPC.
5. `CapacityVerifier` skips `OutputsSumOverflow` because a DAO input is present. [5](#0-4) 

6. The DAO type script only verifies the DAO cell: `105 = 100 × AR_current / AR_deposited` ✓. [6](#0-5) 

7. The transaction is accepted. ~999,990 CKB has been minted out of thin air.
8. Repeat with any DAO deposit to mint unlimited CKB.

The existing test `WithdrawDAOWithOverflowCapacity` only tests that inflating the **DAO output** by 1 shannon is rejected — it does not cover the case of inflated **non-DAO outputs**, confirming the gap. [7](#0-6)

### Citations

**File:** verification/src/transaction_verifier.rs (L461-494)
```rust
/// Perform inputs and outputs `capacity` field related verification
pub struct CapacityVerifier {
    resolved_transaction: Arc<ResolvedTransaction>,
    dao_type_hash: Byte32,
}

impl CapacityVerifier {
    /// Create a new `CapacityVerifier`
    pub fn new(resolved_transaction: Arc<ResolvedTransaction>, dao_type_hash: Byte32) -> Self {
        CapacityVerifier {
            resolved_transaction,
            dao_type_hash,
        }
    }

    /// Verify sum of inputs capacity should be greater than or equal to sum of outputs capacity
    /// Verify outputs capacity should be greater than or equal to its occupied capacity
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

**File:** util/dao/src/lib.rs (L199-205)
```rust
        let target_g2 = target_epoch
            .secondary_block_issuance(target.number(), self.consensus.secondary_epoch_reward())?;
        let (_, target_parent_c, _, target_parent_u) = extract_dao_data(target_parent.dao());
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** test/src/specs/dao/dao_tx.rs (L38-78)
```rust
pub struct WithdrawDAOWithOverflowCapacity;

impl Spec for WithdrawDAOWithOverflowCapacity {
    fn modify_chain_spec(&self, spec: &mut ckb_chain_spec::ChainSpec) {
        spec.params.genesis_epoch_length = Some(2);
        spec.params.epoch_duration_target = Some(16);
        spec.params.permanent_difficulty_in_dummy = Some(true);
    }

    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];
        let utxos = generate_utxo_set(node, 21);
        let mut user = DAOUser::new(node, utxos);

        ensure_committed(node, &user.deposit());
        node.mine(20); // Time makes interest
        ensure_committed(node, &user.prepare());

        let withdrawal = user.withdraw();
        let invalid_withdrawal = {
            let outputs: Vec<_> = withdrawal
                .outputs()
                .into_iter()
                .map(|cell_output| {
                    let old_capacity: Capacity = cell_output.capacity().into();
                    let new_capacity = old_capacity.safe_add(Capacity::one()).unwrap();
                    cell_output.as_builder().capacity(new_capacity).build()
                })
                .collect();
            withdrawal
                .as_advanced_builder()
                .set_outputs(outputs)
                .build()
        };
        let since = EpochNumberWithFraction::from_full_value(
            withdrawal.inputs().get(0).unwrap().since().into(),
        );
        goto_target_point(node, since);
        assert_send_transaction_fail(node, &invalid_withdrawal, "Overflow");
        ensure_committed(node, &withdrawal);
    }
```
