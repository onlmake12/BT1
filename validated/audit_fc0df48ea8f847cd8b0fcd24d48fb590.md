Audit Report

## Title
`HeaderAcceptor::accept()` Missing `BLOCK_INVALID` Early-Return Allows Invalid Headers to Corrupt Peer Best-Known-Header State - (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` at lines 301–302 noting that `BLOCK_INVALID` status should trigger an early return but does not. Because `BLOCK_INVALID` (`1 << 12 = 4096`) shares no bits with `HEADER_VALID` (`1`), the only early-return guard is bypassed, all three subsequent sub-checks pass for a well-formed header with an invalid body, and `insert_valid_header` is called at line 356, corrupting the peer's best-known-header to point to an invalid block. The block fetcher then issues `GetBlocks` for that block, enabling a repeated request cycle driven by a single mined block replayed to many nodes.

## Finding Description
**Root cause:** In `sync/src/synchronizer/headers_process.rs` lines 301–304, the developer left a `FIXME` acknowledging the gap:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

From `shared/src/block_status.rs`:
- `HEADER_VALID = 1`
- `BLOCK_INVALID = 1 << 12 = 4096`

`4096 & 1 == 0`, so `status.contains(HEADER_VALID)` is `false` when status is `BLOCK_INVALID`. The guard does not fire.

**Three sub-checks all pass for a valid-header/invalid-body block:**

1. `prev_block_check` (line 244–253): checks `parent_hash` for `BLOCK_INVALID`, not the header's own hash. Passes.
2. `non_contextual_check` (line 255–283): calls `HeaderVerifier::verify()`, which validates PoW, block number, epoch, and timestamp — all valid for a well-formed header. Passes.
3. `version_check` (line 286–293): checks `version == 0`. Passes.

`insert_valid_header` is then called at line 356, writing the header into `header_map` and updating the peer's best-known-header to the invalid block.

**Block fetcher does not filter `BLOCK_INVALID`:** In `sync/src/synchronizer/block_fetcher.rs` lines 247–284, the fetcher checks `status.contains(BLOCK_STORED)` and `status.contains(BLOCK_RECEIVED)` before skipping. `BLOCK_INVALID` (4096) contains neither (`BLOCK_STORED = 7`, `BLOCK_RECEIVED = 3`), so the block is added to the inflight list and a `GetBlocks` request is issued.

**`get_block_status` layered lookup** (`shared/src/shared.rs` lines 425–444): checks `block_status_map` first, then `header_map`. While `BLOCK_INVALID` remains in `block_status_map`, `get_block_status` still returns `BLOCK_INVALID` correctly — but `insert_valid_header` has already written to `header_map`. If `remove_block_status` is ever called for this hash (e.g., on a successful re-verification path), `get_block_status` would return `HEADER_VALID` from `header_map` instead, silently re-legitimizing the invalid header.

**Contrast with correct guards:** `compact_block_process.rs` line 259 has an explicit `BLOCK_INVALID` guard; `orphan_broker.rs` line 119 checks `parent_status.eq(&BlockStatus::BLOCK_INVALID)`. The pattern is known and applied elsewhere but absent here.

## Impact Explanation
**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single mined block (valid PoW, invalid body) can be replayed to arbitrarily many nodes. Each targeted node: (1) marks the block `BLOCK_INVALID` on first receipt via `SendBlock`; (2) accepts the header via `SendHeaders` due to the missing guard; (3) issues `GetBlocks` for the invalid block because the block fetcher does not filter `BLOCK_INVALID` status. The attacker responds with the same block, the node rejects the body, and the cycle can repeat after the inflight timeout. One mining operation amortized across many nodes produces sustained bandwidth and CPU waste at network scale.

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer. The only prerequisite is mining one block with a valid header and an invalid body (e.g., a transaction with zero inputs, the same pattern used in `test/src/specs/sync/invalid_block.rs` lines 35–41). The same mined block can be replayed to arbitrarily many nodes without additional mining. The `FIXME` comment in production code confirms developer awareness of the gap. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation
In `HeaderAcceptor::accept()`, add an explicit early-return guard for `BLOCK_INVALID` immediately after reading the status, before the `HEADER_VALID` check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent));
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
}
```

This resolves the developer-acknowledged `FIXME` and mirrors the guard already present in `compact_block_process.rs` (line 259) and `orphan_broker.rs` (line 119). Additionally, consider adding a `BLOCK_INVALID` filter in `block_fetcher.rs` alongside the existing `BLOCK_STORED`/`BLOCK_RECEIVED` checks.

## Proof of Concept
1. Connect a malicious peer to a CKB node.
2. Mine block `B` at height `N` with a valid header (valid PoW, correct number/epoch/timestamp) but an invalid body (e.g., `TransactionBuilder::default().build()` — zero inputs, same pattern as `test/src/specs/sync/invalid_block.rs` lines 35–41).
3. Send block `B` via `SendBlock`. The node's chain service runs non-contextual verification, fails, and calls `insert_block_status(B.hash, BLOCK_INVALID)`.
4. Send the header of `B` via `SendHeaders`. `HeaderAcceptor::accept()` is called:
   - `get_block_status(B.hash)` returns `BLOCK_INVALID` (4096).
   - `status.contains(HEADER_VALID)` → `4096 & 1 == 0` → false. Guard does not fire.
   - `prev_block_check`: checks parent hash, not `B.hash`. Passes.
   - `non_contextual_check`: header-only PoW/number/epoch/timestamp. Passes.
   - `version_check`: version == 0. Passes.
   - `insert_valid_header` is called: `header_map[B.hash]` is set; peer's best-known-header updated to `B`.
5. Block fetcher runs: peer's best-known-header is `B` with higher difficulty. Status of `B` is `BLOCK_INVALID` (4096). `4096 & BLOCK_STORED(7) == 0`, `4096 & BLOCK_RECEIVED(3) == 0`. Block `B` is added to inflight; `GetBlocks` is sent to the attacker.
6. Attacker responds with `B`. Node rejects the body again.
7. Repeat steps 4–6 across many nodes using the same mined block `B`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** sync/src/synchronizer/headers_process.rs (L301-304)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
```

**File:** sync/src/synchronizer/headers_process.rs (L354-357)
```rust
        }

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

**File:** shared/src/shared.rs (L425-444)
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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** chain/src/orphan_broker.rs (L119-120)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
```
