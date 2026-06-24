Audit Report

## Title
Unbounded PoW Verification DoS via Unsolicited `SendHeaders` with Minimum-Difficulty Headers — (`sync/src/synchronizer/mod.rs`, `verification/src/header_verifier.rs`)

## Summary
The `Synchronizer` dispatches `SendHeaders` messages into `tokio::task::block_in_place` with no rate limiting, while `HeaderVerifier` never validates a header's `compact_target` against the epoch's expected difficulty. An unprivileged peer can send batches of up to 2,000 novel minimum-difficulty headers, each requiring a full Eaglesong PoW computation, blocking a tokio worker thread per batch and degrading block relay and tx-pool throughput.

## Finding Description

**No rate limiter on `SendHeaders`:**

`sync/src/synchronizer/mod.rs` L402–406 dispatches `SendHeaders` directly into `block_in_place` with no rate-limit guard:

```rust
packed::SyncMessageUnionReader::SendHeaders(reader) => {
    tokio::task::block_in_place(|| {
        HeadersProcess::new(reader, self, peer, &nc).execute()
    })
}
```

A `grep_search` for `rate_limiter` across all of `sync/src/synchronizer/` returns zero matches. By contrast, `sync/src/relayer/mod.rs` L116–123 explicitly guards every non-`CompactBlock` relay message with a per-`(peer, message_id)` rate limiter. No equivalent guard exists on the sync protocol path.

**Only a count ceiling — no difficulty floor:**

`HeadersProcess::execute` in `sync/src/synchronizer/headers_process.rs` L106–109 enforces only `MAX_HEADERS_LEN = 2,000` and continuity (L127–130). There is no check on `compact_target`.

**PoW runs on every novel header:**

`HeaderAcceptor::accept()` at L303–304 has an early exit only when `BlockStatus::HEADER_VALID` is already set. For any unseen header it calls `non_contextual_check` (L334), which calls `HeaderVerifier::verify()`.

`HeaderVerifier::verify()` in `verification/src/header_verifier.rs` L32–34 runs `PowVerifier` first, before any other check:

```rust
fn verify(&self, header: &Self::Target) -> Result<(), Error> {
    // POW check first
    PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

`PowVerifier::verify()` at L161–167 runs the full Eaglesong hash regardless of `compact_target`.

**`compact_target` is never validated against epoch difficulty:**

`EpochVerifier::verify()` at L133–148 checks only `is_well_formed()` and `is_successor_of()`. A `grep_search` for `compact_target` across all of `verification/src/` returns zero matches. An attacker can embed `compact_target = 0x207fffff` in every header, making valid Eaglesong PoW trivially cheap to compute while passing all checks.

**Timestamp constraint does not block the attack:**

`ALLOWED_FUTURE_BLOCKTIME = 15_000 ms` (15 seconds). A batch of 2,000 headers with timestamps incrementing by 1 ms spans only 2 seconds, well within the future window. The median-time lower bound advances slowly (CKB uses a 37-block median), so all 2,000 headers can carry valid timestamps. A "too new" timestamp would produce `TemporaryInvalid` and stop the batch early, but the attacker simply keeps timestamps at or just below `now + 15s` to avoid this.

**Sequential insertion enables full-chain processing:**

After each header passes verification, `insert_valid_header` is called at L356, making it available as a parent for the next header. All 2,000 headers in a single message are processed sequentially, each triggering a full Eaglesong computation.

## Impact Explanation

Each `SendHeaders` message with 2,000 novel minimum-difficulty headers forces 2,000 Eaglesong hash computations on the victim, blocking a tokio worker thread for the entire duration via `block_in_place`. With no rate limiting, an attacker can pipeline messages continuously, producing sustained CPU load and increased latency for block relay and tx-pool processing. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

- Requires only a standard P2P connection — no privilege, no key, no majority hashpower.
- Crafting 2,000 minimum-difficulty Eaglesong headers is trivially cheap; batches can be pre-computed offline.
- The `HEADER_VALID` early-exit means the attacker must use fresh headers per batch, but this is trivially cheap at `compact_target = 0x207fffff`.
- The node is not in IBD (the stated precondition), so the IBD-only disconnect logic does not apply.
- No existing guard (rate limiter, difficulty floor, or per-peer message quota) prevents this on the sync protocol path.

## Recommendation

1. **Add a rate limiter to `Synchronizer`** mirroring the one in `Relayer` — key by `(PeerIndex, message_item_id)`, capped at a reasonable RPS (e.g., 10–30/s).
2. **Validate `compact_target` in `HeaderVerifier`** against the epoch's expected difficulty, or enforce a consensus-defined floor on `compact_target` so that headers with trivially low difficulty are rejected before PoW verification.
3. **Consider banning peers** that send headers whose `compact_target` deviates significantly from the expected epoch difficulty, as this is unambiguously malicious on mainnet.

## Proof of Concept

```
1. Connect to victim node (out of IBD) via the Sync P2P protocol.
2. Obtain the current chain tip hash T and its epoch E.
3. Craft header H1: parent_hash=T, number=tip+1, epoch=successor(E),
   compact_target=0x207fffff, timestamp=now+1ms.
   Mine a valid Eaglesong nonce (trivial at 0x207fffff).
4. Craft H2..H2000 chaining from H1, each with compact_target=0x207fffff,
   incrementing timestamps by 1ms (total span 2s, within the 15s future window).
5. Send SendHeaders{headers: [H1..H2000]} → victim runs 2000 Eaglesong verifications
   synchronously inside block_in_place, blocking a tokio worker thread.
6. Craft a new batch (different nonces/hashes) and repeat in a tight loop.
7. Measure victim CPU utilization and block relay latency; assert both increase
   proportionally to message rate.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** sync/src/synchronizer/mod.rs (L402-406)
```rust
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L106-109)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L303-344)
```rust
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

**File:** sync/src/synchronizer/headers_process.rs (L354-357)
```rust
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** verification/src/header_verifier.rs (L32-34)
```rust
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** verification/src/header_verifier.rs (L161-167)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if self.pow.verify(&self.header.data()) {
            Ok(())
        } else {
            Err(PowError::InvalidNonce.into())
        }
    }
```

**File:** verification/src/lib.rs (L35-35)
```rust
pub const ALLOWED_FUTURE_BLOCKTIME: u64 = 15 * 1000; // 15 Second
```
