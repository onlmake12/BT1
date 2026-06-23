### Title
Malicious Miner Process Can Substitute Cellbase Witness Lock to Redirect Block Rewards - (`rpc/src/module/miner.rs`, `verification/src/block_verifier.rs`)

---

### Summary

The `submit_block` RPC accepts any structurally valid block without verifying that the cellbase witness lock matches the node's configured `block_assembler` lock. A malicious or compromised miner process (the CKB analog of the Connext relayer) can replace the cellbase witness lock with its own lock script before submitting a solved block, redirecting the future block reward to an attacker-controlled address. The consensus verifier only checks the witness format, not its identity against the node's intended reward recipient.

---

### Finding Description

CKB uses a client-server (CS) architecture for mining. The node process (`ckb run`) generates a block template via `get_block_template`, embedding the configured `block_assembler` lock script into the cellbase witness. The miner process (`ckb miner`) fetches this template, solves the PoW puzzle, and submits the solved block via `submit_block`.

The reward mechanism is delayed: when miner mines block H(c), the reward is finalized at block H(c + PROPOSAL_WINDOW.farthest + 1). The `RewardCalculator::block_reward_internal` reads the cellbase witness lock from the stored target block to determine who receives the reward: [1](#0-0) 

The `RewardVerifier` then enforces that the finalization block's cellbase output lock matches this stored witness lock: [2](#0-1) 

The critical gap is that **neither `submit_block` nor `CellbaseVerifier` checks that the cellbase witness lock in the submitted block matches the node's configured `block_assembler` lock**. The `submit_block` implementation only verifies the header and passes the block to `blocking_process_block`: [3](#0-2) 

The `work_id` parameter is accepted and logged but is never used to retrieve the original template and compare the cellbase. The `CellbaseVerifier` only checks structural validity (format, hash type, input structure) — it does not check the witness lock identity: [4](#0-3) 

The `BlockAssembler::build_cellbase` correctly embeds the configured lock into the witness, but this is only advisory — the submitted block's cellbase witness is accepted as-is: [5](#0-4) 

---

### Impact Explanation

A malicious miner process (or any process with access to the `submit_block` RPC) can:

1. Fetch the block template (which contains the node operator's configured reward lock in the cellbase witness).
2. Replace the cellbase witness lock with an attacker-controlled lock script.
3. Solve the PoW puzzle (or intercept a solved block).
4. Submit the modified block via `submit_block`.

The node accepts the block because `CellbaseVerifier` only validates the witness format and hash type, not the lock identity. The stored cellbase witness now contains the attacker's lock. When the finalization block is mined `PROPOSAL_WINDOW.farthest + 1` blocks later, `RewardCalculator` reads the attacker's lock from the stored witness and the entire block reward (primary issuance + secondary issuance + tx fees + proposal reward) is paid to the attacker's address.

The legitimate node operator loses their entire block reward for that block with no on-chain recourse. [6](#0-5) 

---

### Likelihood Explanation

The `submit_block` RPC is bound to `127.0.0.1:8114` by default, so the attacker must be a process running on the same host as the node. This is realistic in several scenarios:

- **Third-party mining software**: Node operators commonly use third-party miner binaries (not the official `ckb miner`) that connect to the local RPC. A malicious or backdoored miner binary can silently substitute the witness lock.
- **Mining pool workers**: In pool mining setups, the pool server submits blocks on behalf of workers. A malicious pool operator can substitute the witness lock to steal the block reward from the node operator.
- **Notify mode**: The `ckb miner` supports a notify mode where an external HTTP server triggers template fetches. A compromised notify endpoint can coordinate witness substitution. [7](#0-6) 

---

### Recommendation

The `submit_block` RPC should verify that the cellbase witness lock in the submitted block matches the configured `block_assembler` lock before accepting the block. Specifically:

1. In `MinerRpcImpl::submit_block`, after deserializing the block, extract the cellbase witness lock and compare it against `self.shared`'s configured `block_assembler` lock.
2. Alternatively, store the original cellbase transaction keyed by `work_id` when the template is generated, and in `submit_block`, verify that the submitted block's cellbase witness matches the stored one for that `work_id`.

The `BlockTemplate` documentation already states "Miners must use it as the cellbase transaction without changes in the assembled block," but this constraint is not enforced in code. [8](#0-7) 

---

### Proof of Concept

1. Node is configured with `block_assembler` lock `L_node` (the legitimate operator's lock).
2. `get_block_template` returns a template with cellbase witness containing `L_node`.
3. Attacker's miner process fetches the template, replaces the cellbase witness lock with `L_attacker`, recomputes the transactions root (the cellbase hash changes, so the merkle root changes), and solves PoW over the modified header.
4. Attacker calls `submit_block(work_id, modified_block)`.
5. `CellbaseVerifier` passes: the witness is a valid `CellbaseWitness` with a valid `hash_type`.
6. `RewardVerifier` passes: the cellbase output lock matches the *target* block's witness (a different, earlier block), not the current block's witness.
7. The block is accepted and stored with `L_attacker` in the cellbase witness.
8. `PROPOSAL_WINDOW.farthest + 1` blocks later, `RewardCalculator::block_reward_internal` reads `L_attacker` from the stored witness and the full block reward is paid to the attacker. [1](#0-0) [4](#0-3)

### Citations

**File:** util/reward-calculator/src/lib.rs (L19-32)
```rust
/// Block Reward Calculator.
/// A Block reward calculator is used to calculate the total block reward for the target block.
///
/// For block(i) miner, CKB issues its total block reward by enforcing the
/// block(i + PROPOSAL_WINDOW.farthest + 1)'s cellbase:
///   - cellbase output capacity is block(i)'s total block reward
///   - cellbase output lock is block(i)'s miner provided lock in block(i) 's cellbase output-data
///     Conventionally, We say that block(i) is block(i + PROPOSAL_WINDOW.farthest + 1)'s target block.
///
/// Target block's total reward consists of four parts:
///  - primary block reward
///  - secondary block reward
///  - proposals reward
///  - transactions fees
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L262-271)
```rust
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

**File:** rpc/src/module/miner.rs (L260-298)
```rust
    fn submit_block(&self, work_id: String, block: Block) -> Result<H256> {
        let block: packed::Block = block.into();
        let block: Arc<core::BlockView> = Arc::new(block.into_view());
        let header = block.header();
        debug!(
            "start to submit block, work_id = {}, block = #{}({})",
            work_id,
            block.number(),
            block.hash()
        );

        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();

        // Verify header
        HeaderVerifier::new(snapshot, consensus)
            .verify(&header)
            .map_err(|err| handle_submit_error(&work_id, &err))?;
        if self
            .shared
            .snapshot()
            .get_block_header(&block.parent_hash())
            .is_none()
        {
            let err = format!(
                "Block parent {} of {}-{} not found",
                block.parent_hash(),
                block.number(),
                block.hash()
            );

            return Err(handle_submit_error(&work_id, &err));
        }

        // Verify and insert block
        let is_new = self
            .chain
            .blocking_process_block(Arc::clone(&block))
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

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

**File:** util/app-config/src/configs/miner.rs (L19-30)
```rust
pub struct ClientConfig {
    /// CKB node RPC endpoint.
    pub rpc_url: String,
    /// The poll interval in seconds to get work from the CKB node.
    pub poll_interval: u64,
    /// By default, miner submits a block and continues to get the next work.
    ///
    /// When this is enabled, miner will block until the submission RPC returns.
    pub block_on_submit: bool,
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** util/jsonrpc-types/src/block_template.rs (L72-76)
```rust
    /// Provided cellbase transaction template.
    ///
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
```
