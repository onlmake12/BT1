Audit Report

## Title
`BLOCK_INVALID` Status Bypass in `HeaderAcceptor::accept` Allows Repeated Sync State Corruption — (`sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::accept` guards re-processing with `status.contains(BlockStatus::HEADER_VALID)`, but `BLOCK_INVALID` (`1 << 12`) and `HEADER_VALID` (`1`) are orthogonal bits, so a header already marked `BLOCK_INVALID` passes this guard. The developer explicitly acknowledged this with a `// FIXME` comment at the exact location. The header then falls through all three sub-checks and reaches `insert_valid_header`, which inserts it into `header_map` and corrupts both the peer's `best_known_header` and the global `shared_best_header`. Because `insert_valid_header` does not update `block_status_map`, `get_block_status` continues returning `BLOCK_INVALID`, making the exploit repeatable on every `SendHeaders` message at zero marginal cost after the initial PoW.

## Finding Description

**Bit-flag orthogonality (confirmed):**

`shared/src/block_status.rs` lines 11 and 16:
- `HEADER_VALID = 1` (bit 0)
- `BLOCK_INVALID = 1 << 12 = 4096` (bit 12)

`(4096 & 1) == 0`, so `status.contains(BlockStatus::HEADER_VALID)` is `false` when `status == BLOCK_INVALID`.

**The unguarded path in `accept()` (confirmed, lines 301–356):**

The `// FIXME` comment at line 301 is a developer-acknowledged gap:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {   // false when BLOCK_INVALID
    ...
    return result;
}
```

When `status == BLOCK_INVALID`, execution falls through to:
1. `prev_block_check` (line 324) — checks the **parent's** status, not H itself.
2. `non_contextual_check` (line 334) — runs `HeaderVerifier::verify`, which only checks PoW, timestamp, and epoch. A header that previously passed these checks before its block body was found invalid will pass again.
3. `version_check` (line 346) — checks `version == 0`.
4. `insert_valid_header` (line 356) — called unconditionally if all three pass.

**How a header reaches `BLOCK_INVALID` with valid header fields:**

In `chain/src/verify.rs` lines 175–177, when full block verification fails (invalid transactions, script execution, etc.), the block is marked `BLOCK_INVALID`. The header itself already passed PoW/timestamp/version checks before body verification, so `non_contextual_check` will pass again.

**What `insert_valid_header` does (confirmed, `sync/src/types/mod.rs` lines 1129–1140):**
- Line 1129: inserts the header into `header_map`
- Line 1132: calls `may_set_best_known_header` — updates the peer's best known header to the invalid chain tip
- Line 1140: calls `may_set_shared_best_header` — potentially updates the **global** shared best header to the invalid chain tip

**Repeatability confirmed via `get_block_status` (`shared/src/shared.rs` lines 425–445):**

`block_status_map` is checked first. Since `insert_valid_header` does not call `insert_block_status`, `block_status_map` retains `BLOCK_INVALID`. Every subsequent call to `get_block_status` for H returns `BLOCK_INVALID`, so the bypass is re-triggered on every `SendHeaders` message containing H.

**Contrast with `CompactBlockProcess` (`sync/src/relayer/compact_block_process.rs` lines 259–260):**

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
```

The relay path correctly guards against `BLOCK_INVALID`. The sync header path does not.

## Impact Explanation

The `shared_best_header` is the global anchor used by all peers' sync decisions. Corrupting it to an invalid chain tip causes the block fetcher to schedule downloads of blocks building on an invalid chain, wasting bandwidth and disrupting sync for all connected peers. Combined with the zero-marginal-cost repeatability (one `SendHeaders` message per trigger), an attacker can sustain this disruption indefinitely after a single PoW investment, matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

The actual chain state (UTXO set, tip) is not corrupted; this is not a consensus deviation.

## Likelihood Explanation

The attack requires an unprivileged P2P connection. The attacker must mine one block with a valid header (valid PoW, timestamp, version) but an invalid body (e.g., a failing script or invalid signature). On mainnet, the PoW cost is significant but one-time: a single such block enables unlimited repeated exploitation via `SendHeaders`. On a low-difficulty testnet or during IBD on a low-difficulty chain, the cost is negligible. After the initial block is marked `BLOCK_INVALID` by the target node, the attacker sends `SendHeaders` messages containing that header hash indefinitely, each triggering `insert_valid_header` and re-corrupting `shared_best_header`.

## Recommendation

Add an explicit `BLOCK_INVALID` early-return guard at the top of `accept()`, immediately after the status check, resolving the `// FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent));
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // existing logic
}
```

This mirrors the guard already present in `CompactBlockProcess` at lines 259–260.

## Proof of Concept

1. Pre-insert `BLOCK_INVALID` status for header hash H in `block_status_map` (simulating a prior failed full-block verification, as done in `chain/src/verify.rs` lines 175–177).
2. Construct H with a valid parent (not `BLOCK_INVALID`), valid PoW/timestamp, and `version == 0`.
3. Call `HeadersProcess::execute` with a `SendHeaders` message containing H.
4. Assert `accept()` returns `ValidationState::Valid` — **this is the bug**; it should return `Invalid`.
5. Assert `header_map` now contains H — **this is the bug**; it should not.
6. Assert `may_set_best_known_header` was called with H's index — **this is the bug**.
7. Call `get_block_status(H)` — confirm it still returns `BLOCK_INVALID` (repeatability confirmed).
8. Re-send `SendHeaders` with H and assert steps 4–6 repeat.

The existing test `test_in_block_status_map` in `sync/src/relayer/tests/compact_block_process.rs` demonstrates the correct behavior for the relay path and serves as a direct template. The analogous test for `HeadersProcess` would fail at step 4 due to the missing `BLOCK_INVALID` guard.