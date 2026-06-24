Audit Report

## Title
Missing `BLOCK_INVALID` Guard Enables Repeated Re-Download of Invalid Blocks — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` in `headers_process.rs` contains a developer-acknowledged `FIXME` noting the absence of an early-exit guard for `BLOCK_INVALID` status. Because `BLOCK_INVALID` (`1 << 12`) shares no bits with `HEADER_VALID` (`1`), the `status.contains(HEADER_VALID)` check does not catch already-invalidated blocks. All three header-level sub-checks pass for a block with a structurally valid header, causing `insert_valid_header` to be called and the peer's best-known-header to be advanced to the invalid block. `block_fetcher.rs` independently lacks a `BLOCK_INVALID` branch, so the block is unconditionally re-queued for download, creating a repeatable resource-exhaustion loop at the cost of a single PoW solution.

## Finding Description

**Root cause — `headers_process.rs` (lines 301–356)**

The `accept()` function reads the block status but only guards on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
``` [1](#0-0) 

`BLOCK_INVALID = 1 << 12` shares no bits with `HEADER_VALID = 1`, so the guard is never triggered for an invalid block: [2](#0-1) 

The three sub-checks (`prev_block_check`, `non_contextual_check`, `version_check`) all operate on the *header* only. `prev_block_check` checks whether the **parent** has `BLOCK_INVALID` — not the block itself — so it passes when the parent is on the main chain: [3](#0-2) 

All three checks pass for a block with a valid header and invalid body, and execution reaches:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [4](#0-3) 

`insert_valid_header` inserts the header into `header_map` and advances the peer's `best_known_header`, but does **not** write to `block_status_map`: [5](#0-4) 

**Secondary cause — `block_fetcher.rs` (lines 257–284)**

The fetcher iterates the best-known-header chain and skips blocks only for `BLOCK_STORED` and `BLOCK_RECEIVED`. There is no `BLOCK_INVALID` branch: [6](#0-5) 

`get_block_status` checks `block_status_map` first. Since the chain service writes `BLOCK_INVALID` there on contextual verification failure (and deletes the block from the unverified store), the status is `BLOCK_INVALID` — but neither caller acts on it: [7](#0-6) [8](#0-7) 

**Full exploit flow:**
1. Attacker crafts block `B`: valid PoW/timestamp/version/parent, invalid body (e.g., transaction spending a non-existent cell).
2. Node downloads `B`; contextual verification fails; `BLOCK_INVALID` is written to `block_status_map`; block is deleted from the unverified store.
3. Attacker re-sends a `Headers` P2P message containing only `B`'s header.
4. `HeaderAcceptor::accept()` reads status = `BLOCK_INVALID`; the `HEADER_VALID` guard does not fire; all three header checks pass; `insert_valid_header` is called; peer's `best_known_header` is set to `B`.
5. `BlockFetcher::fetch()` walks the best-known chain, finds `B` with status `BLOCK_INVALID`, matches neither `BLOCK_STORED` nor `BLOCK_RECEIVED`, and (in IBD mode unconditionally, in non-IBD mode when `compare_with_pending_compact` is satisfied) calls `write_inflight_blocks().insert(...)` and pushes `B` to the fetch list.
6. Node sends `GetBlocks`, re-downloads `B`, re-runs contextual verification (CKB-VM script execution), fails again, and the cycle repeats from step 3.

## Impact Explanation

Each cycle consumes a full block download (bandwidth) and a complete contextual verification pass including CKB-VM script execution (CPU). A single crafted block, after its one-time PoW cost, can sustain a continuous loop against any reachable node. Under sustained attack, sync throughput degrades and legitimate block processing is delayed. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

- The entry point is a standard `Headers` P2P message, reachable by any unprivileged peer with a connection to the target node.
- The attacker pays the PoW cost exactly once; all subsequent iterations require only cheap header messages.
- The `FIXME` comment in production code confirms the developers are aware the guard is absent.
- The `block_fetcher.rs` gap is independent and would be triggered by any code path that places a `BLOCK_INVALID` block into the best-known-header chain.
- During IBD the `IBDState::In` branch makes the fetch unconditional; during normal sync the `compare_with_pending_compact` path provides an additional trigger vector via compact block relay.

## Recommendation

**In `headers_process.rs`**, resolve the FIXME by adding an explicit `BLOCK_INVALID` guard before the `HEADER_VALID` check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

**In `block_fetcher.rs`**, add a `BLOCK_INVALID` skip branch alongside the existing guards:

```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    // Already known invalid; do not re-download
} else if status.contains(BlockStatus::BLOCK_STORED) {
    ...
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    ...
} else if ... {
    fetch.push(header)
}
```

## Proof of Concept

1. Craft block `B` at height `N` with a valid header (correct PoW, timestamp, version, valid parent) and an invalid body (e.g., a transaction spending a non-existent cell or with an invalid witness).
2. Send `B` to the target node via the block relay protocol. Confirm via logs that contextual verification fails and `BLOCK_INVALID` is set.
3. In a loop, send a `Headers` P2P message containing only `B`'s header to the same node.
4. Observe via node metrics and logs that `GetBlocks` requests are issued for `B` on each iteration, bandwidth is consumed, and CKB-VM script execution is re-triggered, confirming the repeated re-download cycle.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L244-252)
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

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** sync/src/types/mod.rs (L1129-1132)
```rust
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
```

**File:** sync/src/synchronizer/block_fetcher.rs (L257-284)
```rust
                if status.contains(BlockStatus::BLOCK_STORED) {
                    if status.contains(BlockStatus::BLOCK_VALID) {
                        // If the block is stored, its ancestor must on store
                        // So we can skip the search of this space directly
                        self.sync_shared
                            .state()
                            .peers()
                            .set_last_common_header(self.peer, header.number_and_hash());
                    }

                    end = window_end(header.number(), BLOCK_DOWNLOAD_WINDOW, best_known.number());
                    break;
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
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

**File:** chain/src/verify.rs (L173-181)
```rust
                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```
