Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Enables Repeated Block Download Loop - (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains an acknowledged FIXME: it checks for `HEADER_VALID` but not `BLOCK_INVALID` before proceeding to non-contextual checks. A header whose block was previously invalidated falls through all three non-contextual checks and reaches `insert_valid_header`, inserting the header into `header_map` and updating the peer's best-known header. The block fetcher then schedules repeated `GetBlocks` requests for the same known-invalid block indefinitely, wasting bandwidth and consuming inflight block slots.

## Finding Description

**Root cause — missing guard in `accept()`:**

The FIXME comment is present verbatim in the code at `sync/src/synchronizer/headers_process.rs` lines 301–303:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ...
    return result;
}
```

`BLOCK_INVALID = 1 << 12` shares no bits with `HEADER_VALID = 1`, so the early-return at line 304 is never triggered for a previously-invalidated block.

**Three subsequent checks all pass for a structurally valid header:**

1. `prev_block_check` (lines 244–253) checks `parent_hash()` — the *parent's* status — not the current block's hash. If the parent is valid, this passes regardless of the current block's `BLOCK_INVALID` status.
2. `non_contextual_check` (lines 255–283) runs `HeaderVerifier::verify()`, which is a structural check (PoW, timestamp). A header with valid PoW but invalid block body passes.
3. `version_check` (lines 286–293) checks `version == 0`. Passes for any conforming header.

`insert_valid_header` is then called unconditionally at line 356.

**`BlockFetcher` has no `BLOCK_INVALID` guard:**

In `sync/src/synchronizer/block_fetcher.rs` lines 257–284, the fetcher loop checks only `BLOCK_STORED` and `BLOCK_RECEIVED`:

```rust
if status.contains(BlockStatus::BLOCK_STORED) {
    // ...
    break;
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    // Do not download repeatedly
} else if ... && state.write_inflight_blocks().insert(...) {
    fetch.push(header)  // BLOCK_INVALID falls through here
}
```

There is no `BLOCK_INVALID` guard. A header with `BLOCK_INVALID` status falls through to the `else if` branch and is added to the inflight list, triggering a `GetBlocks` request.

**Loop mechanism:**

When the block arrives, `new_block_received` checks `BlockStatus::HEADER_VALID.eq(&status)` (exact equality). Since status is `BLOCK_INVALID`, the check fails, the block is not re-verified, but the inflight entry is removed. On the next fetcher tick, `BlockFetcher` re-adds the block. The cycle repeats indefinitely without any further attacker action.

## Impact Explanation

An unprivileged peer can force the victim node into a perpetual download loop for a known-invalid block. This wastes outbound `GetBlocks` bandwidth, inbound block-download bandwidth, and inflight block slots (capped per peer), potentially delaying legitimate block synchronization. Sustained across multiple peers or multiple invalid blocks, this constitutes low-cost network congestion. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* (10001–15000 points).

## Likelihood Explanation

Any peer that can establish a connection can trigger this. The attacker sends one block with a valid header but invalid body (e.g., bad cellbase reward) to cause the node to mark the hash `BLOCK_INVALID`, then sends one `SendHeaders` message containing that header. No special privilege, key material, or majority hashpower is required. After the initial setup the loop is self-sustaining: even if the attacker goes silent, inflight timeouts cause the fetcher to retry. The attack is cheap to initiate and free to sustain.

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

Additionally, `BlockFetcher` should skip headers whose status contains `BLOCK_INVALID` to provide defence-in-depth.

## Proof of Concept

1. Connect to a target CKB node as an unprivileged peer.
2. Craft a block with a valid header (valid PoW, valid timestamp, version 0) but an invalid body (e.g., cellbase output capacity exceeds reward).
3. Send it via `SendBlock`. The node verifies the block, fails contextual checks, and marks the hash `BLOCK_INVALID` in `block_status_map`.
4. Construct a `SendHeaders` message containing only that header (one entry; passes `MAX_HEADERS_LEN` and `is_continuous`).
5. Send the `SendHeaders` message. `HeaderAcceptor::accept()` does not return early (FIXME at line 301 is unresolved); all three non-contextual checks pass; `insert_valid_header` is called at line 356; the header enters `header_map`; the peer's best-known header is updated.
6. Observe that the node sends a `GetBlocks` request for the same hash (block fetcher tick).
7. Respond with the same invalid block. `new_block_received` removes the inflight entry and returns `false` (no re-verification). On the next fetcher tick, `GetBlocks` is sent again.
8. The loop repeats indefinitely, consuming bandwidth and inflight slots, without any further `SendHeaders` messages from the attacker.