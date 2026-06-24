Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept()` Causes Redundant PoW Re-verification — (`File: sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::accept()` in the `SendHeaders` message handler lacks an early-return for headers already cached as `BLOCK_INVALID`. A developer-acknowledged `FIXME` comment at lines 301–302 confirms the gap. Any peer can repeatedly send `SendHeaders` messages containing previously-rejected headers, forcing the node to re-execute `HeaderVerifier::verify()` — including Eaglesong PoW computation — on every delivery instead of performing a single hash-map lookup.

## Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` reads the cached block status and short-circuits only on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer state and return
}
``` [1](#0-0) 

`BLOCK_INVALID` is defined as `1 << 12`, which is disjoint from `HEADER_VALID = 1`, so `status.contains(BlockStatus::HEADER_VALID)` is always `false` for an invalid header and the early-return is never triggered. [2](#0-1) 

When the early-return is missed, execution falls through to `non_contextual_check`, which calls `HeaderVerifier::verify()`. That function runs `PowVerifier` as its **first** step before any cheaper checks:

```rust
fn verify(&self, header: &Self::Target) -> Result<(), Error> {
    // POW check first
    PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
    ...
    NumberVerifier::new(parent_fields.number, header).verify()?;
``` [3](#0-2) 

The `execute()` loop does break on the first `ValidationState::Invalid` result, so only one header per message is re-verified before the function returns. However, the attacker can send a new `SendHeaders` message immediately, triggering another full verification cycle with no back-pressure. [4](#0-3) 

By contrast, `compact_block_process.rs` correctly short-circuits on `BLOCK_INVALID` before any verification:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [5](#0-4) 

## Impact Explanation

The impact is repeated, unnecessary CPU work (one Eaglesong hash per message) with no rate-limit or peer-ban applied to senders of already-invalid headers. Because `execute()` returns after the first invalid header, the attacker gets one PoW verification per message, not `MAX_HEADERS_LEN`. Eaglesong verification is a single hash computation — fast in absolute terms. This does not plausibly crash a node or cause network-wide congestion. The concrete impact is a **performance regression / suboptimal implementation** that wastes CPU proportional to the message rate from a malicious peer. This maps to **Low (501–2000 points): any other important performance improvements for CKB**, not to the High DoS tier claimed.

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
``` [6](#0-5) 

## Proof of Concept

1. Connect to a CKB node as a standard sync peer.
2. Send a `SendHeaders` message with one header `H` whose PoW nonce is invalid. The node runs `PowVerifier`, fails, marks `H` as `BLOCK_INVALID`.
3. Repeatedly send `SendHeaders` messages each containing `H` as the first header.
4. For each message, `accept()` reads `status = BLOCK_INVALID`, does not short-circuit, and re-executes `PowVerifier::verify()` (one Eaglesong hash) before returning `ValidationState::Invalid`.
5. CPU cost per message is one Eaglesong hash; the attacker's cost is one UDP/TCP packet. The ratio is bounded but non-zero and unbounded in aggregate across message rate. [7](#0-6)

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

**File:** sync/src/synchronizer/headers_process.rs (L295-344)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

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

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** verification/src/header_verifier.rs (L32-41)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
