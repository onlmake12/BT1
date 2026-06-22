### Title
Sync Layer `HeaderAcceptor::version_check` Hardcodes `version != 0` Rejection Without Consulting rfc_0048 Hardfork State — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::version_check` unconditionally rejects any header with `version != 0` and permanently marks it `BLOCK_INVALID`, regardless of whether rfc_0048 has activated. The hardfork flag `is_remove_header_version_reservation_rule_enabled` is defined in `util/types/src/core/hardfork/ckb2023.rs` but is **never called anywhere** in the codebase. The consensus-layer verifiers (`BlockVerifier`, `HeaderVerifier`) do **not** check header version at all, so the sync layer's hardcoded check is the only guard — and it is epoch-blind.

---

### Finding Description

**`version_check` — hardcoded, no hardfork awareness:** [1](#0-0) 

The check `self.header.version() != 0` is unconditional. It has no access to the consensus object, the current epoch, or the hardfork switch.

**`accept()` — permanently marks the header `BLOCK_INVALID`:** [2](#0-1) 

On failure, `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)` is called. This is a permanent, non-recoverable status.

**`is_remove_header_version_reservation_rule_enabled` — defined but never called:** [3](#0-2) 

A grep across the entire repository finds this symbol only at its definition site. No production code ever calls it to gate the version check.

**`HeaderVerifier::verify()` — does not check header version:** [4](#0-3) 

Only PoW, parent, number, epoch, and timestamp are verified. A header with `version=1` and valid PoW passes `non_contextual_check` cleanly and reaches `version_check`.

**`BlockVerifier::verify()` — does not check header version:** [5](#0-4) 

The non-contextual block verifier checks proposals limit, block bytes, cellbase, duplicates, and merkle root — no version field check.

**Call sequence in `accept()`:** [6](#0-5) 

Order: `prev_block_check` → `non_contextual_check` (passes for version=1 with valid PoW) → `version_check` (rejects, marks `BLOCK_INVALID`).

---

### Impact Explanation

A peer relaying a `SendHeaders` message containing a header with `version=1` and valid PoW, at an epoch past rfc_0048 activation, causes the receiving node to:

1. Pass PoW and all consensus checks in `non_contextual_check`.
2. Fail `version_check` due to the hardcoded `!= 0` guard.
3. Permanently write `BLOCK_INVALID` for that header hash.
4. Propagate `BLOCK_INVALID` to all descendants via `prev_block_check`.

The node is permanently unable to sync any chain tip descending from that header. The status is never cleared.

---

### Likelihood Explanation

The rfc_0048 hardfork removes the version reservation rule, meaning post-fork consensus **permits** non-zero header versions. However, in practice, no miner on mainnet currently produces `version != 0` headers because versionbits signaling was moved to the cellbase witness: [7](#0-6) 

The old header-version-based signaling is commented out. So the bug is latent: it would only trigger if a miner deliberately sets `version != 0` post-rfc_0048, or if a future soft fork reintroduces header-version signaling. The attack path is concrete and locally testable, but the realistic probability of triggering it on mainnet today is low.

---

### Recommendation

In `HeaderAcceptor::version_check`, gate the check on the hardfork state. The `HeaderAcceptor` already holds a `verifier` that has access to `consensus`. The fix is to skip the `version != 0` rejection when `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(header.epoch().number())` returns `true`. The `is_remove_header_version_reservation_rule_enabled` method already exists and is correct — it simply needs to be called.

---

### Proof of Concept

```
1. Configure a dev node with rfc_0048 activated at epoch 0
   (CKB2023::new_dev_default() sets rfc_0048 = 0).

2. Build a HeaderView with version=1, epoch > 0, valid PoW
   (use HeaderBuilder::version(1u32.pack()), solve Eaglesong).

3. Construct a HeaderAcceptor for that header against the dev node's
   SyncShared (which has rfc_0048-enabled consensus).

4. Call acceptor.accept().

5. Assert: result.state == ValidationState::Invalid
            AND shared.get_block_status(header.hash())
                       .contains(BlockStatus::BLOCK_INVALID)

6. This demonstrates that a post-rfc_0048 header with version=1
   is permanently rejected by the sync layer despite being valid
   under the activated consensus rules.
```

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

**File:** sync/src/synchronizer/headers_process.rs (L295-358)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
            let header_index = sync_shared
                .get_header_index_view(
                    &self.header.hash(),
                    status.contains(BlockStatus::BLOCK_STORED),
                )
                .unwrap_or_else(|| {
                    panic!(
                        "header {}-{} with HEADER_VALID should exist",
                        self.header.number(),
                        self.header.hash()
                    )
                })
                .as_header_index();
            state
                .peers()
                .may_set_best_known_header(self.peer, header_index);
            return result;
        }

        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

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

        sync_shared.insert_valid_header(self.peer, self.header);
        result
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

**File:** spec/src/versionbits/mod.rs (L440-455)
```rust
    fn condition<I: VersionbitsIndexer>(&self, header: &HeaderView, indexer: &I) -> bool {
        if let Some(cellbase) = indexer.cellbase(&header.hash())
            && let Some(witness) = cellbase.witnesses().get(0)
            && let Ok(reader) = CellbaseWitnessReader::from_slice(&witness.raw_data())
        {
            let message = reader.message().to_entity();
            if message.len() >= 4
                && let Ok(raw) = message.raw_data()[..4].try_into()
            {
                let version = u32::from_le_bytes(raw);
                return ((version & VERSIONBITS_TOP_MASK) == VERSIONBITS_TOP_BITS)
                    && (version & self.mask()) != 0;
            }
        }
        false
    }
```
