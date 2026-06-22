### Title
Miner Block Reward Permanently Burned When Below Minimum Cell Capacity — (`tx-pool/src/block_assembler/mod.rs`, `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

When the total block reward (primary + secondary + tx fees + proposal reward) falls below the minimum occupied capacity required to create a `CellOutput` with the target miner's lock script, the protocol mandates a cellbase with **no output**. The reward shannons are permanently burned — there is no accumulation mechanism, no carry-forward, and no alternative payout path. This is the direct CKB analog of the Dahlia `payable`-on-`withdraw` pattern: value is accepted by the protocol but silently discarded with no retrieval path.

---

### Finding Description

In `build_cellbase` (`tx-pool/src/block_assembler/mod.rs`), the block assembler computes the reward and the target lock, builds a candidate `CellOutput`, then checks:

```rust
let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;
if no_finalization_target || insufficient_reward_to_create_cell {
    tx_builder.build()   // cellbase with ZERO outputs — reward is dropped
} else {
    tx_builder.output(output).output_data(Bytes::default()).build()
}
``` [1](#0-0) 

The consensus verifier `RewardVerifier::verify` mirrors this exactly: when `insufficient_reward_to_create_cell` is true it **requires** the cellbase to have no outputs, and accepts the block as valid:

```rust
if no_finalization_target || insufficient_reward_to_create_cell {
    let ret = if cellbase.transaction.outputs().is_empty() {
        Ok(())
    } else {
        Err((CellbaseError::InvalidRewardTarget).into())
    };
    return ret;
}
``` [2](#0-1) 

The `is_lack_of_capacity` check computes the full occupied capacity of the output cell — 8 bytes for the capacity field, plus the serialized lock script (32-byte `code_hash` + 1-byte `hash_type` + `args` length). For a standard secp256k1 lock (20-byte args) this is 61 bytes = **6,100,000,000 shannons (61 CKB)**. Any total reward below that threshold triggers the burn path. [3](#0-2) 

The `InsufficientReward` integration test explicitly exercises and confirms this path — a block with an empty-output cellbase is accepted when the reward is insufficient: [4](#0-3) 

---

### Impact Explanation

Any block reward that falls below the minimum occupied capacity of the miner's lock-script cell is **permanently and irrecoverably burned**. The shannons are neither paid to the miner, nor accumulated for a future block, nor redirected to the NervosDAO treasury. They simply cease to exist in the UTXO set. This is a direct, protocol-enforced loss of miner compensation with no opt-out or workaround available to the miner.

A miner using a larger-than-minimum lock script (e.g., a multisig or custom script with a large `args` field) faces a higher threshold and will hit this burn path sooner than a miner using a minimal lock.

---

### Likelihood Explanation

The primary epoch reward halves every `primary_epoch_reward_halving_interval` epochs (8760 on mainnet, ~4 years). After sufficient halvings the primary reward per block approaches zero. The secondary reward does not halve but is proportional to the fraction of CKB held outside the NervosDAO; as more CKB is locked in the DAO, the miner's share of secondary issuance also shrinks. Transaction fees are variable and can be zero. The combination of these three factors makes the `insufficient_reward_to_create_cell` condition reachable on a long enough time horizon on mainnet, and immediately reachable on any chain with aggressive halving parameters (as the `InsufficientReward` test spec demonstrates). [5](#0-4) 

---

### Recommendation

Introduce a carry-forward accumulator for sub-threshold rewards. When `insufficient_reward_to_create_cell` is true, the reward shannons should be added to a persistent counter (stored in the chain state or a dedicated cell) and paid out in the next block whose cumulative reward exceeds the minimum cell capacity. This mirrors how Bitcoin handles dust-level coinbase rewards and prevents permanent loss of miner compensation.

---

### Proof of Concept

1. Configure a chain with aggressive halving: `primary_epoch_reward_halving_interval = 2`, `genesis_epoch_length = 20` (as in the `InsufficientReward` spec).
2. Mine past the halving point until `RewardCalculator::block_reward_to_finalize` returns a `total` below 61 CKB (6,100,000,000 shannons).
3. Call `get_block_template` via RPC. The returned cellbase will have zero outputs.
4. Submit the block via `submit_block`. `RewardVerifier` accepts it with `Ok(())`.
5. Observe via `get_block_economic_state` that the block reward is non-zero, yet the cellbase output is empty — the shannons are permanently gone from the UTXO set with no corresponding output anywhere on chain.

Root cause entry points:
- `build_cellbase` — `tx-pool/src/block_assembler/mod.rs` line 550–552 [6](#0-5) 
- `RewardVerifier::verify` — `verification/contextual/src/contextual_block_verifier.rs` line 247–256 [7](#0-6)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L550-558)
```rust
            let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;
            if no_finalization_target || insufficient_reward_to_create_cell {
                tx_builder.build()
            } else {
                tx_builder
                    .output(output)
                    .output_data(Bytes::default())
                    .build()
            }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L247-256)
```rust
        let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;

        if no_finalization_target || insufficient_reward_to_create_cell {
            let ret = if cellbase.transaction.outputs().is_empty() {
                Ok(())
            } else {
                Err((CellbaseError::InvalidRewardTarget).into())
            };
            return ret;
        }
```

**File:** util/gen-types/src/extension/capacity.rs (L29-50)
```rust
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

    /// Returns if the [`capacity`] in `CellOutput` is smaller than the [`occupied capacity`].
    ///
    /// [`capacity`]: #method.capacity
    /// [`occupied capacity`]: #method.occupied_capacity
    pub fn is_lack_of_capacity(&self, data_capacity: Capacity) -> CapacityResult<bool> {
        self.occupied_capacity(data_capacity)
            .map(|cap| cap > self.capacity().into())
    }
```

**File:** test/src/specs/consensus/insufficient_reward.rs (L14-38)
```rust
impl Spec for InsufficientReward {
    fn before_run(&self) -> Vec<Node> {
        let mut node = Node::new(spec_name(self), "node1");

        // modify chain spec
        node.modify_chain_spec(|spec| {
            spec.params.initial_primary_epoch_reward = Some(Capacity::shannons(2000_00000000));
            spec.params.secondary_epoch_reward = Some(Capacity::shannons(100_00000000));
            spec.params.primary_epoch_reward_halving_interval = Some(2);
            spec.params.epoch_duration_target = Some(80);
            spec.params.genesis_epoch_length = Some(20);
        });

        // import vendor data
        let data_path = VENDOR_PATH
            .lock()
            .join("consensus")
            .join("insufficient_reward.json")
            .to_string_lossy()
            .to_string();
        node.import(data_path);

        node.start();
        vec![node]
    }
```

**File:** test/src/specs/consensus/insufficient_reward.rs (L72-87)
```rust
        // build a block with empty reward
        let new_block = new_block_builder.build();
        let cellbase = &new_block.transactions()[0];
        let result = node
            .rpc_client()
            .submit_block("".to_owned(), new_block.data().into());

        assert!(
            cellbase.outputs().is_empty(),
            "Cellbase output should be empty"
        );
        assert!(
            result.is_ok(),
            "Empty reward block should be submitted successfully, but not"
        )
    }
```
