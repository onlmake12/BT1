### Title
Unvalidated Cellbase Witness Lock Script Permanently Locks Block Rewards Including User-Paid Transaction Fees — (File: `verification/src/block_verifier.rs`)

---

### Summary

CKB defers block reward finalization by `finalization_delay_length` blocks (11 on mainnet). The lock script that controls who can spend the reward cell is taken verbatim from the target block's cellbase witness. The `CellbaseVerifier` only checks that the witness is well-formed and that the `hash_type` byte is in the allowed set — it never validates that the `code_hash` references a deployed cell or that the script is actually executable. A miner who configures an unspendable lock script (wrong `code_hash`, non-deployed script, always-fail script) will cause the finalized reward cell — which bundles primary issuance, secondary issuance, and all user-paid transaction fees — to be permanently locked with no recovery path.

---

### Finding Description

CKB's reward mechanism works as follows:

1. When miner mines block `i`, they embed their desired lock script inside the cellbase witness (`CellbaseWitness.lock`).
2. `finalization_delay_length` blocks later (block `i + PROPOSAL_WINDOW.farthest + 1`), `RewardCalculator::block_reward_internal` reads that lock script from the stored cellbase witness and uses it as the output lock of the reward cell.
3. `RewardVerifier` then enforces that the finalizing block's cellbase output **must** use exactly that lock — no other lock is accepted.

The only validation applied to the cellbase witness lock at block-submission time is inside `CellbaseVerifier::verify()`:

```rust
// verification/src/block_verifier.rs  lines 106-124
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

`ENABLED_SCRIPT_HASH_TYPE` is `{0=Data, 1=Type, 2=Data1, 4=Data2}`. The check only confirms the `hash_type` byte is in that set. The `code_hash` field and `args` field are never validated against on-chain state. A lock script with a syntactically valid `hash_type` but a `code_hash` that does not correspond to any live cell passes this check unconditionally.

Eleven blocks later, `block_reward_internal` blindly reads that lock:

```rust
// util/reward-calculator/src/lib.rs  lines 90-101
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

And `RewardVerifier` enforces the finalizing cellbase output must carry exactly `target_lock`:

```rust
// verification/contextual/src/contextual_block_verifier.rs  lines 262-271
if cellbase.transaction.outputs().get(0)
    .expect("cellbase should have output")
    .lock() != target_lock
{
    return Err((CellbaseError::InvalidRewardTarget).into());
}
```

There is no escape hatch. The protocol mandates the unspendable lock, and no future block can override it.

---

### Impact Explanation

The reward cell bundles four components:

- **Primary issuance** — newly minted CKB
- **Secondary issuance** — inflation allocated to miners
- **Committed transaction fees** — 60% of every fee paid by users whose transactions were committed in block `i`
- **Proposal rewards** — 40% of fees for transactions proposed in block `i` and committed later

All four are locked into a single cell whose lock script is taken from the cellbase witness. If that lock is unspendable, the entire bundle is permanently inaccessible. User-paid fees — funds that belong to the protocol's economic participants, not the miner — are destroyed with no recourse. On a busy block this can represent a meaningful CKB amount. There is no governance mechanism, no admin key, and no timeout that can recover the cell.

---

### Likelihood Explanation

The `BlockAssemblerConfig` is a plain TOML file that every miner must configure manually. A miner who:

- Rotates to a new lock script before it is deployed on-chain,
- Copies a `code_hash` from a different network (testnet vs. mainnet),
- Mistypes the `args` field making the script permanently unspendable, or
- Uses a custom script whose deployment transaction is later orphaned

will silently embed an unspendable lock. The node accepts the block, the miner sees no error, and the damage is only discovered 11 blocks later when the reward cell is created — at which point it is too late. The 11-block delay means the miner may not even notice until after several reward cells have been locked.

---

### Recommendation

Validate the cellbase witness lock script against on-chain state at block-submission time inside `CellbaseVerifier::verify()`. Specifically:

1. For `hash_type = Data / Data1 / Data2`: verify that a live cell whose **data hash** matches `code_hash` exists in the current UTXO set.
2. For `hash_type = Type`: verify that a live cell whose **type script hash** matches `code_hash` exists.

If the referenced cell does not exist, reject the block with a new `CellbaseError::InvalidWitnessLock` variant. This mirrors the external report's recommended fix of allocating/validating at the triggering event (block submission) rather than deferring to a later step where recovery is impossible.

---

### Proof of Concept

1. Configure `ckb.toml` `[block_assembler]` with a `code_hash` that is 32 zero bytes and `hash_type = "data"` — a syntactically valid but non-deployed script:

   ```toml
   [block_assembler]
   code_hash = "0x0000000000000000000000000000000000000000000000000000000000000000"
   args      = "0x"
   hash_type = "data"
   message   = "0x"
   ```

2. Mine block `i`. `CellbaseVerifier` accepts it — `hash_type = 0` is in `ENABLED_SCRIPT_HASH_TYPE`, witness parses as valid `CellbaseWitness`.

3. Mine 11 more blocks. At block `i + 11`, `RewardCalculator::block_reward_internal` reads the zero `code_hash` lock from block `i`'s cellbase witness and `RewardVerifier` enforces the finalizing cellbase output carries that lock.

4. The reward cell (primary + secondary issuance + all committed tx fees from block `i`) is now on-chain with a lock that references a non-existent cell. Any attempt to spend it will fail at script resolution with `OutPoint not found`. The CKB is permanently burned.

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

**File:** util/constant/src/consensus.rs (L7-12)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
};
```

**File:** util/reward-calculator/src/lib.rs (L85-133)
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
    }
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

**File:** tx-pool/src/block_assembler/mod.rs (L490-519)
```rust
    pub(crate) fn build_cellbase_witness(
        config: &BlockAssemblerConfig,
        snapshot: &Snapshot,
    ) -> CellbaseWitness {
        let hash_type: ScriptHashType = config.hash_type.into();
        let cellbase_lock = Script::new_builder()
            .args(config.args.as_bytes())
            .code_hash(&config.code_hash)
            .hash_type(hash_type)
            .build();
        let tip = snapshot.tip_header();

        let mut message = vec![];
        if let Some(version) = snapshot.compute_versionbits(tip) {
            message.extend_from_slice(&version.to_le_bytes());
            message.extend_from_slice(b" ");
        }
        if config.use_binary_version_as_message_prefix {
            message.extend_from_slice(config.binary_version.as_bytes());
        }
        if !config.message.is_empty() {
            message.extend_from_slice(b" ");
            message.extend_from_slice(config.message.as_bytes());
        }

        CellbaseWitness::new_builder()
            .lock(cellbase_lock)
            .message(message)
            .build()
    }
```

**File:** util/types/src/core/reward.rs (L13-46)
```rust
pub struct BlockReward {
    /// The total block reward.
    pub total: Capacity,
    /// The primary block reward.
    pub primary: Capacity,
    /// The secondary block reward.
    ///
    /// # Notice
    ///
    /// - A part of the secondary issuance goes to the miners, the ratio depends on how many CKB
    ///   are used to store state.
    /// - And a part of the secondary issuance goes to the NervosDAO, the ratio depends on how many
    ///   CKB are deposited and locked in the NervosDAO.
    /// - The rest of the secondary issuance is determined by the community through the governance
    ///   mechanism.
    ///   Before the community can reach agreement, this part of the secondary issuance is going to
    ///   be burned.
    pub secondary: Capacity,
    /// The transaction fees that are rewarded to miners because the transaction is committed in
    /// the block.
    ///
    /// # Notice
    ///
    /// Miners only get 60% of the transaction fee for each transaction committed in the block.
    pub tx_fee: Capacity,
    /// The transaction fees that are rewarded to miners because the transaction is proposed in the
    /// block or its uncles.
    ///
    /// # Notice
    ///
    /// Miners only get 40% of the transaction fee for each transaction proposed in the block
    /// and committed later in its active commit window.
    pub proposal_reward: Capacity,
}
```
