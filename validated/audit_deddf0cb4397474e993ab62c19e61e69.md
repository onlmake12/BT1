I need to trace both code paths carefully before rendering a verdict.

**Path 1: Sync layer — `HeaderAcceptor::version_check`** [1](#0-0) 

```rust
pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
    if self.header.version() != 0 {
        state.invalid(Some(ValidationError::Version));
        Err(())
    } else {
        Ok(())
    }
}
```

This is unconditional — no epoch check, no hardfork flag consulted. When it fails, `accept()` immediately writes `BLOCK_INVALID`: [2](#0-1) 

**Path 2: Chain verification — `HeaderVerifier::verify`**

The `HeaderVerifier` used in `non_contextual_check` checks only PoW, parent existence, block number, epoch, and timestamp. There is **no version field check**: [3](#0-2) 

**Path 3: Chain verification — `ContextualBlockVerifier::verify`**

The contextual verifier checks epoch, uncles, two-phase commit, DAO header, reward, block extension, and transactions. Again, **no version field check**: [4](#0-3) 

**Path 4: `is_remove_header_version_reservation_rule_enabled` usage**

The function is defined via macro in `ckb2023.rs`: [5](#0-4) 

A grep across all `.rs` files for `remove_header_version_reservation_rule` returns **only** the definition file. It is **not called in any verifier** — neither the sync-layer `HeaderAcceptor` nor the chain-layer `ContextualBlockVerifier` nor `HeaderVerifier` consults it.

---

### Divergence confirmed

| Path | Version check | Outcome for `version=1` block |
|---|---|---|
| `HeaderAcceptor` (SendHeaders) | Unconditional `!= 0` → `BLOCK_INVALID` | Permanently rejected |
| `HeaderVerifier` + `ContextualBlockVerifier` (chain) | None | Accepted |

The question's premise that "the full block verifier consults `is_remove_header_version_reservation_rule_enabled`" is actually **false** — the chain path has *no* version check at all, making the divergence even broader than described (it exists pre- and post-rfc_0048 activation).

---

### Title
Unconditional `version != 0` rejection in `HeaderAcceptor::version_check` diverges from chain verifier which performs no version check, enabling permanent consensus split — (`sync/src/synchronizer/headers_process.rs`)

### Summary
`HeaderAcceptor::version_check` unconditionally marks any block with `header.version != 0` as `BLOCK_INVALID` in the sync layer, while the chain verification path (`HeaderVerifier` + `ContextualBlockVerifier`) performs no version check whatsoever. A block with `version=1` and valid PoW will be accepted by nodes that process it via the chain path and permanently rejected by nodes that first encounter it via `SendHeaders`, causing an irrecoverable consensus split.

### Finding Description
In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::version_check` (line 286) rejects any header whose `version != 0` without consulting the hardfork epoch or `is_remove_header_version_reservation_rule_enabled`. On failure it calls `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)` (line 352), permanently poisoning that block hash on the receiving node.

Meanwhile, `HeaderVerifier::verify` (the verifier used in `non_contextual_check`) checks only PoW, parent, number, epoch, and timestamp — no version field. `ContextualBlockVerifier::verify` similarly has no version check. `is_remove_header_version_reservation_rule_enabled` is defined in `ckb2023.rs` but is never invoked in any production verifier.

The result: two nodes with identical chain state will permanently diverge if one receives a `version=1` block via `SendHeaders` and the other receives it via the block relay / chain path.

### Impact Explanation
Any node that processes the crafted block via the headers-sync path will permanently mark it `BLOCK_INVALID` and refuse to ever accept it or any descendant. Nodes that receive it via the block relay path will accept it and build on it. This is a hard consensus split matching the "Critical — consensus deviation" scope.

### Likelihood Explanation
The attacker needs to mine exactly one block with `version=1` and valid PoW. This does not require majority hashpower — any miner (or pool participant) can produce such a block. The attacker then submits it to a subset of peers via `SendHeaders` and to others via the normal block relay path. No privileged access, no key material, and no social engineering is required.

### Recommendation
`HeaderAcceptor::version_check` must be made epoch-aware. After `rfc_0048` activation it should permit `version != 0` (or be removed entirely if the chain path already enforces the correct rule). The simplest fix is to consult `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(header.epoch().number())` before rejecting, mirroring whatever rule the chain verifier applies. Ideally, the version check should live in a single shared location called by both paths.

### Proof of Concept
1. Run two CKB nodes (A and B) on a post-epoch-12293 chain (or a devnet with `rfc_0048 = 0`).
2. Mine a block with `header.version = 1` and valid PoW.
3. Send the block to node A via the normal `SendBlock` / compact-block relay path.
4. Send only the header to node B via `SendHeaders`.
5. Observe: node A accepts the block and advances its tip; node B marks the block hash `BLOCK_INVALID` and never advances past it.
6. Assert tip disagreement and that node B's `BLOCK_INVALID` flag is permanent (it will refuse the block even if later sent via `SendBlock`).

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L286-293)
```rust
    pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.header.version() != 0 {
            state.invalid(Some(ValidationError::Version));
            Err(())
        } else {
            Ok(())
        }
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L346-354)
```rust
        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }
```

**File:** verification/src/header_verifier.rs (L32-51)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
}
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L630-692)
```rust
    pub fn verify(
        &'a self,
        resolved: &'a [Arc<ResolvedTransaction>],
        block: &'a BlockView,
    ) -> Result<(Cycle, Vec<Completed>), Error> {
        let parent_hash = block.data().header().raw().parent_hash();
        let header = block.header();
        let parent = self
            .context
            .store
            .get_block_header(&parent_hash)
            .ok_or_else(|| UnknownParentError {
                parent_hash: parent_hash.clone(),
            })?;

        let epoch_ext = if block.is_genesis() {
            self.context.consensus.genesis_epoch_ext().to_owned()
        } else {
            self.context
                .consensus
                .next_epoch_ext(&parent, &self.context.store.borrow_as_data_loader())
                .ok_or_else(|| UnknownParentError {
                    parent_hash: parent.hash(),
                })?
                .epoch()
        };

        if !self.switch.disable_epoch() {
            EpochVerifier::new(&epoch_ext, block).verify()?;
        }

        if !self.switch.disable_uncles() {
            let uncle_verifier_context = UncleVerifierContext::new(&self.context, &epoch_ext);
            UnclesVerifier::new(uncle_verifier_context, block).verify()?;
        }

        if !self.switch.disable_two_phase_commit() {
            TwoPhaseCommitVerifier::new(&self.context, block).verify()?;
        }

        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }

        if !self.switch.disable_reward() {
            RewardVerifier::new(&self.context, resolved, &parent).verify()?;
        }

        if !self.switch.disable_extension() {
            BlockExtensionVerifier::new(&self.context, self.chain_root_mmr, &parent)
                .verify(block)?;
        }

        let ret = BlockTxsVerifier::new(
            self.context.clone(),
            header,
            self.handle,
            &self.txs_verify_cache,
            &parent,
        )
        .verify(resolved, self.switch.disable_script())?;
        Ok(ret)
    }
```

**File:** util/types/src/core/hardfork/ckb2023.rs (L66-73)
```rust
define_methods!(
    CKB2023,
    rfc_0048,
    remove_header_version_reservation_rule,
    is_remove_header_version_reservation_rule_enabled,
    disable_rfc_0048,
    "RFC PR 0048"
);
```
