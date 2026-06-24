Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return Causes Redundant Header Re-Verification — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` noting that it should return early when a header's status is already `BLOCK_INVALID`, but it does not. Because `BLOCK_INVALID = 1 << 12` does not set the `HEADER_VALID = 1` bit, the `status.contains(BlockStatus::HEADER_VALID)` guard at line 304 is false for `BLOCK_INVALID` headers, causing the code to fall through and redundantly re-execute `prev_block_check`, `non_contextual_check` (including `HeaderVerifier::verify()`), and `version_check` for every header already known to be invalid.

## Finding Description
In `HeaderAcceptor::accept()` (lines 295–358), after fetching the block status at line 303, only the `HEADER_VALID` path has an early-return (lines 304–322). The `BLOCK_INVALID` path has no early-return, as explicitly noted by the `FIXME` at lines 301–302. Since `BLOCK_INVALID = 1 << 12 = 4096` and `HEADER_VALID = 1`, the bitwise check `status.contains(BlockStatus::HEADER_VALID)` evaluates to `false` for any `BLOCK_INVALID` header, and execution falls through to lines 324–354 where `prev_block_check`, `non_contextual_check`, and `version_check` are all re-executed. The `non_contextual_check` calls `self.verifier.verify(self.header)` (line 256), which re-runs full header verification including PoW. The checks will ultimately fail again and return `ValidationState::Invalid`, causing `execute()` to return `StatusCode::HeadersIsInvalid` (code 415), which does trigger a peer ban via `should_ban()` (lines 165–179 of `status.rs`).

The claim's assertion that "the peer is never banned" is incorrect: `HeadersIsInvalid = 415` falls in the 4xx range, and `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` for all 4xx codes except `GetHeadersMissCommonAncestors`. The peer is banned after the first offending message. This means the "tight loop" DoS scenario requires the attacker to reconnect after each ban, which requires Sybil capability — contradicting the claim that "no Sybil capability" is needed.

The "state confusion" scenario (impact point 3) is also not caused by this bug. After a node restart, `block_status_map` is cleared entirely, so the child header's `BLOCK_INVALID` status is also gone; `get_block_status` returns `UNKNOWN` for it, and the missing early-return is never triggered. Partial clearing of the map would require a separate bug.

The actual impact is: for each `SendHeaders` message containing a known-`BLOCK_INVALID` header, the node performs redundant `HeaderVerifier::verify()` calls before arriving at the same `Invalid` result and banning the peer. This is wasted CPU per message, bounded to one message per peer connection before the ban.

## Impact Explanation
The concrete impact is redundant CPU computation — re-running `HeaderVerifier::verify()` (including Eaglesong PoW verification) for headers already cached as `BLOCK_INVALID` — before the peer is banned. This does not crash a node, does not cause consensus deviation, and does not cause network-wide congestion (the peer is banned after one message, requiring reconnection for each subsequent attempt). The impact fits **Low (501–2000 points): Any other important performance improvements for CKB**. The claimed High DoS impact is not proven because the ban mechanism is functional and limits each attacker connection to one offending message.

## Likelihood Explanation
Any P2P peer can send `SendHeaders` with a known-invalid header. However, the peer is banned after the first such message via `StatusCode::HeadersIsInvalid` → `should_ban()` → `BAD_MESSAGE_BAN_TIME`. Sustained attack requires reconnection after each ban, which requires either many IPs or repeated reconnections. The redundant computation per message is real but bounded and not catastrophic.

## Recommendation
Add an explicit early-return for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, resolving the `FIXME` at line 301:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent));
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return logic
}
```

## Proof of Concept
1. Connect to a CKB node as a P2P peer.
2. Send a `SendHeaders` message with a header `H` whose hash is in `block_status_map` as `BLOCK_INVALID`.
3. Observe via tracing/logging that `prev_block_check` and `non_contextual_check` (including `HeaderVerifier::verify()`) are executed before the `Invalid` result is returned — rather than returning immediately after the status check at line 303.
4. Confirm the peer is banned after this single message (expected behavior via `should_ban()`).
5. The `FIXME` at line 301 of `sync/src/synchronizer/headers_process.rs` is the developers' own acknowledgment of the missing path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L301-304)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
```

**File:** sync/src/synchronizer/headers_process.rs (L324-344)
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
```

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** shared/src/block_status.rs (L8-17)
```rust
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```
