All code citations check out exactly. Every claim in the report is verified against the actual source:

- `BLOCK_INVALID = 1 << 12` is bit-disjoint from `HEADER_VALID = 1` [1](#0-0) 
- The `// FIXME` comment and missing guard are present at L301–302 [2](#0-1) 
- Execution falls through to all three checks and `insert_valid_header` when `status == BLOCK_INVALID` [3](#0-2) 
- `compact_block_process.rs`, `orphan_broker.rs`, and `verify.rs` all have explicit `BLOCK_INVALID` guards that `accept()` lacks [4](#0-3) 
- `get_block_status` checks `block_status_map` first, then `header_map`, then DB — confirming the status elevation path in the restart scenario [5](#0-4) 
- `insert_valid_header` inserts into `header_map` and updates peer's `best_known_header` [6](#0-5) 

---

Audit Report

## Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Enables Repeated Re-validation of Invalid Headers — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` lacks an early-return guard for `BlockStatus::BLOCK_INVALID`, a gap explicitly acknowledged by a developer `// FIXME` comment. Because `BLOCK_INVALID = 1 << 12` shares no bits with `HEADER_VALID = 1`, the existing `status.contains(HEADER_VALID)` check does not fire when a header is already marked invalid, allowing any P2P peer to repeatedly trigger PoW re-verification and, in the post-restart scenario, elevate a header's status from `BLOCK_INVALID` to `HEADER_VALID` in the node's live view.

## Finding Description
`BlockStatus` defines `HEADER_VALID = 1` and `BLOCK_INVALID = 1 << 12 = 4096` as bit-disjoint flags. In `HeaderAcceptor::accept()` (L295–358), after retrieving `status`, the only early-return guard is `status.contains(BlockStatus::HEADER_VALID)` at L304. When `status == BLOCK_INVALID`, this check evaluates to `false` and execution falls through to `prev_block_check` (L324), `non_contextual_check` (L334, which includes PoW verification via `HeaderVerifier::verify()`), and `version_check` (L346). If all three pass, `insert_valid_header` is called at L356, inserting the header into `header_map` and updating the peer's `best_known_header`.

**In-memory scenario:** A header H is marked `BLOCK_INVALID` in `block_status_map` (e.g., by `compact_block_process.rs` after a contextual failure). An attacker re-sends H via `SendHeaders`. `accept()` sees `BLOCK_INVALID`, skips the `HEADER_VALID` guard, runs all three checks, and calls `insert_valid_header`. Since `block_status_map` is checked first by `get_block_status`, the status remains `BLOCK_INVALID` externally, but CPU and memory are wasted on every re-send.

**Post-restart scenario:** After a node restart, `block_status_map` is cleared. If the database holds `block_ext.verified == Some(false)` for H, `get_block_status` returns `BLOCK_INVALID` from the DB fallback. After `insert_valid_header` inserts H into `header_map`, subsequent `get_block_status` calls find H in `header_map` before reaching the DB (since `block_status_map` has no entry), returning `HEADER_VALID`. This is a genuine status elevation that can cause the block fetcher to issue `GetBlocks` for H and download the full block for re-verification.

Every other status-check site in the codebase handles `BLOCK_INVALID` explicitly: `compact_block_process.rs` L259–260 returns `BlockIsInvalid`, `orphan_broker.rs` L119–120 routes to `process_invalid_block`, and `verify.rs` L245–252 returns an error. The `accept()` path is the sole exception, as acknowledged by the `// FIXME` at L301–302.

## Impact Explanation
The concrete impact is repeated CPU expenditure including PoW verification on headers already determined to be invalid, wasted `header_map` memory insertions, and peer `best_known_header` updates for invalid headers. In the post-restart scenario, the status elevation to `HEADER_VALID` triggers redundant `GetBlocks` requests and full-block downloads for blocks already known to be invalid, wasting bandwidth. There is no peer disconnection on this path since `ValidationState::Invalid` is not returned when all three checks pass. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not rise to node crash, consensus deviation, or network-wide congestion.

## Likelihood Explanation
Any P2P peer can send `SendHeaders` messages without special privileges. The attacker only needs to re-send headers that were previously rejected contextually (e.g., median-time-past violation) but are non-contextually valid (valid PoW, valid parent, version 0). Such headers arise naturally from forks or timestamp-manipulated blocks. The attack is repeatable at will with no visible rate-limiting barrier in the `accept()` path. The post-restart scenario is reachable after any normal node restart.

## Recommendation
Add an explicit `BLOCK_INVALID` guard immediately after retrieving `status` in `HeaderAcceptor::accept()`, consistent with `compact_block_process.rs`, `orphan_broker.rs`, and `verify.rs`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

// Resolves FIXME: guard against already-invalid headers
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing path
}
```

This resolves the `// FIXME` at L301–302 and makes the `BLOCK_INVALID` check pattern uniform across all status-check sites.

## Proof of Concept
1. Connect to a CKB node as a P2P peer using the Sync protocol.
2. Send a `SendHeaders` message containing a header H whose parent is on the main chain, whose PoW is valid, but whose `timestamp` violates the median-time-past rule (contextually invalid).
3. Confirm H is marked `BLOCK_INVALID` in `block_status_map` (observable by sending a compact block for H and receiving `BlockIsInvalid` from `compact_block_process.rs`).
4. Send a second `SendHeaders` message containing the same header H.
5. Observe via debug logging that `prev_block_check`, `non_contextual_check`, and `version_check` are re-executed, and that `insert_valid_header` is called (confirmed by the `"inserted valid header"` log line at multiples of 10,000, or by inspecting `header_map`).
6. Repeat step 4 indefinitely to demonstrate unbounded CPU and memory waste with no peer disconnection.
7. For the status-elevation path: restart the node (clearing `block_status_map`), repeat steps 4–5, then query `get_block_status` for H and observe it returns `HEADER_VALID` instead of `BLOCK_INVALID`.

### Citations

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** sync/src/synchronizer/headers_process.rs (L301-303)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
```

**File:** sync/src/synchronizer/headers_process.rs (L324-357)
```rust
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

**File:** sync/src/relayer/compact_block_process.rs (L259-260)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
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

**File:** sync/src/types/mod.rs (L1129-1132)
```rust
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
```
