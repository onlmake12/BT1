Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Causes Redundant PoW Re-verification — (File: sync/src/synchronizer/headers_process.rs)

## Summary

`HeaderAcceptor::accept()` checks only for `HEADER_VALID` when reading cached block status, skipping an early-return for `BLOCK_INVALID`. A developer-acknowledged `FIXME` at lines 301–302 confirms the gap. Any peer can repeatedly send `SendHeaders` messages containing a previously-rejected header, forcing one Eaglesong PoW hash computation per message with no rate-limit or ban applied.

## Finding Description

In `sync/src/synchronizer/headers_process.rs`, `accept()` reads the cached status and short-circuits only on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
``` [1](#0-0) 

`BLOCK_INVALID = 1 << 12` is bitwise-disjoint from `HEADER_VALID = 1`, so `status.contains(BlockStatus::HEADER_VALID)` is always `false` for an invalid header and the early-return is never triggered. [2](#0-1) 

Execution falls through to `non_contextual_check`, which calls `HeaderVerifier::verify()`. That function runs `PowVerifier` as its **first** step:

```rust
fn verify(&self, header: &Self::Target) -> Result<(), Error> {
    // POW check first
    PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
``` [3](#0-2) 

The `execute()` loop does break on the first `ValidationState::Invalid` result, so only one header per message triggers re-verification before the function returns. [4](#0-3) 

By contrast, `compact_block_process.rs` correctly short-circuits on `BLOCK_INVALID` before any verification:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [5](#0-4) 

## Impact Explanation

**Low (501–2000 points): Any other important performance improvements for CKB.** The concrete impact is one Eaglesong hash computation per malicious `SendHeaders` message, with no rate-limit or peer-ban applied to senders of already-invalid headers. Eaglesong is a single hash — fast in absolute terms — so this does not plausibly crash a node or cause network-wide congestion. The impact is a confirmed performance regression: unbounded CPU waste proportional to message rate from a malicious peer, with zero back-pressure. This does not meet the High DoS tier.

## Likelihood Explanation

Triggerable by any unprivileged peer with a standard sync connection. The attacker needs one previously-rejected header hash, obtainable by sending a single malformed header and observing the rejection. The `FIXME` comment in production source confirms developer awareness. No special privileges, key material, or hashpower are required.

## Recommendation

Add an explicit `BLOCK_INVALID` early-return at the top of `accept()`, mirroring `compact_block_process.rs`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing peer-state update
    return result;
}
``` [1](#0-0) 

## Proof of Concept

1. Connect to a CKB node as a standard sync peer.
2. Send a `SendHeaders` message with one header `H` whose PoW nonce is invalid. The node runs `PowVerifier`, fails, and marks `H` as `BLOCK_INVALID` via `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)`. [6](#0-5) 
3. Repeatedly send `SendHeaders` messages each containing `H` as the first header.
4. For each message, `accept()` reads `status = BLOCK_INVALID`, does not short-circuit (disjoint from `HEADER_VALID`), and re-executes `PowVerifier::verify()` before returning `ValidationState::Invalid`.
5. CPU cost per message is one Eaglesong hash; attacker cost is one TCP packet. The ratio is bounded but non-zero and unbounded in aggregate across message rate.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L132-141)
```rust
        let result = self.accept_first(&headers[0]);
        match result.state {
            ValidationState::Invalid => {
                debug!(
                    "HeadersProcess accept_first result is invalid, error = {:?}, first header = {:?}",
                    result.error, headers[0]
                );
                return StatusCode::HeadersIsInvalid
                    .with_context(format!("accept first header {:?}", headers[0]));
            }
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

**File:** sync/src/synchronizer/headers_process.rs (L339-342)
```rust
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
```

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
