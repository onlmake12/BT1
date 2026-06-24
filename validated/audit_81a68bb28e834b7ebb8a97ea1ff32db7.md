The code confirms the claim exactly. Both checks are verified:

- [1](#0-0)  — `RewardVerifier::verify` only checks total `outputs_capacity()` and the lock of `outputs[0]`. No check on `outputs[0].capacity()` and no check on output count.
- [2](#0-1)  — `CapacityVerifier` explicitly skips `OutputsSumOverflow` for cellbase, delegating to `RewardVerifier`, which does not fill the gap.

---

Audit Report

## Title
Cellbase Reward Recipient Receives Only First-Output Lock Check, Not Full Capacity — (File: verification/contextual/src/contextual_block_verifier.rs)

## Summary
`RewardVerifier::verify` checks that the cellbase's total output capacity equals `block_reward.total` and that `outputs[0]` carries the correct `target_lock`, but never checks that `outputs[0].capacity() == block_reward.total` or that there is exactly one output. A miner can split the cellbase into a dust first output satisfying the lock check and a second output redirecting the bulk of the reward to themselves, while passing all verification.

## Finding Description
In `verification/contextual/src/contextual_block_verifier.rs` lines 258–272, `RewardVerifier::verify` performs exactly two checks when a finalization target exists and the reward is sufficient to create a cell:

```rust
if cellbase.transaction.outputs_capacity()? != block_reward.total {
    return Err((CellbaseError::InvalidRewardAmount).into());
}
if cellbase.transaction.outputs().get(0)
    .expect("cellbase should have output")
    .lock() != target_lock
{
    return Err((CellbaseError::InvalidRewardTarget).into());
}
```

Check 1 verifies the **sum** of all outputs equals `block_reward.total`. Check 2 verifies only the **lock** of the first output. There is no check that the cellbase has exactly one output, nor that `outputs[0].capacity() == block_reward.total`.

`CapacityVerifier` in `verification/src/transaction_verifier.rs` lines 479–493 explicitly skips `OutputsSumOverflow` for cellbase transactions with the comment "cellbase's outputs are verified by RewardVerifier." This delegation is incomplete: `RewardVerifier` checks the total but not the per-output distribution.

A malicious miner crafts a cellbase with:
- `output[0]`: lock = `target_lock`, capacity = minimum occupied capacity (~61 CKB)
- `output[1]`: lock = attacker's own lock, capacity = `block_reward.total − 61 CKB`

Both checks pass: `outputs_capacity() == block_reward.total` ✓ and `outputs[0].lock() == target_lock` ✓. The block is accepted by all nodes.

## Impact Explanation
This is a **Critical** vulnerability matching "Vulnerabilities which could easily damage CKB economy." The delayed reward model means the miner of block `H` constructs the cellbase paying block `H − PROPOSAL_WINDOW.farthest − 1`'s miner. An attacker can steal nearly the entire block reward from the intended recipient on every block they mine. The stolen funds are irreversible once the block is committed.

## Likelihood Explanation
Any miner who successfully mines a single block can execute this attack with no special privileges, no majority hashpower, and no victim cooperation. The attack is a deliberate pre-submission construction of the cellbase transaction. It is silent — the block is valid and accepted by all nodes — and the victim has no recourse. The attack is repeatable on every block the attacker mines.

## Recommendation
Add a check in `RewardVerifier::verify` that enforces the first output carries the full block reward capacity:

```rust
if cellbase.transaction.outputs()
    .get(0)
    .expect("cellbase should have output")
    .capacity()
    .unpack() != block_reward.total
{
    return Err((CellbaseError::InvalidRewardAmount).into());
}
```

Alternatively, enforce that the cellbase has exactly one output (`outputs().len() == 1`) to prevent any capacity splitting. Either fix closes the gap left by the delegation from `CapacityVerifier` to `RewardVerifier`.

## Proof of Concept
1. Attacker mines block `H` (which finalizes the reward for block `H − PROPOSAL_WINDOW.farthest − 1`).
2. Attacker reads `target_lock` from block `H − PROPOSAL_WINDOW.farthest − 1`'s cellbase witness.
3. Attacker constructs the cellbase for block `H` with two outputs:
   - `output[0]`: lock = `target_lock`, capacity = 6100000000 shannons (61 CKB, minimum for a secp256k1 lock cell)
   - `output[1]`: lock = attacker's own lock, capacity = `block_reward.total − 6100000000`
4. Attacker submits block `H` via `submit_block` RPC.
5. `RewardVerifier::verify` (lines 258–272) passes: `outputs_capacity() == block_reward.total` ✓ and `outputs[0].lock() == target_lock` ✓.
6. `CapacityVerifier` (lines 479–493) skips `OutputsSumOverflow` for cellbase ✓.
7. Block `H` is accepted by all nodes. Block `H − PROPOSAL_WINDOW.farthest − 1`'s miner receives 61 CKB; attacker retains the remainder.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L258-272)
```rust
        if !insufficient_reward_to_create_cell {
            if cellbase.transaction.outputs_capacity()? != block_reward.total {
                return Err((CellbaseError::InvalidRewardAmount).into());
            }
            if cellbase
                .transaction
                .outputs()
                .get(0)
                .expect("cellbase should have output")
                .lock()
                != target_lock
            {
                return Err((CellbaseError::InvalidRewardTarget).into());
            }
        }
```

**File:** verification/src/transaction_verifier.rs (L479-493)
```rust
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
```
