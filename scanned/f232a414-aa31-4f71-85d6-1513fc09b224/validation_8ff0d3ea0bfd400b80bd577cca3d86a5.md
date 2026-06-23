The vulnerability is real. Let me trace through the exact logic.

### Title
`BLOCK_INVALID` Header Bypass in `HeaderAcceptor::accept` Corrupts Sync State — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept` guards against re-processing known headers by checking `status.contains(BlockStatus::HEADER_VALID)`. Because `BLOCK_INVALID` (`1 << 12 = 4096`) and `HEADER_VALID` (`1`) are completely orthogonal bits, a header already marked `BLOCK_INVALID` falls through all three sub-checks and reaches `insert_valid_header`, inserting the invalid header into `header_map`, updating the peer's `best_known_header`, and potentially updating the global `shared_best_header` to an invalid chain tip. The developers explicitly acknowledged this with a `// FIXME` comment at the exact location.

---

### Finding Description

**Bit-flag orthogonality:**

```
HEADER_VALID  = 0b0000_0000_0001  (bit 0)
BLOCK_INVALID = 0b1_0000_0000_0000  (bit 12)
```

`BLOCK_INVALID.contains(HEADER_VALID)` = `(4096 & 1) != 0` = **false**. [1](#0-0) 

**The unguarded path in `accept()`:** [2](#0-1) 

The `// FIXME` comment at line 301 is a developer-acknowledged gap. When `status == BLOCK_INVALID`, the `HEADER_VALID` branch is not taken, and execution falls through to:

1. `prev_block_check` — checks the **parent's** status, not H itself. [3](#0-2) 
2. `non_contextual_check` — runs `HeaderVerifier::verify`, which only checks header-level fields (PoW, timestamp, epoch). [4](#0-3) 
3. `version_check` — checks `version == 0`. [5](#0-4) 
4. If all pass: `insert_valid_header` is called. [6](#0-5) 

**How a header gets `BLOCK_INVALID` while its header fields remain valid:**

In `chain/src/verify.rs`, when full block verification fails (invalid transactions, script execution failure, etc.), the block is marked `BLOCK_INVALID`: [7](#0-6) 

The header itself passed PoW/timestamp/version checks before the block body was verified. So `non_contextual_check` (which only re-runs `HeaderVerifier`) will pass again.

**What `insert_valid_header` does:** [8](#0-7) 

It:
- Inserts the header into `header_map` (line 1129)
- Calls `may_set_best_known_header` (line 1132) — updates the peer's best known header to the invalid chain tip
- Calls `may_set_shared_best_header` (line 1140) — potentially updates the **global** shared best header to the invalid chain tip

Note: `insert_valid_header` does NOT call `insert_block_status`, so `block_status_map` retains `BLOCK_INVALID`. `get_block_status` still returns `BLOCK_INVALID` (the map takes priority over `header_map`), meaning the bug is **repeatable** on every `SendHeaders` message containing H. [9](#0-8) 

---

### Impact Explanation

- **`header_map` pollution**: The invalid header is inserted into the in-memory header map, consuming memory and potentially being used as a parent anchor for further header chains.
- **`best_known_header` corruption**: The peer's best known header is updated to an invalid chain tip, causing the block fetcher to schedule downloads of blocks building on an invalid chain.
- **`shared_best_header` corruption**: If the invalid chain has higher total difficulty, the global shared best header is updated to an invalid tip, disrupting sync decisions for all peers.
- **Repeated exploitation**: Because `block_status_map` still holds `BLOCK_INVALID`, `get_block_status` returns `BLOCK_INVALID` on every call, so the attacker can re-trigger this on every `SendHeaders` message, causing repeated `insert_valid_header` calls.
- **Not a consensus violation**: The actual chain state (UTXO set, tip) is not corrupted. The node will not accept the invalid block into its chain.

---

### Likelihood Explanation

The attack requires only an unprivileged P2P connection. The attacker must:
1. Craft a block with a valid header (valid PoW, timestamp, version) but invalid body (e.g., a script that fails execution, or a transaction with an invalid signature).
2. Send it to the target node and wait for it to be marked `BLOCK_INVALID`.
3. Send a `SendHeaders` message containing that header.

Step 1 requires valid PoW, which is the primary cost barrier. However, on a low-difficulty testnet or during IBD when the node is syncing a low-difficulty chain, this is feasible. On mainnet, the PoW cost is significant but not infinite — a single valid-header/invalid-body block is sufficient to enable repeated exploitation.

---

### Recommendation

Add an explicit `BLOCK_INVALID` early-return guard at the top of `accept()`, immediately after the status check, resolving the `// FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a new variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

The `CompactBlockProcess` path already handles this correctly as a reference: [10](#0-9) 

---

### Proof of Concept

1. Pre-insert `BLOCK_INVALID` status for header hash H in `block_status_map` (simulating a prior failed full-block verification).
2. Call `HeadersProcess::execute` with a `SendHeaders` message containing H as the first element, where H has a valid parent (not `BLOCK_INVALID`), valid PoW/timestamp, and version 0.
3. Assert that `accept()` returns `ValidationState::Valid` (the bug: it should return `Invalid`).
4. Assert that `header_map` now contains H (the bug: it should not).
5. Assert that `may_set_best_known_header` was called with H's index (the bug: it should not be).

The `CompactBlockProcess` test `test_in_block_status_map` demonstrates the correct behavior for the relay path and can serve as a template: [11](#0-10) 

The analogous test for `HeadersProcess` would fail (i.e., `accept()` would return `Valid` instead of `Invalid`) due to the missing `BLOCK_INVALID` guard.

### Citations

**File:** shared/src/block_status.rs (L9-17)
```rust
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
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

**File:** sync/src/synchronizer/headers_process.rs (L255-284)
```rust
    pub fn non_contextual_check(&self, state: &mut ValidationResult) -> Result<(), bool> {
        self.verifier.verify(self.header).map_err(|error| {
            debug!(
                "HeadersProcess accepted {:?} error {:?}",
                self.header.number(),
                error
            );
            // UnknownParentError surfaces as BlockError(UnknownParent), not
            // HeaderError.  Missing parent is a recoverable ordering/context
            // issue, not proof that this header is invalid.
            if error
                .downcast_ref::<BlockError>()
                .is_some_and(|e| e.kind() == BlockErrorKind::UnknownParent)
            {
                state.temporary_invalid(Some(ValidationError::Verify(error)));
                false
            } else if let Some(header_error) = error.downcast_ref::<HeaderError>() {
                if header_error.is_too_new() {
                    state.temporary_invalid(Some(ValidationError::Verify(error)));
                    false
                } else {
                    state.invalid(Some(ValidationError::Verify(error)));
                    true
                }
            } else {
                state.invalid(Some(ValidationError::Verify(error)));
                true
            }
        })
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

**File:** sync/src/synchronizer/headers_process.rs (L301-322)
```rust
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
```

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** chain/src/verify.rs (L175-178)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
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

**File:** shared/src/shared.rs (L425-445)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
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
                }
            }
        }
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/relayer/tests/compact_block_process.rs (L60-71)
```rust
    // BLOCK_INVALID in block_status_map
    {
        relayer
            .shared
            .shared()
            .insert_block_status(block.header().hash(), BlockStatus::BLOCK_INVALID);
    }

    assert_eq!(
        rt.block_on(compact_block_process.execute()),
        StatusCode::BlockIsInvalid.into(),
    );
```
