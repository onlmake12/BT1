All cited code paths verified against the actual repository. Every claim checks out:

- `BlockVerifier::verify` confirmed to skip PoW [1](#0-0) 
- Non-best-block path confirmed to call `notify_new_uncle` directly without `ContextualBlockVerifier` [2](#0-1) [3](#0-2) 
- `receive_candidate_uncle` confirmed to insert with no PoW check [4](#0-3) 
- `CandidateUncles::insert` confirmed to have no PoW check [5](#0-4) 
- `prepare_uncles` confirmed to filter only on epoch, compact target, number, and parent-chain membership — no PoW [6](#0-5) 
- PoW check confirmed to exist only in `UnclesVerifier::verify` [7](#0-6) 

---

Audit Report

## Title
Uncle Candidate Pool Poisoning via Invalid-PoW Block Relay — (`tx-pool/src/block_assembler/candidate_uncles.rs`, `tx-pool/src/service.rs`)

## Summary
An unprivileged P2P peer can relay a structurally valid block with an invalid PoW nonce. Because `BlockVerifier` performs no PoW check and non-best-chain blocks bypass `ContextualBlockVerifier` entirely, the block reaches `receive_candidate_uncle` and is inserted into `CandidateUncles` without any PoW validation. The poisoned uncle then appears in every block template produced by `prepare_uncles`, causing `UnclesVerifier::verify` to reject every miner-submitted block with `PowError::InvalidNonce`. The invalid uncle is never evicted on rejection, so the pool remains poisoned until epoch boundary or natural displacement.

## Finding Description

**Step 1 — Non-contextual verification skips PoW.**

`ChainService::non_contextual_verify` calls `BlockVerifier::verify`, which checks only proposals limit, bytes, cellbase, duplicates, and merkle root — no PoW check is performed.

```
verification/src/block_verifier.rs L36-48
```

**Step 2 — Non-best blocks bypass contextual verification and reach `notify_new_uncle`.**

`new_best_block` is determined solely by total difficulty comparison at `chain/src/verify.rs:306`. The `else` branch for non-best blocks skips `reconcile_main_chain` (which calls `ContextualBlockVerifier`) and proceeds directly to `notify_new_uncle` at lines 421–425.

**Step 3 — `receive_candidate_uncle` inserts without PoW check.**

`tx-pool/src/service.rs:1210-1224` shows `receive_candidate_uncle` directly calls `candidate_uncles.lock().await.insert(uncle)` with no PoW validation.

**Step 4 — `CandidateUncles::insert` stores the uncle unconditionally.**

`tx-pool/src/block_assembler/candidate_uncles.rs:35-58` contains no PoW verification anywhere in the insert path.

**Step 5 — `prepare_uncles` emits the uncle with no PoW check.**

The only filters at lines 131–140 are epoch number, compact target, block number, and parent-chain membership. No PoW check.

**Step 6 — `UnclesVerifier::verify` rejects the uncle at block-submission time.**

The PoW check occurs only at `verification/contextual/src/uncles_verifier.rs:121-128`, after the miner has already expended PoW work.

There is no feedback path from block rejection back to `CandidateUncles` to evict the bad uncle. The pool cap is 128 entries (`MAX_CANDIDATE_UNCLES`), and an attacker who continuously relays fresh invalid-PoW blocks at the current height can keep all slots saturated.

## Impact Explanation
Miners with block assembler enabled will repeatedly produce block templates containing poisoned uncles. Every submitted block is rejected by every peer's `UnclesVerifier` with `PowError::InvalidNonce`. The miner's PoW is entirely wasted on each attempt. If the attacker sustains the relay of fresh invalid-PoW blocks, the miner cannot produce any valid block for the duration of the attack. At network scale, this constitutes sustained reduction of block production capacity with negligible attacker cost, matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The attack requires only the ability to send a P2P `SendBlock` message containing a structurally valid block (valid merkle root, valid cellbase, correct epoch/compact target fields) with an arbitrary nonce. No hashpower, no key material, and no privileged access are needed. The block must not extend the best chain — it must be a side-chain block — which is trivially achieved by copying a real block and flipping the nonce. The attacker can automate continuous relay of such blocks to keep the 128-slot pool saturated indefinitely.

## Recommendation
Add a PoW check in `receive_candidate_uncle` before inserting into the pool:

```rust
pub async fn receive_candidate_uncle(&self, uncle: UncleBlockView) {
    if let Some(ref block_assembler) = self.block_assembler {
        let consensus = self.snapshot().consensus();
        if !consensus.pow_engine().verify(&uncle.header().data()) {
            return;
        }
        block_assembler.candidate_uncles.lock().await.insert(uncle);
        // ...
    }
}
```

Alternatively, add the same check inside `CandidateUncles::insert` or as a final filter in `prepare_uncles`, mirroring the check already present in `UnclesVerifier::verify`.

## Proof of Concept

```rust
// 1. Craft a structurally valid block with an invalid nonce
let bad_uncle = BlockBuilder::default()
    .parent_hash(main_chain_tip.hash())
    .number(main_chain_tip.number() + 1)
    .compact_target(epoch.compact_target())
    .epoch(epoch.number_with_fraction(main_chain_tip.number() + 1))
    .nonce(0xdeadbeef_u128)   // invalid nonce — passes BlockVerifier, fails UnclesVerifier
    .build()
    .as_uncle();

// 2. Insert directly (simulating the notify_new_uncle path — no PoW check occurs)
let mut candidate_uncles = CandidateUncles::new();
candidate_uncles.insert(bad_uncle.clone());

// 3. prepare_uncles returns the poisoned uncle
let uncles = candidate_uncles.prepare_uncles(&snapshot, &epoch);
assert_eq!(uncles[0].hash(), bad_uncle.hash());

// 4. UnclesVerifier rejects the block at submission time, wasting miner PoW
let block_with_uncle = BlockBuilder::default().uncle(bad_uncle).build();
let verifier = UnclesVerifier::new(uncle_verifier_context, &block_with_uncle);
assert!(matches!(
    verifier.verify().unwrap_err().downcast_ref::<PowError>(),
    Some(PowError::InvalidNonce)
));

// 5. Bad uncle remains in pool — repeat steps 3-4 indefinitely
assert!(candidate_uncles.contains(&bad_uncle));
```

### Citations

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

**File:** chain/src/verify.rs (L306-306)
```rust
        let new_best_block = cannon_total_difficulty > current_total_difficulty;
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
