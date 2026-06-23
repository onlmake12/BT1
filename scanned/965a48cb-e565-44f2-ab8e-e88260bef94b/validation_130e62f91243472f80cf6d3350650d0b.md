### Title
Missing Zero `code_hash` Check in `CellbaseVerifier` Allows Permanent Burning of Block Rewards - (File: `verification/src/block_verifier.rs`)

---

### Summary

`CellbaseVerifier::verify()` validates the cellbase witness lock's `hash_type` against `ENABLED_SCRIPT_HASH_TYPE` but performs **no check** on whether `code_hash` is all zeros. A miner (or block submitter via `submit_block` RPC) can submit a block whose cellbase witness encodes a zero `code_hash` lock script. The block passes all validation, is stored on-chain, and when the delayed reward is finalized, `RewardVerifier` **enforces** that the reward output lock must match the stored zero lock — permanently burning the entire block reward to an unspendable address.

---

### Finding Description

CKB uses a delayed reward mechanism: the miner of block `H(c)` specifies their payout lock script inside the cellbase witness of block `H(c)`. The actual reward is paid out in the cellbase of block `H(c + PROPOSAL_WINDOW.farthest + 1)`, using the lock extracted from the stored witness of block `H(c)`.

**Step 1 — Acceptance of zero `code_hash` witness:**

`CellbaseVerifier::verify()` in `verification/src/block_verifier.rs` validates the cellbase witness at lines 106–124:

```rust
if cellbase_transaction
    .witnesses()
    .get(0)
    .and_then(|witness| {
        CellbaseWitness::from_slice(&witness.raw_data())
            .ok()
            .and_then(|cellbase_witness| {
                ScriptHashType::try_from(cellbase_witness.lock().hash_type())
                    .ok()
                    .and_then(|hash_type| {
                        let val: u8 = hash_type.into();
                        ENABLED_SCRIPT_HASH_TYPE.contains(&val).then_some(())
                    })
            })
    })
    .is_none()
{
    return Err((CellbaseError::InvalidWitness).into());
}
```

This only checks that `hash_type` is in `ENABLED_SCRIPT_HASH_TYPE`. It does **not** check whether `code_hash` is `0x0000...0000`. A cellbase witness with `code_hash = [0u8; 32]` and `hash_type = Data` (value `0`, which is in `ENABLED_SCRIPT_HASH_TYPE`) passes this check and the block is accepted. [1](#0-0) 

**Step 2 — Reward extraction without zero check:**

When the finalization block is built, `block_reward_internal()` in `util/reward-calculator/src/lib.rs` extracts `target_lock` from the stored cellbase witness at lines 90–101:

```rust
let target_lock = CellbaseWitness::from_slice(
    &self.store.get_cellbase(&target.hash())
        .expect("target cellbase exist")
        .witnesses().get(0)
        .expect("target witness exist")
        .raw_data(),
)
.expect("cellbase loaded from store should has non-empty witness")
.lock();
```

No check is performed on whether `target_lock.code_hash()` is all zeros. [2](#0-1) 

**Step 3 — `RewardVerifier` enforces the zero lock:**

`RewardVerifier::verify()` in `verification/contextual/src/contextual_block_verifier.rs` at lines 258–271 enforces that the cellbase output lock **must** equal `target_lock`:

```rust
if cellbase.transaction.outputs().get(0)
    .expect("cellbase should have output")
    .lock() != target_lock
{
    return Err((CellbaseError::InvalidRewardTarget).into());
}
```

This means any finalization block that tries to redirect the reward to a real address is **rejected**. The reward is irrevocably bound to the zero lock. [3](#0-2) 

**Contrast with genesis block verifier:**

The genesis block verifier in `spec/src/lib.rs` explicitly filters out zero `code_hash` lock scripts at line 697:

```rust
.filter(|lock_script| {
    lock_script != &genesis_cell_lock && lock_script.code_hash() != all_zero_lock_hash
})
```

The regular-block `CellbaseVerifier` has no equivalent guard. [4](#0-3) 

---

### Impact Explanation

A miner whose cellbase witness contains a zero `code_hash` lock script will have their entire block reward — primary issuance + secondary issuance + transaction fees + proposal rewards — permanently sent to an unspendable lock script (all-zeros `code_hash` matches no deployed cell, so the output can never be consumed). The funds are burned from the miner's perspective with no recovery path. The `RewardVerifier` actively prevents any corrective finalization block from redirecting the reward.

---

### Likelihood Explanation

The entry path is a miner submitting a block via the `submit_block` RPC with a crafted or misconfigured cellbase witness. The `sanitize_block_assembler_config()` in `util/launcher/src/lib.rs` provides a soft guard for the node's own block assembler (warning and disabling mining for non-recommended lock formats), but this guard is bypassed entirely when a block is submitted directly via `submit_block` RPC, which is the standard path for external mining software and pools. [5](#0-4) 

Realistic scenarios include: a mining pool operator misconfiguring `code_hash` as all zeros in their block-building software, a default/uninitialized configuration being used, or a malicious actor tricking a pool's block-building pipeline into using a zero `code_hash`. The CHANGELOG entry `#1602: Use all zeros as lock script which can never be unlocked` confirms that the zero lock is a known-unspendable pattern in CKB, making accidental use a realistic risk.

---

### Recommendation

Add a zero `code_hash` check inside `CellbaseVerifier::verify()` in `verification/src/block_verifier.rs`, mirroring the guard already present in the genesis block verifier:

```rust
let all_zero = packed::Byte32::default();
if cellbase_witness.lock().code_hash() == all_zero {
    return Err((CellbaseError::InvalidWitness).into());
}
```

This should be applied immediately after the `CellbaseWitness` is successfully parsed and before the block is accepted into the chain, ensuring the reward lock is always a meaningful, spendable script.

---

### Proof of Concept

1. Obtain a valid block template via `get_block_template` RPC.
2. Replace the cellbase witness with a `CellbaseWitness` whose `lock` has `code_hash = 0x0000000000000000000000000000000000000000000000000000000000000000` and `hash_type = 0x00` (`Data`).
3. Submit the modified block via `submit_block` RPC. The block is accepted — `CellbaseVerifier` only checks `hash_type`, not `code_hash`.
4. Mine `PROPOSAL_WINDOW.farthest + 1` more blocks.
5. Observe that the finalization cellbase output lock is the zero lock script, and the reward capacity is permanently unspendable. Any attempt to submit a finalization block with a different lock is rejected by `RewardVerifier` with `CellbaseError::InvalidRewardTarget`. [1](#0-0) [6](#0-5) [7](#0-6)

### Citations

**File:** verification/src/block_verifier.rs (L106-124)
```rust
        if cellbase_transaction
            .witnesses()
            .get(0)
            .and_then(|witness| {
                CellbaseWitness::from_slice(&witness.raw_data())
                    .ok()
                    .and_then(|cellbase_witness| {
                        ScriptHashType::try_from(cellbase_witness.lock().hash_type())
                            .ok()
                            .and_then(|hash_type| {
                                let val: u8 = hash_type.into();
                                ENABLED_SCRIPT_HASH_TYPE.contains(&val).then_some(())
                            })
                    })
            })
            .is_none()
        {
            return Err((CellbaseError::InvalidWitness).into());
        }
```

**File:** util/reward-calculator/src/lib.rs (L85-132)
```rust
    fn block_reward_internal(
        &self,
        target: &HeaderView,
        parent: &HeaderView,
    ) -> Result<(Script, BlockReward), DaoError> {
        let target_lock = CellbaseWitness::from_slice(
            &self
                .store
                .get_cellbase(&target.hash())
                .expect("target cellbase exist")
                .witnesses()
                .get(0)
                .expect("target witness exist")
                .raw_data(),
        )
        .expect("cellbase loaded from store should has non-empty witness")
        .lock();

        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L237-275)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let cellbase = &self.resolved[0];
        let no_finalization_target =
            (self.parent.number() + 1) <= self.context.consensus.finalization_delay_length();

        let (target_lock, block_reward) = self.context.finalize_block_reward(self.parent)?;
        let output = CellOutput::new_builder()
            .capacity(block_reward.total)
            .lock(target_lock.clone())
            .build();
        let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;

        if no_finalization_target || insufficient_reward_to_create_cell {
            let ret = if cellbase.transaction.outputs().is_empty() {
                Ok(())
            } else {
                Err((CellbaseError::InvalidRewardTarget).into())
            };
            return ret;
        }

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

**File:** spec/src/lib.rs (L690-698)
```rust
        let all_zero_lock_hash = packed::Byte32::default();
        // Check lock scripts
        for lock_script in block
            .transactions()
            .into_iter()
            .flat_map(|tx| tx.outputs().into_iter().map(move |output| output.lock()))
            .filter(|lock_script| {
                lock_script != &genesis_cell_lock && lock_script.code_hash() != all_zero_lock_hash
            })
```

**File:** util/launcher/src/lib.rs (L101-117)
```rust
            } else if self.args.block_assembler_advanced
                || (block_assembler.hash_type == ScriptHashType::Type
                    && block_assembler.args.len() == SECP256K1_BLAKE160_SIGHASH_ALL_ARG_LEN
                    && check_lock_code_hash(&(&block_assembler.code_hash).into())?)
            {
                if block_assembler.use_binary_version_as_message_prefix {
                    block_assembler.binary_version = self.version.long();
                }
                Some(block_assembler)
            } else {
                warn!(
                    "Miner is disabled because block assembler uses a non-recommended lock format. \
                     Edit ckb.toml or use `ckb run --ba-advanced` for other lock scripts"
                );

                None
            }
```
