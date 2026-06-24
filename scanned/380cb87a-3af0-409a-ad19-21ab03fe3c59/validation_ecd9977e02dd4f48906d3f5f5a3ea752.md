Audit Report

## Title
Missing `BLOCK_INVALID` Early-Exit Guard in `HeaderAcceptor::accept()` Causes Redundant PoW Re-verification — (`sync/src/synchronizer/headers_process.rs`)

## Summary
In `HeaderAcceptor::accept()`, the block status is fetched and checked only for `HEADER_VALID`, while the `BLOCK_INVALID` flag — though present in the fetched status — is never tested. A developer `FIXME` comment at lines 301–302 explicitly acknowledges this gap. As a result, any header previously marked `BLOCK_INVALID` will fall through to `non_contextual_check`, which re-runs `HeaderVerifier::verify()` including PoW (Eaglesong) verification, on every subsequent delivery. The impact is bounded to a performance regression rather than a sustained single-peer DoS, because `HeadersIsInvalid` (status 415) triggers peer banning via `should_ban()`.

## Finding Description
In `sync/src/synchronizer/headers_process.rs` lines 301–322, `accept()` fetches the block status and short-circuits only on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ...update peer state and return
    return result;
}
```

There is no corresponding guard for `BlockStatus::BLOCK_INVALID`. The code falls through unconditionally to:
1. `prev_block_check` (lines 324–332) — checks parent validity
2. `non_contextual_check` (lines 334–344) — calls `self.verifier.verify(self.header)`, which includes Eaglesong PoW verification
3. `version_check` (lines 346–354)

`BLOCK_INVALID` is set in all three failure branches (lines 330, 341, 352) but is never consulted on re-entry. The contrast with `compact_block_process.rs` lines 259–261 is direct — that path explicitly checks `BLOCK_INVALID` and returns early.

**Banning mitigates the single-peer loop claim**: `StatusCode::HeadersIsInvalid` (415) falls in the 4xx range, so `should_ban()` in `sync/src/status.rs` lines 165–179 returns `Some(BAD_MESSAGE_BAN_TIME)`. The synchronizer (confirmed by 3 `should_ban` call-sites in `mod.rs`) bans the peer after the first invalid-header delivery. A single peer therefore cannot sustain a tight-loop attack; the attacker must rotate IP addresses (Sybil), raising the cost significantly beyond "few costs."

The residual issue is that every new peer connection delivering the same known-invalid header causes one unnecessary full PoW re-verification before the ban takes effect, and that the `BLOCK_INVALID` cache entry is never consulted as intended.

## Impact Explanation
**Low (501–2000 points) — Important performance improvement for CKB.**

The missing guard causes avoidable Eaglesong PoW re-verification for every new peer that delivers a header already cached as `BLOCK_INVALID`. Because the peer is banned immediately after the first delivery, a sustained single-peer CPU-exhaustion attack is not achievable without a large pool of distinct IP addresses. The impact does not reach "High" (network congestion with few costs) because the banning mechanism imposes a meaningful per-connection cost on the attacker. The correct classification is a performance deficiency: the `BLOCK_INVALID` cache exists precisely to avoid this redundant work, but is never consulted on re-entry.

## Likelihood Explanation
Any unauthenticated P2P peer can send `SendHeaders` messages. Triggering the redundant verification requires only a structurally valid but PoW-failing header. However, each triggering peer is banned after one delivery, so sustaining meaningful CPU pressure requires a Sybil-scale IP pool. The likelihood of causing node-level impact from a single attacker without significant infrastructure is low.

## Recommendation
Add an explicit early-return for `BLOCK_INVALID` immediately after the `HEADER_VALID` check in `HeaderAcceptor::accept()`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ...existing logic...
    return result;
}
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}
```

This resolves the developer `FIXME`, makes the `BLOCK_INVALID` cache entry useful on re-entry, and aligns with the pattern already used in `compact_block_process.rs`.

## Proof of Concept
1. Connect to a CKB node as an unauthenticated P2P peer.
2. Craft a `SendHeaders` message with a header that has a valid structure but an invalid PoW nonce (Eaglesong target not satisfied).
3. Send the message once. The node runs `non_contextual_check` → `HeaderVerifier::verify()` → PoW fails → `insert_block_status(hash, BLOCK_INVALID)`. The peer is then banned (`HeadersIsInvalid` → `should_ban()` → `BAD_MESSAGE_BAN_TIME`).
4. Reconnect from a different IP and re-send the identical header.
5. Observe that `accept()` again falls through to `non_contextual_check` and re-runs PoW verification, despite the hash being present in the `BLOCK_INVALID` cache.
6. Each new IP triggers one redundant PoW verification before the ban; the `BLOCK_INVALID` cache entry is never used as intended.