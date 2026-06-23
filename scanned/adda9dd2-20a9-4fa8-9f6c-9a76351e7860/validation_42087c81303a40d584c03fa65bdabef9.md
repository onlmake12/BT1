The code evidence is now complete. Here is the full analysis:

---

### Title
Compact Block Path Skips `version_check()`, Allowing a Mined Version=1 Block to Cause Consensus Deviation — (`sync/src/relayer/compact_block_process.rs`)

### Summary

`HeaderAcceptor::version_check()` is called only in the sync-headers path. The compact block (`RelayV3`) path calls `HeaderVerifier::verify()` (which checks PoW, number, epoch, timestamp — but **not** block version) and never calls `version_check()`. Because neither `BlockVerifier` nor `ContextualBlockVerifier` checks the header version either, a miner who produces a valid-PoW block with `version=1` can have it fully accepted by nodes that receive it via compact block, while nodes that receive the same header via the sync-headers path mark it `BLOCK_INVALID`. This is a concrete, order-of-receipt-dependent consensus split.

### Finding Description

**`HeaderVerifier::verify()` does not check block version.** [1](#0-0) 

**`HeaderAcceptor::version_check()` is the sole version gate — it rejects any header with `version != 0` and writes `BLOCK_INVALID`.** [2](#0-1) 

**The sync-headers path calls both `non_contextual_check()` (→ `HeaderVerifier::verify()`) and `version_check()`.** [3](#0-2) 

**The compact block path's `contextual_check()` calls only `header_verifier.verify()` — `version_check()` is never invoked.** [4](#0-3) 

After passing `contextual_check()`, the compact block path immediately marks the header `HEADER_VALID` and calls `accept_block`: [5](#0-4) [6](#0-5) 

**`accept_block` → chain service `non_contextual_verify()` calls only `BlockVerifier` and `NonContextualBlockTxsVerifier` — neither checks header version.** [7](#0-6) [8](#0-7) 

**`ContextualBlockVerifier::verify()` also has no header version check.** [9](#0-8) 

The compact block path does check `BLOCK_INVALID` status before proceeding, so order of receipt matters: [10](#0-9) 

### Impact Explanation

A miner who produces a valid-PoW block with `header.version = 1` and broadcasts it:

- To peers via **RelayV3 compact block**: the block passes all checks and is fully committed to the chain (`HEADER_VALID` → `BLOCK_STORED`).
- To peers via **Sync `SendHeaders`**: the header is rejected at `version_check()` and written as `BLOCK_INVALID`.

Nodes in the first group build on top of the version=1 block. Nodes in the second group refuse to extend it and treat any descendant as having an invalid parent. This is a **persistent consensus split** between two sets of honest nodes, triggered purely by which P2P message arrived first.

### Likelihood Explanation

Any permissionless miner can produce a single valid-PoW block with `version=1`. This does not require majority hashpower — only the ability to mine one block. The "miner/block-template paths" entry point is explicitly in scope. The differential is deterministic and locally reproducible.

### Recommendation

Add a `version_check` (or equivalent inline guard) inside `contextual_check()` in `compact_block_process.rs`, immediately after `header_verifier.verify()` succeeds, mirroring the check in `HeaderAcceptor::accept()`:

```rust
if compact_block_header.version() != 0 {
    shared.shared().insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    return StatusCode::CompactBlockHasInvalidHeader.with_context(...);
}
```

Alternatively, move `version_check` into `HeaderVerifier::verify()` so it is enforced uniformly across all ingestion paths.

### Proof of Concept

1. Run two nodes, A and B, connected to each other.
2. Mine a block with `header.version = 1` and valid PoW.
3. Send it to node A via a `RelayV3` compact block message.
4. Send the same header to node B via a `Sync` `SendHeaders` message.
5. Assert: node A has the block in its canonical chain (`BLOCK_STORED`); node B has the hash in `BLOCK_INVALID` state.
6. Observe that node A continues building on the version=1 block while node B rejects all descendants — a confirmed consensus split.

### Citations

**File:** verification/src/header_verifier.rs (L32-50)
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
```

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

**File:** sync/src/synchronizer/headers_process.rs (L334-354)
```rust
        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

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

**File:** sync/src/relayer/compact_block_process.rs (L77-78)
```rust
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** sync/src/relayer/compact_block_process.rs (L118-119)
```rust
                self.relayer
                    .accept_block(Arc::clone(&self.nc), self.peer, block, "CompactBlock");
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L324-339)
```rust
    let header_verifier = HeaderVerifier::new(&median_time_context, shared.consensus());
    if let Err(err) = header_verifier.verify(compact_block_header) {
        if err
            .downcast_ref::<HeaderError>()
            .map(|e| e.is_too_new())
            .unwrap_or(false)
        {
            return Status::ignored();
        } else {
            shared
                .shared()
                .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
            return StatusCode::CompactBlockHasInvalidHeader
                .with_context(format!("{block_hash} {err}"));
        }
    }
```

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

**File:** verification/src/block_verifier.rs (L39-47)
```rust
    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
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
