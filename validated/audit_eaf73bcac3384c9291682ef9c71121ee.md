Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Enables Repeated Block Download Loop - (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains an acknowledged FIXME: it checks for `HEADER_VALID` but not `BLOCK_INVALID` before proceeding to non-contextual checks. Because `BLOCK_INVALID = 1 << 12` shares no bits with `HEADER_VALID = 1`, a header whose block was previously invalidated falls through all three non-contextual checks and reaches `insert_valid_header`. This inserts the header into `header_map` and updates the peer's best-known-header, causing the block fetcher to schedule repeated `GetBlocks` requests for the same known-invalid block. The block is not re-verified (a guard in `new_block_received` prevents that), but the download loop persists indefinitely, wasting bandwidth and consuming inflight block slots.

## Finding Description

**Root cause — missing guard in `accept()`:**

`BLOCK_INVALID = 1 << 12` does not contain the `HEADER_VALID = 1` bit, so the only early-return at line 304 is never triggered for an invalid block:

```
// FIXME If status == BLOCK_INVALID then return early.
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
// BLOCK_INVALID falls through here
```

All three subsequent checks (`prev_block_check`, `non_contextual_check`, `version_check`) are non-contextual and pass for a structurally valid header. `insert_valid_header` is then called.

**What `insert_valid_header` actually does:**

`insert_valid_header` (mod.rs:1094–1141) inserts the header into `header_map` and calls `may_set_best_known_header`. It does **not** call `insert_block_status`. Therefore `block_status_map` retains `BLOCK_INVALID` for this hash.

**`get_block_status` priority:**

`get_block_status` (shared.rs:425–445) checks `block_status_map` first; the `header_map` fallback is only reached when the hash is absent from `block_status_map`. So `get_block_status` continues to return `BLOCK_INVALID` after `insert_valid_header` is called.

**Block fetcher loop:**

`BlockFetcher` (block_fetcher.rs:247–284) traverses the `header_map` chain from the peer's best-known header. For each header it checks `BLOCK_STORED` and `BLOCK_RECEIVED` but has no guard for `BLOCK_INVALID`. A header with `BLOCK_INVALID` status falls through to the `else if` branch and is added to the inflight list, triggering a `GetBlocks` request.

When the block arrives, `new_block_received` (mod.rs:1200–1227) checks `BlockStatus::HEADER_VALID.eq(&status)` (exact equality). Since status is `BLOCK_INVALID`, the check fails and the function returns `false` — the block is **not** re-verified. However, the inflight entry is removed, so the block fetcher re-adds the block on its next tick. The cycle repeats indefinitely without any attacker action beyond the initial setup.

**`execute()` has no pre-filter:**

`HeadersProcess::execute()` checks only size, emptiness, and chain continuity; it has no guard against headers already in `BLOCK_INVALID` state.

## Impact Explanation

An unprivileged peer can force the victim node into a perpetual download loop for a known-invalid block: the block fetcher schedules `GetBlocks`, the block is downloaded, `new_block_received` rejects it without re-verification, the inflight entry is cleared, and the fetcher re-schedules on the next tick. This wastes outbound `GetBlocks` bandwidth, inbound block-download bandwidth, and inflight block slots (capped per peer), potentially delaying legitimate block synchronization. Sustained across multiple peers or multiple invalid blocks, this constitutes low-cost network congestion.

**Applicable impact class:** High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.

## Likelihood Explanation

Any peer that can establish a connection can trigger this. The attacker sends one block with a valid header but invalid body (e.g., bad cellbase reward or script failure) to cause the node to mark the hash `BLOCK_INVALID`, then sends one `SendHeaders` message containing that header. No special privilege, key material, or majority hashpower is required. After the initial setup the loop is self-sustaining: even if the attacker goes silent, the inflight timeout causes the fetcher to retry. The attack is cheap to initiate and free to sustain.

## Recommendation

Add an explicit early-return for `BLOCK_INVALID` immediately before the `HEADER_VALID` check in `HeaderAcceptor::accept()`, resolving the existing FIXME:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // existing logic
    return result;
}
```

Additionally, `BlockFetcher` should skip headers whose status is `BLOCK_INVALID` to provide defence-in-depth.

## Proof of Concept

1. Connect to a target CKB node as an unprivileged peer.
2. Craft a block with a valid header (valid PoW, valid timestamp, version 0) but an invalid body (e.g., cellbase output capacity exceeds reward).
3. Send it via `SendBlock`. The node verifies the block, fails contextual checks, and marks the hash `BLOCK_INVALID` in `block_status_map` (chain/src/verify.rs:177).
4. Construct a `SendHeaders` message containing only that header (one entry; passes `MAX_HEADERS_LEN` and `is_continuous`).
5. Send the `SendHeaders` message. `HeaderAcceptor::accept()` does not return early; all three non-contextual checks pass; `insert_valid_header` is called; the header enters `header_map`; the peer's best-known header is updated.
6. Observe that the node sends a `GetBlocks` request for the same hash (block fetcher tick).
7. Respond with the same invalid block. `new_block_received` removes the inflight entry and returns `false` (no re-verification). On the next fetcher tick, `GetBlocks` is sent again.
8. The loop repeats indefinitely, consuming bandwidth and inflight slots, without any further `SendHeaders` messages from the attacker.