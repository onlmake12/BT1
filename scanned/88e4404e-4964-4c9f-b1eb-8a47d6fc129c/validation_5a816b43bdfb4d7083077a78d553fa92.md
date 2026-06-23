The key question is whether a block with an invalid PoW nonce can pass through the chain's verification pipeline and reach `notify_new_uncle`. Let me trace the exact code path.

### Title
Uncle Candidate Pool Poisoning via Invalid-PoW Block Relay — (`tx-pool/src/block_assembler/candidate_uncles.rs`, `chain/src/verify.rs`)

### Summary

An unprivileged P2P peer can relay a structurally valid block with an invalid PoW nonce. Because `BlockVerifier` (the non-contextual verifier) performs no PoW check, and because non-best-chain blocks bypass `ContextualBlockVerifier` entirely, the block passes all chain-side checks and triggers `notify_new_uncle`. `receive_candidate_uncle` and `CandidateUncles::insert` also perform no PoW check. The poisoned uncle then appears in block templates produced by `prepare_uncles`. When a miner submits a block built from that template, `UnclesVerifier::verify` calls `pow_engine.verify(&uncle.data().header())`, returns false, and the block is rejected with `PowError::InvalidNonce`. The invalid uncle is never evicted from the pool on rejection, so the template remains poisoned until the uncle ages out of the epoch window.

---

### Finding Description

**Step 1 — Non-contextual verification skips PoW.**

`ChainService::non_contextual_verify` calls only `BlockVerifier::verify`: [1](#0-0) 

`BlockVerifier::verify` checks proposals limit, bytes, cellbase, duplicates, and merkle root — no PoW: [2](#0-1) 

**Step 2 — Non-best blocks skip contextual verification entirely.**

In `verify_block`, the `new_best_block` branch calls `reconcile_main_chain` → `ContextualBlockVerifier`. The else branch (non-best block) only inserts the block ext and immediately calls `notify_new_uncle`: [3](#0-2) [4](#0-3) 

**Step 3 — `receive_candidate_uncle` inserts without PoW check.** [5](#0-4) 

**Step 4 — `CandidateUncles::insert` stores the uncle unconditionally.** [6](#0-5) 

**Step 5 — `prepare_uncles` emits the uncle with no PoW check.**

The only filters are epoch number, compact target, block number, and parent-chain membership: [7](#0-6) 

**Step 6 — `UnclesVerifier::verify` rejects the uncle at block-submission time.** [8](#0-7) 

The `PowError::InvalidNonce` error type is defined at: [9](#0-8) 

---

### Impact Explanation

A miner with block assembler enabled will repeatedly produce blocks that are rejected by every peer's `UnclesVerifier`. The invalid uncle is never removed from `CandidateUncles` on rejection (there is no feedback path from block rejection back to the uncle pool). It persists until the epoch boundary evicts it or it is displaced by newer uncles (pool cap is 128). An attacker who continuously relays fresh invalid-PoW blocks at the same height can keep the pool saturated, causing sustained miner DoS: every template includes a poisoned uncle, every submitted block is rejected, and the miner's PoW is wasted.

---

### Likelihood Explanation

The attack requires only the ability to send a P2P `SendBlock` or compact block message with a structurally valid block (valid merkle root, valid cellbase, correct epoch/target fields) but an arbitrary nonce. No hashpower, no key material, and no privileged access are needed. The attacker does not need the block to extend the best chain — in fact, it must not. The structural validity requirements are trivially satisfiable by copying a real block and flipping the nonce.

---

### Recommendation

Add a PoW check in `receive_candidate_uncle` (or in `CandidateUncles::insert`) before accepting an uncle into the pool:

```rust
pub async fn receive_candidate_uncle(&self, uncle: UncleBlockView) {
    if let Some(ref block_assembler) = self.block_assembler {
        // Reject uncles that fail PoW before inserting
        let consensus = /* obtain consensus */;
        if !consensus.pow_engine().verify(&uncle.header().data()) {
            return;
        }
        block_assembler.candidate_uncles.lock().await.insert(uncle);
        // ...
    }
}
```

Alternatively, add the check inside `prepare_uncles` as a final filter before appending to the output vector, mirroring the check already present in `UnclesVerifier::verify`.

---

### Proof of Concept

```rust
// 1. Build a block with a valid structure but invalid nonce
let bad_uncle = BlockBuilder::default()
    .parent_hash(main_chain_tip.hash())
    .number(main_chain_tip.number() + 1)
    .compact_target(epoch.compact_target())
    .epoch(epoch.number_with_fraction(main_chain_tip.number() + 1))
    .nonce(0xdeadbeef_u128)   // invalid nonce
    .build()
    .as_uncle();

// 2. Insert directly (simulating notify_new_uncle path)
let mut candidate_uncles = CandidateUncles::new();
candidate_uncles.insert(bad_uncle.clone());

// 3. prepare_uncles returns it
let uncles = candidate_uncles.prepare_uncles(&snapshot, &epoch);
assert_eq!(uncles[0].hash(), bad_uncle.hash()); // uncle is present

// 4. UnclesVerifier rejects it
let block_with_uncle = BlockBuilder::default()
    .uncle(bad_uncle)
    .build();
let verifier = UnclesVerifier::new(uncle_verifier_context, &block_with_uncle);
assert!(matches!(
    verifier.verify().unwrap_err().downcast_ref::<PowError>(),
    Some(PowError::InvalidNonce)
));
```

### Citations

**File:** chain/src/chain_service.rs (L72-89)
```rust
    fn non_contextual_verify(&self, block: &BlockView) -> Result<(), Error> {
        let consensus = self.shared.consensus();
        BlockVerifier::new(consensus).verify(block).map_err(|e| {
            debug!("[process_block] BlockVerifier error {:?}", e);
            e
        })?;

        NonContextualBlockTxsVerifier::new(consensus)
            .verify(block)
            .map_err(|e| {
                debug!(
                    "[process_block] NonContextualBlockTxsVerifier error {:?}",
                    e
                );
                e
            })
            .map(|_| ())
    }
```

**File:** verification/src/block_verifier.rs (L36-48)
```rust
impl<'a> Verifier for BlockVerifier<'a> {
    type Target = BlockView;

    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
}
```

**File:** chain/src/verify.rs (L356-358)
```rust
        } else {
            db_txn.insert_block_ext(&block.header().hash(), &ext)?;
        }
```

**File:** chain/src/verify.rs (L421-425)
```rust
            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.notify_new_uncle(block.as_uncle()) {
                    error!("[verify block] notify new_uncle error {}", e);
                }
```

**File:** tx-pool/src/service.rs (L1210-1224)
```rust
    pub async fn receive_candidate_uncle(&self, uncle: UncleBlockView) {
        if let Some(ref block_assembler) = self.block_assembler {
            {
                block_assembler.candidate_uncles.lock().await.insert(uncle);
            }
            if self
                .block_assembler_sender
                .send(BlockAssemblerMessage::Uncle)
                .await
                .is_err()
            {
                error!("block_assembler receiver dropped");
            }
        }
    }
```

**File:** tx-pool/src/block_assembler/candidate_uncles.rs (L35-58)
```rust
    pub fn insert(&mut self, uncle: UncleBlockView) -> bool {
        let number: BlockNumber = uncle.header().number();
        if self.count >= MAX_CANDIDATE_UNCLES {
            let first_key = *self.map.keys().next().expect("length checked");
            if number > first_key {
                if let Some(set) = self.map.remove(&first_key) {
                    self.count -= set.len();
                }
            } else {
                return false;
            }
        }

        let set = self.map.entry(number).or_default();
        if set.len() < MAX_PER_HEIGHT {
            let ret = set.insert(uncle);
            if ret {
                self.count += 1;
            }
            ret
        } else {
            false
        }
    }
```

**File:** tx-pool/src/block_assembler/candidate_uncles.rs (L125-143)
```rust
        for uncle in self.values() {
            if uncles.len() == max_uncles_num {
                break;
            }
            let parent_hash = uncle.header().parent_hash();
            // we should keep candidate util next epoch
            if uncle.compact_target() != current_epoch_ext.compact_target()
                || uncle.epoch().number() != epoch_number
            {
                removed.push(uncle.clone());
            } else if !snapshot.is_main_chain(&uncle.hash())
                && !snapshot.is_uncle(&uncle.hash())
                && uncle.number() < candidate_number
                && (uncles.iter().any(|u| u.hash() == parent_hash)
                    || snapshot.is_main_chain(&parent_hash)
                    || snapshot.is_uncle(&parent_hash))
            {
                uncles.push(uncle.clone());
            }
```

**File:** verification/contextual/src/uncles_verifier.rs (L121-128)
```rust
            if !self
                .provider
                .consensus()
                .pow_engine()
                .verify(&uncle.data().header())
            {
                return Err((PowError::InvalidNonce).into());
            }
```

**File:** verification/src/error.rs (L278-284)
```rust
pub enum PowError {
    /// Error occurs during PoW verification.
    #[error(
        "InvalidNonce: please set logger.filter to \"info,ckb-pow=debug\" for detailed PoW verification information"
    )]
    InvalidNonce,
}
```
