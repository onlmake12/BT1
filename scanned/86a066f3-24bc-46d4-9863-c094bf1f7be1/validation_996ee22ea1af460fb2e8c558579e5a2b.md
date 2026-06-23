### Title
`HeaderAcceptor::accept()` Skips Early Rejection of Known-Invalid Headers, Allowing Re-insertion as `HEADER_VALID` - (File: `sync/src/synchronizer/headers_process.rs`)

### Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` noting that a header with `BLOCK_INVALID` status should trigger an early return, but no such guard is implemented. An unprivileged P2P peer can send a `SendHeaders` message containing a header previously marked `BLOCK_INVALID` (e.g., because the corresponding block failed contextual verification). If the header passes the remaining header-level checks, it is re-inserted as `HEADER_VALID` via `insert_valid_header`, corrupting the node's shared best header and peer best-known-header state.

### Finding Description

In `HeaderAcceptor::accept()`, the status of the incoming header is fetched at line 303:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best known and return
    return result;
}
``` [1](#0-0) 

`BlockStatus::BLOCK_INVALID` is defined as `1 << 12`, a completely separate bit from `HEADER_VALID` (`1`). [2](#0-1) 

Because `BLOCK_INVALID` does not contain `HEADER_VALID`, the early-return branch is not taken. Execution falls through to `prev_block_check` (which only checks whether the *parent* is `BLOCK_INVALID`, not the header itself), then `non_contextual_check` (PoW + number + epoch + timestamp), then `version_check`. [3](#0-2) 

If all three pass — which they will for any header whose block failed *contextual* (not structural) verification — `insert_valid_header` is called: [4](#0-3) 

`insert_valid_header` adds the header to `header_map`, calls `may_set_best_known_header` for the peer, and calls `may_set_shared_best_header`, potentially updating the node's global view of the best chain to a header backed by an invalid block. [5](#0-4) 

The `BLOCK_INVALID` status is set in `block_status_map` (in-memory) when block contextual verification fails, and persists in the store as `block_ext.verified = Some(false)` across restarts. [6](#0-5) [7](#0-6) 

### Impact Explanation

An attacker who knows about a block that failed contextual verification on the target node (e.g., a block with a valid PoW header but invalid transactions — double-spend, failing script, invalid DAO data) can send a `SendHeaders` P2P message containing that block's header. The header will be accepted as `HEADER_VALID`, and:

1. The peer's `best_known_header` is updated to this invalid block's header.
2. If the invalid block's total difficulty exceeds the current shared best, `shared_best_header` is updated globally, corrupting the node's fork-choice view.
3. The node may attempt to download blocks up to this height from the attacker's peer, wasting bandwidth and CPU.

The node's actual chain state is not corrupted (the block itself remains `BLOCK_INVALID` in the store), but its sync state machine is misled.

### Likelihood Explanation

Medium. The attacker does not need 51% hashpower. They need only:
- Knowledge of a block that passed PoW and header checks but failed contextual verification on the target node (e.g., a block with an invalid script or double-spend that was received and stored before being rejected by `ConsumeUnverifiedBlockProcessor`).
- The ability to send a `SendHeaders` P2P message — available to any unprivileged peer.

Such blocks arise naturally during chain reorganizations or can be deliberately crafted by any miner with enough hashpower to produce a single valid-PoW block.

### Recommendation

Implement the early return that the `FIXME` comment calls for. When `status == BLOCK_INVALID`, `accept()` should immediately set the result to `Invalid` and return, without proceeding to `prev_block_check`, `non_contextual_check`, or `version_check`. The appropriate error type is `ValidationError::InvalidParent` or a new `ValidationError::KnownInvalid` variant.

```rust
// Resolve the FIXME: reject known-invalid headers immediately
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
```

### Proof of Concept

1. Attacker mines block `B` with a valid Eaglesong PoW header but containing an invalid transaction (e.g., a double-spend or a script that fails execution).
2. Block `B` is relayed to the target node. It passes `non_contextual_verify` in `chain_service.rs` and is stored. `ConsumeUnverifiedBlockProcessor::verify_block` fails contextual verification; `B` is marked `BLOCK_INVALID` in `block_status_map` and `block_ext.verified = Some(false)` in the store.
3. Attacker sends a `SendHeaders` P2P message containing `B`'s header.
4. `HeadersProcess::execute()` calls `accept_first` or the loop body for `B`'s header, invoking `HeaderAcceptor::accept()`.
5. `get_block_status(&B.hash())` returns `BLOCK_INVALID`. The `HEADER_VALID` branch is not taken (no early return — the FIXME).
6. `prev_block_check`: `B`'s parent is valid → passes.
7. `non_contextual_check` via `HeaderVerifier::verify`: PoW valid, number/epoch/timestamp valid → passes.
8. `version_check`: version == 0 → passes.
9. `insert_valid_header` is called: `B`'s header enters `header_map`; `may_set_best_known_header` and `may_set_shared_best_header` are updated to `B`'s header index.
10. The node's shared best header now points to an invalid block, corrupting sync decisions for all peers. [8](#0-7) [9](#0-8)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L154-179)
```rust
        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L244-253)
```rust
    pub fn prev_block_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.active_chain.contains_block_status(
            &self.header.data().raw().parent_hash(),
            BlockStatus::BLOCK_INVALID,
        ) {
            state.invalid(Some(ValidationError::InvalidParent));
            return Err(());
        }
        Ok(())
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

**File:** shared/src/block_status.rs (L9-16)
```rust
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```

**File:** chain/src/verify.rs (L175-181)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```

**File:** shared/src/shared.rs (L432-441)
```rust
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
```
