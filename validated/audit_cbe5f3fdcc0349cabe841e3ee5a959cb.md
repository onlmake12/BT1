### Title
Miner-Controlled Arbitrary Lock Script in Cellbase Witness Leads to Permanently Locked Block Reward Capacity — (File: `verification/src/block_verifier.rs`)

---

### Summary

CKB's `CellbaseVerifier` validates only the `hash_type` field of the lock script embedded in the cellbase witness, but performs no check that the `code_hash` refers to a deployed, executable cell on-chain. Because the cellbase witness lock script is entirely miner-controlled and is later used verbatim as the output lock of the finalized block reward cell, a miner can embed an arbitrary or non-existent `code_hash`, causing the entire block reward (primary issuance + secondary issuance + transaction fees) to be permanently locked in an unspendable cell.

---

### Finding Description

**Reward lock derivation flow:**

Block `H(i)` miner embeds a `CellbaseWitness` in the cellbase transaction's witness field. The `lock` sub-field of that witness is an arbitrary `Script` chosen by the miner. At block `H(i + PROPOSAL_WINDOW.farthest + 1)`, `RewardCalculator::block_reward_internal` extracts this lock verbatim:

```rust
// util/reward-calculator/src/lib.rs:90-101
let target_lock = CellbaseWitness::from_slice(
    &self
        .store
        .get_cellbase(&target.hash())
        ...
        .raw_data(),
)
.expect("cellbase loaded from store should has non-empty witness")
.lock();
```

This `target_lock` is then used directly as the output lock of the reward cell in `BlockAssembler::build_cellbase`:

```rust
// tx-pool/src/block_assembler/mod.rs:541-544
let output = CellOutput::new_builder()
    .capacity(block_reward.total)
    .lock(target_lock)
    .build();
```

And `RewardVerifier::verify` enforces that the submitted cellbase output lock **matches** this miner-chosen `target_lock` — it does not independently validate whether the lock script is executable:

```rust
// verification/contextual/src/contextual_block_verifier.rs:262-270
if cellbase.transaction.outputs().get(0)...lock() != target_lock {
    return Err((CellbaseError::InvalidRewardTarget).into());
}
```

**What `CellbaseVerifier` actually checks:**

`CellbaseVerifier::verify` in `verification/src/block_verifier.rs` validates only that the `hash_type` byte of the witness lock is a member of `ENABLED_SCRIPT_HASH_TYPE` (`{0, 1, 2, 4}`):

```rust
// verification/src/block_verifier.rs:106-124
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

`ENABLED_SCRIPT_HASH_TYPE` is `{0, 1, 2, 4}`:

```rust
// util/constant/src/consensus.rs:7-12
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

There is **no check** that the `code_hash` field of the witness lock refers to a cell that exists on-chain or that the referenced script is executable. A `code_hash` of all-zeros, a random 32-byte value, or a hash of code that was never deployed all pass `CellbaseVerifier` without error.

**Local block assembler guard is insufficient:**

`sanitize_block_assembler_config` in `util/launcher/src/lib.rs` restricts the local block assembler to the standard secp256k1 lock unless `--ba-advanced` is passed. However, this guard applies only to the node's own block template generation. A miner can bypass it entirely by:
- Submitting a block directly via the `submit_block` RPC
- Relaying a block over P2P

Both paths go through `CellbaseVerifier`, which does not check `code_hash` existence.

---

### Impact Explanation

When the miner of block `H(i)` embeds a lock script whose `code_hash` does not correspond to any deployed cell, the reward cell created at block `H(i + PROPOSAL_WINDOW.farthest + 1)` carries that unresolvable lock. The CKB-VM will fail to locate the script code at spend time, making the cell permanently unspendable. The locked value equals the full block reward: primary issuance + secondary issuance + all committed transaction fees. The capacity is irrecoverably removed from circulation. Because the lock is enforced by `RewardVerifier` to match the miner-chosen `target_lock`, no subsequent block can correct it.

---

### Likelihood Explanation

Any miner operating with `--ba-advanced` (explicitly supported) or submitting blocks directly via `submit_block` RPC or P2P can trigger this. Misconfiguration of a custom lock script's `code_hash` (e.g., using a data-hash of code that was never deployed, or a type-hash of a cell that does not exist on the target chain) is a realistic operational error. The protocol provides no feedback at block submission time that the witness lock is unexecutable; the error only manifests when the reward cell is later attempted to be spent.

---

### Recommendation

- In `CellbaseVerifier::verify`, add a contextual check (or a separate contextual verifier) that resolves the `code_hash` in the cellbase witness lock against the current live-cell set and rejects the block if the referenced script cell does not exist.
- Alternatively, restrict the cellbase witness lock to a consensus-enforced whitelist of known system lock scripts (analogous to how `sanitize_block_assembler_config` restricts the local assembler), so that only verifiably executable lock scripts can be used as reward destinations.

---

### Proof of Concept

1. Miner constructs a cellbase transaction whose witness is a `CellbaseWitness` with `lock.code_hash = 0xdeadbeef...` (32 bytes not matching any deployed cell) and `lock.hash_type = 0x01` (Type — passes `ENABLED_SCRIPT_HASH_TYPE`).
2. Miner submits the block via `submit_block` RPC or P2P relay.
3. `CellbaseVerifier::verify` accepts the block: `hash_type = 1` is in `ENABLED_SCRIPT_HASH_TYPE`; no `code_hash` existence check is performed.
4. The block is committed. `RewardCalculator::block_reward_internal` reads the witness lock from the stored cellbase and returns it as `target_lock`.
5. At block `H(i + PROPOSAL_WINDOW.farthest + 1)`, `build_cellbase` creates a reward cell with `lock = target_lock` (the unresolvable script). `RewardVerifier` confirms the output lock matches `target_lock` and accepts the block.
6. The reward cell (primary + secondary + fees) is now on-chain with an unexecutable lock. Any attempt to spend it will fail at script resolution with "script cell not found." The capacity is permanently locked.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** util/reward-calculator/src/lib.rs (L90-101)
```rust
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
```

**File:** tx-pool/src/block_assembler/mod.rs (L537-544)
```rust
            let (target_lock, block_reward) = block_in_place(|| {
                RewardCalculator::new(snapshot.consensus(), snapshot).block_reward_to_finalize(tip)
            })?;
            let input = CellInput::new_cellbase_input(candidate_number);
            let output = CellOutput::new_builder()
                .capacity(block_reward.total)
                .lock(target_lock)
                .build();
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L242-271)
```rust
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
```

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
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
