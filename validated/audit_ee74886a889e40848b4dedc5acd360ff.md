I need to verify the key claims in this report by reading the actual code.

Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Allows Sync State Corruption and Redundant Block Downloads - (File: sync/src/synchronizer/headers_process.rs)

## Summary

`HeaderAcceptor::accept()` contains a developer-acknowledged FIXME that omits an early-return guard for `BLOCK_INVALID` block status. Because `BLOCK_INVALID` (`1 << 12`) and `HEADER_VALID` (`1`) are completely disjoint bit flags, a header whose block was previously determined invalid bypasses the only status check and falls through all three lightweight validators (`prev_block_check`, `non_contextual_check`, `version_check`). If those pass, `insert_valid_header` is called, corrupting the peer's best-known header and the shared best header, and causing the `BlockFetcher` to re-request the already-rejected block body. The identical scenario is correctly handled in `CompactBlockProcess` via an explicit `BLOCK_INVALID` guard.

## Finding Description

**Root cause — acknowledged FIXME with no guard:**

In `sync/src/synchronizer/headers_process.rs` lines 301–322, `accept()` reads the block status and branches only on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
```

`BLOCK_INVALID = 1 << 12 = 4096` and `HEADER_VALID = 1` share no bits (`shared/src/block_status.rs` lines 11–16), so `status.contains(BlockStatus::HEADER_VALID)` is always `false` for an invalid block, and execution falls through.

**Why the three subsequent checks do not catch it:**

- `prev_block_check` (lines 244–253): checks `parent_hash` for `BLOCK_INVALID`, not the header itself.
- `non_contextual_check` (lines 255–283): runs `HeaderVerifier`, which performs structural/PoW checks. A block marked invalid due to script execution failure has a structurally valid header and passes.
- `version_check` (lines 286–293): checks only the version field.

**Consequence — `insert_valid_header` is called (line 356):**

`insert_valid_header` (`sync/src/types/mod.rs` lines 1094–1141) inserts the header into `header_map`, calls `may_set_best_known_header` for the peer, and calls `may_set_shared_best_header`. It does **not** update `block_status_map`, so `get_block_status` still returns `BLOCK_INVALID` for the block hash (checked first in `shared/src/shared.rs` lines 425–444).

**Consequence — `BlockFetcher` re-requests the block:**

In `sync/src/synchronizer/block_fetcher.rs` lines 247–284, the fetcher skips blocks with `BLOCK_STORED` or `BLOCK_RECEIVED` status. `BLOCK_INVALID` satisfies neither, so the block is added to the inflight list and re-downloaded. After re-download, `new_block_received` (`sync/src/types/mod.rs` lines 1199–1227) checks `if !BlockStatus::HEADER_VALID.eq(&status)` and returns false for `BLOCK_INVALID`, but the download has already occurred.

**Contrast — `CompactBlockProcess` correctly rejects `BLOCK_INVALID`:**

`sync/src/relayer/compact_block_process.rs` lines 259–260:
```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
```
This is confirmed by the test at `sync/src/relayer/tests/compact_block_process.rs` lines 60–71.

## Impact Explanation

An attacker with a single peer connection can repeatedly send `SendHeaders` messages containing the header of a block already marked `BLOCK_INVALID`. Each iteration causes the victim node to:
1. Accept the header as valid (corrupting `shared_best_header` and the peer's `best_known_header` if the invalid chain has higher total difficulty).
2. Re-download the full block body via `GetBlocks`.
3. Re-run full block verification (including script execution), consuming CPU.
4. Re-mark the block `BLOCK_INVALID` — and the cycle repeats.

The cost to the attacker is negligible (small `SendHeaders` P2P messages). The cost to the victim is significant (full block bandwidth + verification CPU per cycle). Scaled across many connected peers or many nodes, this constitutes **CKB network congestion with few costs**, matching the High impact class (10001–15000 points).

Additionally, if the invalid chain has higher total difficulty, `shared_best_header` is advanced to it (`SyncState::may_set_shared_best_header`, `sync/src/types/mod.rs` lines 1398–1408), misleading the synchronizer's chain-selection and IBD-completion logic.

## Likelihood Explanation

Any unprivileged connected peer can trigger this. The attacker needs only to:
1. Know a block hash marked `BLOCK_INVALID` on the target node (e.g., by submitting a block with a valid header but failing script, or observing one propagated on the network).
2. Send a `SendHeaders` P2P message containing that header.

No keys, majority hashpower, or special privileges are required. The `SendHeaders` message is a standard sync protocol message accepted from all peers. The attack is repeatable indefinitely while the peer remains connected, and the FIXME comment confirms the developers are aware of the gap.

## Recommendation

Add an explicit early-return for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, resolving the acknowledged FIXME:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
}
```

This mirrors the guard already present in `CompactBlockProcess` (`sync/src/relayer/compact_block_process.rs` line 259).

## Proof of Concept

1. Start a CKB node and connect a peer.
2. Submit a block with a structurally valid header but a failing script body. Wait for the node to mark it `BLOCK_INVALID` in `block_status_map` (confirmed via `chain/src/verify.rs` lines 175–177).
3. From the peer, send a `SendHeaders` message containing only that block's header.
4. Observe: `HeaderAcceptor::accept()` falls through all three checks and calls `insert_valid_header`, updating `shared_best_header` and the peer's `best_known_header`.
5. Observe: `BlockFetcher` sees the peer's `best_known_header` is ahead, finds the block not in `BLOCK_STORED`/`BLOCK_RECEIVED` state, and issues a `GetBlocks` request for the already-rejected block.
6. Repeat step 3 in a loop. Each iteration triggers a full block download and re-verification.

A unit test analogous to `test_in_block_status_map` in `sync/src/relayer/tests/compact_block_process.rs` can be written for `HeaderAcceptor::accept()`: insert `BLOCK_INVALID` into `block_status_map` for a header, call `accept()`, and assert the result state is `ValidationState::Invalid` — currently this assertion fails (result is `ValidationState::Valid`), confirming the bug.