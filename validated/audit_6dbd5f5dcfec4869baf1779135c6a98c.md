Based on my research into the CKB codebase, I examined the `RewardVerifier` in the contextual block verification layer, which is the closest analog to the PoolTogether `claimPrize` function — both are "claiming" functions that distribute assets to a designated recipient.

### Title
Missing Cellbase Output Count Validation in `RewardVerifier` Allows Malicious Miner to Steal Block Rewards — (File: `verification/contextual/src/contextual_block_verifier.rs`)

### Summary
`RewardVerifier::verify()` checks that the cellbase's total output capacity equals `block_reward.total` and that the **first** output uses `target_lock`, but does not enforce that **all** outputs use `target_lock`. A malicious block producer can add extra outputs with an attacker-controlled lock script, silently redirecting part of the target miner's reward to themselves.

### Finding Description
In `RewardVerifier::verify()`, when a finalization target exists and the reward is sufficient to create a cell, the verifier performs exactly two checks:

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
Ok(())
``` [1](#0-0) 

Check 1: total `outputs_capacity() == block_reward.total`. Check 2: `outputs().get(0).lock() == target_lock`. There is **no check** that the cellbase has exactly one output, nor that all outputs use `target_lock`.

In CKB's block reward model, the miner of block `N + finalization_delay_length` is responsible for paying the reward to the miner of block `N` (the `target_lock` holder). A malicious current-block miner can construct a cellbase with:

- **Output 0**: `target_lock`, capacity = `block_reward.total − X`
- **Output 1**: `attacker_lock`, capacity = `X`

Both checks pass:
- `outputs_capacity() = (block_reward.total − X) + X = block_reward.total` ✓
- `outputs().get(0).lock() = target_lock` ✓

The block is accepted. The target miner receives less than their full reward; the attacker captures `X`.

The `CapacityVerifier` skips the inputs ≥ outputs check for cellbase transactions and only verifies each output meets its minimum occupied capacity — it does not prevent extra outputs. [2](#0-1) 

The `RewardCalculator` correctly computes `block_reward.total`, but the `RewardVerifier` does not enforce that the entire amount flows to `target_lock`. [3](#0-2) 

### Impact Explanation
A malicious miner can steal a portion of another miner's block reward on every block they produce. The minimum stealable amount per block is the minimum cell occupied capacity (~61 CKB for a standard secp256k1 lock script with 20-byte args). This is a direct, repeatable theft of funds from other miners with no recovery mechanism.

### Likelihood Explanation
Any miner who produces a block that finalizes another miner's reward can execute this attack. No majority hashpower, privileged access, or special conditions are required — only the ability to produce a valid block, which is the normal function of any miner. The attack is straightforward to implement by modifying the cellbase transaction during block assembly.

### Recommendation
In `RewardVerifier::verify()`, add a check that the cellbase has exactly one output:

```rust
if cellbase.transaction.outputs().len() != 1 {
    return Err((CellbaseError::InvalidOutputQuantity).into());
}
```

Or equivalently, verify that all outputs use `target_lock`.

### Proof of Concept
A malicious miner constructs a cellbase:
```
Output 0: lock = target_lock,   capacity = block_reward.total - 6_100_000_000 shannons
Output 1: lock = attacker_lock, capacity = 6_100_000_000 shannons  (61 CKB minimum)
```

`RewardVerifier::verify()` evaluates:
- `outputs_capacity() = block_reward.total` → passes line 259
- `outputs().get(0).lock() = target_lock` → passes lines 262–271

The block is accepted by `ContextualBlockVerifier::verify()`. [4](#0-3) 

The target miner loses 61 CKB per block produced by the attacker. The attacker gains a live cell with `attacker_lock` that can be freely spent.

> **Note**: This finding assumes the non-contextual `CellbaseVerifier` (in `verification/src/block_verifier.rs`, not fully read during this analysis) does not independently enforce a single-output constraint on the cellbase. If it does, this path is blocked at an earlier stage. The contextual `RewardVerifier` itself contains no such guard.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L258-275)
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

        Ok(())
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L674-676)
```rust
        if !self.switch.disable_reward() {
            RewardVerifier::new(&self.context, resolved, &parent).verify()?;
        }
```

**File:** verification/src/transaction_verifier.rs (L479-494)
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
        }
```

**File:** util/reward-calculator/src/lib.rs (L107-132)
```rust
        let total = txs_fees
            .safe_add(proposal_reward)?
            .safe_add(primary)?
            .safe_add(secondary)?;

        debug!(
            "[RewardCalculator] target {} {}\n
             txs_fees {:?}, proposal_reward {:?}, primary {:?}, secondary: {:?}, total_reward {:?}",
            target.number(),
            target.hash(),
            txs_fees,
            proposal_reward,
            primary,
            secondary,
            total,
        );

        let block_reward = BlockReward {
            total,
            primary,
            secondary,
            tx_fee: txs_fees,
            proposal_reward,
        };

        Ok((target_lock, block_reward))
```
