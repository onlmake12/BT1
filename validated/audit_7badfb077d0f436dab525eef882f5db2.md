Audit Report

## Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Allows Re-elevation of Invalid Headers and Repeated Block Download Cycles — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` skips the `BLOCK_INVALID` status check (acknowledged by a `// FIXME` comment) because `BLOCK_INVALID = 1 << 12` shares no bits with `HEADER_VALID = 1`, so `status.contains(HEADER_VALID)` is `false` when status is `BLOCK_INVALID`. A peer can re-send a header already marked invalid, causing `accept()` to re-run all non-contextual checks, call `insert_valid_header()`, and update the peer's `best_known_header` — which then causes the block fetcher to issue repeated `GetBlocks` requests for a block the node already knows is invalid.

## Finding Description

**Root cause — bit-flag gap:**
`BLOCK_INVALID = 1 << 12 = 4096` is an isolated bit with no overlap with the `HEADER_VALID` chain (`1, 3, 7, 15`). [1](#0-0) 

The early-return guard in `accept()` only fires for `HEADER_VALID`, so `BLOCK_INVALID` falls through to full re-validation: [2](#0-1) 

If `prev_block_check`, `non_contextual_check`, and `version_check` all pass (which they do for a header that was marked invalid by a *contextual* check elsewhere, e.g., in `compact_block_process.rs`), `insert_valid_header()` is called: [3](#0-2) 

**`insert_valid_header` inserts into `header_map` but does NOT update `block_status_map`:** [4](#0-3) 

**`get_block_status` checks `block_status_map` first**, so the status remains `BLOCK_INVALID` for subsequent calls: [5](#0-4) 

**Block fetcher does NOT guard against `BLOCK_INVALID`** — it only skips `BLOCK_STORED` and `BLOCK_RECEIVED`. A header with status `BLOCK_INVALID` falls through to the `else if` branch and is added to the inflight fetch list: [6](#0-5) 

**Exploit flow:**
1. Attacker sends a compact block whose header H has valid PoW but fails a contextual check (e.g., median-time-past). `compact_block_process.rs` marks H as `BLOCK_INVALID` in `block_status_map`. [7](#0-6) 
2. Attacker re-sends H via `SendHeaders`. `accept()` reads `status = BLOCK_INVALID`, the `HEADER_VALID` guard does not fire, all three sub-checks pass, and `insert_valid_header(H)` is called.
3. H is now in `header_map`; peer's `best_known_header` is set to H.
4. Block fetcher traverses from `best_known_header = H`. `get_block_status(H)` returns `BLOCK_INVALID`. This does not match `BLOCK_STORED` or `BLOCK_RECEIVED`, so H is added to inflight and `GetBlocks` is issued.
5. Full block arrives, fails contextual verification, is re-marked `BLOCK_INVALID`. H remains in `header_map` and `best_known_header` is not downgraded.
6. On the next block-fetch timer tick, step 4 repeats — the node issues `GetBlocks` for H again, indefinitely.

**Every other status-check site guards `BLOCK_INVALID` explicitly:** [8](#0-7) [9](#0-8) 

## Impact Explanation

The node enters a persistent cycle of issuing `GetBlocks` for a block it already knows is invalid, downloading the full block, re-verifying it, and re-marking it invalid — wasting CPU and bandwidth on every block-fetch timer tick. This is an individual-node resource-exhaustion issue. It does not crash the node, cause consensus deviation, or damage the economy. The impact fits **Low (501–2000 points): Any other important performance improvements for CKB**, specifically eliminating avoidable repeated block downloads and re-validation work.

## Likelihood Explanation

Any connected P2P peer can send `SendHeaders` messages at will. The attacker must first obtain a header H that is `BLOCK_INVALID` on the target node and passes non-contextual checks — the most realistic source is a header with valid PoW that fails a contextual rule (e.g., median-time-past). Mining such a header requires real hashpower, making this a non-trivial but one-time cost. Once H is in `header_map`, the repeated download cycle runs automatically without further attacker action. The attack is repeatable across restarts only if `block_status_map` (an in-memory `DashMap`) is repopulated.

## Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `accept()`, before the `HEADER_VALID` check, consistent with every other status-check site:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let sync_shared = self.active_chain.sync_shared();
    let state = self.active_chain.state();
    let shared = sync_shared.shared();

    let status = self.active_chain.get_block_status(&self.header.hash());

    // Resolves the FIXME: guard against already-invalid headers
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

This resolves the `// FIXME` comment and prevents `insert_valid_header` from being called for headers already known to be invalid. [10](#0-9) 

## Proof of Concept

1. Connect to a CKB node as a P2P peer using the Sync + Relay protocols.
2. Construct header H with valid PoW whose `timestamp` violates the median-time-past rule for the current tip.
3. Send H as a `CompactBlock` via the Relay protocol. Observe (via debug logging) that `compact_block_process.rs` returns `BlockIsInvalid` and `block_status_map[H] = BLOCK_INVALID`.
4. Send a `SendHeaders` message containing H via the Sync protocol.
5. Observe the `"inserted valid header"` log line (or a `GetBlocks` request for H's hash) confirming `insert_valid_header` was called despite `BLOCK_INVALID` status.
6. Observe that on the next block-fetch timer tick (IBD: ~`IBD_BLOCK_FETCH_INTERVAL`; non-IBD: ~`NOT_IBD_BLOCK_FETCH_INTERVAL`), the node issues another `GetBlocks` for H without any further attacker action, confirming the persistent download cycle.

### Citations

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
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

**File:** sync/src/synchronizer/headers_process.rs (L354-357)
```rust
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** sync/src/types/mod.rs (L1129-1132)
```rust
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
```

**File:** shared/src/shared.rs (L425-431)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
```

**File:** sync/src/synchronizer/block_fetcher.rs (L247-284)
```rust
            let mut status = self
                .sync_shared
                .active_chain()
                .get_block_status(&header.hash());

            // Judge whether we should fetch the target block, neither stored nor in-flighted
            for _ in 0..span {
                let parent_hash = header.parent_hash();
                let hash = header.hash();

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

**File:** sync/src/relayer/compact_block_process.rs (L259-260)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
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

**File:** chain/src/orphan_broker.rs (L119-120)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
```
