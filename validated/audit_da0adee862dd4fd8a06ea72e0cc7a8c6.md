Audit Report

## Title
Missing `BLOCK_INVALID` Early-Exit in `HeaderAcceptor::accept()` Enables CPU-Exhaustion DoS via Repeated Re-Verification of Rejected Headers — (File: sync/src/synchronizer/headers_process.rs)

## Summary

`HeaderAcceptor::accept()` contains a `// FIXME` comment acknowledging a missing early-exit for `BLOCK_INVALID` status. Because `BLOCK_INVALID` (`1 << 12 = 4096`) shares no bits with `HEADER_VALID` (`1`), the only status guard in the function is bypassed for already-rejected headers. An unprivileged remote peer can repeatedly send `SendHeaders` P2P messages containing headers already stored as `BLOCK_INVALID` in `block_status_map`, forcing the node to re-run full `HeaderVerifier::verify()` (including Eaglesong PoW) on each one instead of performing an O(1) map lookup rejection.

## Finding Description

**Root cause:** In `HeaderAcceptor::accept()` at lines 301–322 of `sync/src/synchronizer/headers_process.rs`, the only status-based early-exit is:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
```

`BlockStatus::BLOCK_INVALID` is defined as `1 << 12 = 4096` in `shared/src/block_status.rs` (L16). `BlockStatus::HEADER_VALID` is `1` (L11). The bitwise `contains` check (`4096 & 1 == 0`) is false, so a header stored as `BLOCK_INVALID` in `block_status_map` is **not caught** by this guard.

**Exploit flow:**

1. `get_block_status()` in `shared/src/shared.rs` (L425–444) checks `block_status_map` first. If the hash is present with `BLOCK_INVALID`, it returns `BLOCK_INVALID` immediately.
2. Back in `accept()`, `status.contains(BlockStatus::HEADER_VALID)` evaluates to `false` for `BLOCK_INVALID`, so execution falls through.
3. `prev_block_check()` (L324) checks whether the *parent* is `BLOCK_INVALID`, not the header itself — passes if the parent is fine.
4. `non_contextual_check()` (L334) calls `self.verifier.verify(self.header)`, which runs the full `HeaderVerifier` including Eaglesong PoW, timestamp median-time, and epoch checks — the expensive path.
5. `version_check()` (L346) runs next.
6. Only after all three checks fail does the function return `Invalid` and store `BLOCK_INVALID` again.

**Why existing checks are insufficient:** The `prev_block_check` only guards against an invalid *parent*, not the header itself. There is no check of the form `if status.contains(BlockStatus::BLOCK_INVALID) { return early; }` anywhere in `accept()`. The `FIXME` comment is the codebase's own acknowledgment of this gap.

**Contrast with relay path:** `compact_block_process.rs` (L259–261) correctly implements the guard:
```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
```
The synchronizer's `headers_process.rs` path lacks this equivalent check.

**Re-insertion risk:** If all three sub-checks happen to pass (e.g., the header was originally marked `BLOCK_INVALID` only because its parent was invalid, and the parent's `block_status_map` entry was later evicted), `sync_shared.insert_valid_header(self.peer, self.header)` at L356 executes, inserting the header into `header_map` and calling `may_set_shared_best_header` with an invalid chain tip (L1129–1140 of `sync/src/types/mod.rs`).

## Impact Explanation

**Primary — CPU-exhaustion DoS against a CKB node (High, 10001–15000 points):** The cost asymmetry is severe: the attacker sends a small P2P `SendHeaders` message (a few hundred bytes); the victim runs Eaglesong PoW verification, timestamp median-time computation, and epoch checks for each header. With `MAX_HEADERS_LEN = 2000` headers per message and multiple simultaneous connections, an attacker can saturate CPU on the victim node, causing it to become unresponsive or crash. This matches **"Vulnerabilities which could easily crash a CKB node"** and **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

**Secondary — Shared best-header state corruption:** Under the eviction race condition described above, `shared_best_header` can be set to an invalid chain tip, corrupting fork-choice and causing the node to request blocks on an invalid chain. This path requires a specific eviction condition and is lower likelihood, but the primary DoS path alone is sufficient for High severity.

## Likelihood Explanation

Any unprivileged peer can connect and send `SendHeaders` messages — no key, no hashpower, no special privilege required. The attack is self-bootstrapping: the attacker sends one invalid header (e.g., `version != 0`), waits for it to be stored as `BLOCK_INVALID`, then re-sends it indefinitely. The attacker can open multiple connections to amplify the effect. Even if the node disconnects/bans the peer after receiving invalid headers, the attacker can reconnect from different IPs or use the 2000-header batch to maximize CPU burn per connection before disconnection.

## Recommendation

Add an explicit `BLOCK_INVALID` early-exit at the top of `HeaderAcceptor::accept()`, resolving the existing `FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
```

This mirrors the pattern already correctly implemented in `contextual_check()` in `sync/src/relayer/compact_block_process.rs` (L259–261).

## Proof of Concept

1. Connect to a CKB node as an unprivileged peer.
2. Send a `SendHeaders` P2P message with a single header where `version != 0`. The node processes it through `HeaderAcceptor::accept()`, fails `version_check()` (L346–353), and stores the hash as `BLOCK_INVALID` in `block_status_map`.
3. Immediately re-send the identical header in a new `SendHeaders` message.
4. Instrument `non_contextual_check()` (L334) or `HeaderVerifier::verify()` with a counter/log. Observe that it is called again for the already-rejected header — confirming the `BLOCK_INVALID` guard is not triggered.
5. Repeat step 3 in a tight loop from multiple connections. Monitor CPU usage on the victim node; it will scale linearly with the send rate, demonstrating the DoS amplification.
6. For a unit test: call `HeaderAcceptor::accept()` twice on the same header after manually inserting `BLOCK_INVALID` into `block_status_map` via `shared.insert_block_status(hash, BlockStatus::BLOCK_INVALID)`. Assert that the second call returns `ValidationState::Invalid` without invoking `HeaderVerifier::verify()` (mock the verifier to assert it is not called on the second invocation).