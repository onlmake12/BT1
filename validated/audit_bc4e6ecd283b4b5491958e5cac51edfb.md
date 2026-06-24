Audit Report

## Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Enables Repeated Re-validation of Invalid Headers — (`sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::accept()` in `sync/src/synchronizer/headers_process.rs` contains a developer-acknowledged `// FIXME` comment noting the absence of an early-return guard for `BlockStatus::BLOCK_INVALID`. Because `BLOCK_INVALID = 1 << 12 = 4096` shares no bits with `HEADER_VALID = 1`, the existing `status.contains(BlockStatus::HEADER_VALID)` guard does not fire when a header's status is `BLOCK_INVALID`. A remote peer can repeatedly re-send headers the node has already marked invalid, causing the node to re-run `prev_block_check`, `non_contextual_check` (including PoW verification), and `version_check` on each re-send, and — for contextually-invalid but non-contextually-valid headers — to call `insert_valid_header`, inserting the header into `header_map` and updating the peer's `best_known_header`.

## Finding Description

**Root cause — bit-disjoint flags:**

`BlockStatus` is defined in `shared/src/block_status.rs` (L8–17): [1](#0-0) 

`BLOCK_INVALID = 4096` shares no bits with `HEADER_VALID = 1`, so `status.contains(HEADER_VALID)` evaluates to `false` when `status == BLOCK_INVALID`.

**The gap in `accept()`:**

`HeaderAcceptor::accept()` at `sync/src/synchronizer/headers_process.rs` L295–358: [2](#0-1) 

The `// FIXME` comment at L301–302 explicitly acknowledges the missing guard. When `status == BLOCK_INVALID`, the `HEADER_VALID` check at L304 does not fire, and execution falls through to `prev_block_check` (L324), `non_contextual_check` (L334), and `version_check` (L346). If all three pass, `insert_valid_header` is called at L356. [3](#0-2) 

**Contrast with every other status-check site:**

- `compact_block_process.rs` L259–260 returns early on `BLOCK_INVALID`: [4](#0-3) 

- `orphan_broker.rs` L119–120 routes to `process_invalid_block` on `BLOCK_INVALID`: [5](#0-4) 

- `verify.rs` L245–252 returns an error on `BLOCK_INVALID`: [6](#0-5) 

**`get_block_status` priority order:**

`shared/src/shared.rs` L425–445 checks `block_status_map` first, then `header_map`, then the database: [7](#0-6) 

**Two concrete exploit paths:**

1. **`BLOCK_INVALID` in `block_status_map` (in-memory):** A header H is marked `BLOCK_INVALID` in `block_status_map` (e.g., by `compact_block_process.rs` after a contextual check failure). The attacker re-sends H via `SendHeaders`. `accept()` sees `BLOCK_INVALID`, skips the `HEADER_VALID` guard, runs all three checks. If H has valid PoW and a valid parent, all checks pass, `insert_valid_header` is called (L356), H is inserted into `header_map`, and the peer's `best_known_header` is updated. `block_status_map` still holds `BLOCK_INVALID`, so `get_block_status` continues to return `BLOCK_INVALID` — but the inconsistent state and CPU/memory cost are real and repeatable.

2. **`BLOCK_INVALID` only in the database (e.g., after node restart):** If `block_status_map` has no entry for H (cleared on restart) but the database has `block_ext.verified == Some(false)`, `get_block_status` returns `BLOCK_INVALID` from the database fallback. After `insert_valid_header` inserts H into `header_map`, subsequent `get_block_status` calls find H in `header_map` before reaching the database, returning `HEADER_VALID`. This is a genuine status elevation from `BLOCK_INVALID` to `HEADER_VALID` in the node's live view, which can cause the block fetcher to issue `GetBlocks` for H. [8](#0-7) 

## Impact Explanation

The concrete impact is repeated CPU expenditure (including PoW verification inside `HeaderVerifier::verify()`) on headers the node has already determined to be invalid, plus wasted `header_map` memory insertions and peer `best_known_header` updates for invalid headers. In the database-only scenario, the status elevation to `HEADER_VALID` can trigger redundant `GetBlocks` requests and full-block downloads for blocks already known to be invalid, wasting bandwidth. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not rise to node crash, consensus deviation, or network-wide congestion, because `block_status_map` (when populated) still returns `BLOCK_INVALID` to the block fetcher, limiting the blast radius of the in-memory scenario.

## Likelihood Explanation

Any P2P peer can send `SendHeaders` messages without special privileges. The attacker does not need to mine new blocks — they only need to re-send headers they previously advertised that were rejected contextually (e.g., median-time-past violation) but are non-contextually valid (valid PoW, valid parent, version 0). Such headers arise naturally from forks or timestamp-manipulated blocks. The database-only scenario is reachable after a node restart. The attack is repeatable at will with no rate-limiting barrier visible in the `accept()` path, and the `execute()` caller only disconnects the peer on `ValidationState::Invalid` — which is not returned when all three checks pass and `insert_valid_header` is called.

## Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, immediately after retrieving the status, consistent with `compact_block_process.rs`, `orphan_broker.rs`, and `verify.rs`:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let sync_shared = self.active_chain.sync_shared();
    let state = self.active_chain.state();
    let shared = sync_shared.shared();

    let status = self.active_chain.get_block_status(&self.header.hash());

    // Resolves FIXME: guard against already-invalid headers
    if status.contains(BlockStatus::BLOCK_INVALID) {
        result.invalid(None);
        return result;
    }

    if status.contains(BlockStatus::HEADER_VALID) {
        // ... existing path
        return result;
    }
    // ... rest of validation
}
```

This resolves the `// FIXME` at L301–302 and makes the status-check pattern uniform.

## Proof of Concept

1. Connect to a CKB node as a P2P peer using the Sync protocol.
2. Send a `SendHeaders` message containing a header H whose parent is on the main chain, whose PoW is valid, but whose `timestamp` violates the median-time-past rule (contextually invalid).
3. Confirm H is marked `BLOCK_INVALID` in `block_status_map` (observable via debug logging or by sending a compact block for H and receiving `BlockIsInvalid`).
4. Send a second `SendHeaders` message containing the same header H.
5. Observe via the `"inserted valid header"` log line (emitted at multiples of 10,000 in `insert_valid_header`) or by inspecting `header_map` that H was re-inserted, confirming `BLOCK_INVALID` was not respected and the node re-ran full validation.
6. Repeat step 4 indefinitely to demonstrate unbounded CPU and memory waste with no peer disconnection.

### Citations

**File:** shared/src/block_status.rs (L8-17)
```rust
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L295-357)
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
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** chain/src/orphan_broker.rs (L119-121)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
```

**File:** chain/src/verify.rs (L244-252)
```rust
            let parent_status = self.shared.get_block_status(&parent_hash);
            if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
                return Err(InternalErrorKind::Other
                    .other(format!(
                        "block: {}'s parent: {} previously verified failed",
                        block_hash, parent_hash
                    ))
                    .into());
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
