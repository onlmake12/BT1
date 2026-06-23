### Title
Unbounded PoW Verification DoS via Unsolicited `SendHeaders` with Minimum-Difficulty Headers — (`sync/src/synchronizer/headers_process.rs`, `verification/src/header_verifier.rs`)

---

### Summary

An unprivileged remote peer can send repeated `SendHeaders` messages each containing up to 2,000 headers with valid Eaglesong PoW at attacker-chosen minimum difficulty. The `Synchronizer` has no rate limiter for `SendHeaders`, and `HeaderVerifier` does not validate that a header's `compact_target` matches the epoch's expected difficulty. Each message forces up to 2,000 Eaglesong hash computations on the victim node, running synchronously via `tokio::task::block_in_place`, degrading block relay and tx-pool throughput.

---

### Finding Description

**Entry point:** Any connected peer sends a `SyncMessage::SendHeaders` P2P message.

**Dispatch — no rate limiting:**

`Synchronizer::try_process` dispatches `SendHeaders` directly to `HeadersProcess::execute()` with no rate-limit check:

```rust
packed::SyncMessageUnionReader::SendHeaders(reader) => {
    tokio::task::block_in_place(|| {
        HeadersProcess::new(reader, self, peer, &nc).execute()
    })
}
``` [1](#0-0) 

The `Synchronizer` struct carries no `rate_limiter` field. Compare with `Relayer`, which explicitly guards every non-CompactBlock message:

```rust
if should_check_rate && self.rate_limiter.check_key(&(peer, message.item_id())).is_err() {
    return StatusCode::TooManyRequests.with_context(message.item_name());
}
``` [2](#0-1) 

**Size check only — no difficulty validation:**

`HeadersProcess::execute` enforces only a count ceiling:

```rust
if headers.len() > MAX_HEADERS_LEN {   // MAX_HEADERS_LEN = 2_000
    return StatusCode::HeadersIsInvalid.with_context("oversize");
}
``` [3](#0-2) 

It then calls `accept_first` and iterates all remaining headers through `HeaderAcceptor::accept()`: [4](#0-3) 

**PoW runs on every novel header:**

`HeaderAcceptor::accept()` has an early exit only when `HEADER_VALID` is already set. For any header the node has not seen before, it calls `non_contextual_check`, which invokes `HeaderVerifier::verify()`:

```rust
fn verify(&self, header: &Self::Target) -> Result<(), Error> {
    // POW check first
    PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
    ...
}
``` [5](#0-4) 

`PowVerifier` runs the full Eaglesong hash regardless of the header's `compact_target`:

```rust
pub fn verify(&self) -> Result<(), Error> {
    if self.pow.verify(&self.header.data()) { Ok(()) }
    else { Err(PowError::InvalidNonce.into()) }
}
``` [6](#0-5) 

**`compact_target` is never validated against epoch difficulty in `HeaderVerifier`:**

`EpochVerifier` only checks epoch number continuity (`is_well_formed`, `is_successor_of`); it never compares `compact_target` to the epoch's expected difficulty: [7](#0-6) 

The contextual difficulty check exists only in the full block verifier, not in the sync-path `HeaderVerifier`. An attacker can therefore embed `compact_target = 0x207fffff` (minimum difficulty) in every header, making valid Eaglesong PoW trivially cheap to compute while still passing all checks in `HeaderVerifier`.

**`block_in_place` blocks the tokio runtime thread:**

The entire 2,000-header verification loop runs inside `tokio::task::block_in_place`, which stalls the tokio worker thread for the duration, delaying other async tasks (block relay, tx-pool notifications). [1](#0-0) 

---

### Impact Explanation

Each `SendHeaders` message with 2,000 novel minimum-difficulty headers forces 2,000 Eaglesong hash computations on the victim, blocking a tokio worker thread. With no rate limiting, an attacker can pipeline messages as fast as the network allows (~32 messages/s at 100 Mbps given ~384 KB per message), producing ~64,000 Eaglesong verifications per second per attacker connection. Multiple attacker connections multiply the load. The result is elevated CPU usage and increased latency for block relay and tx-pool processing.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privilege, no key, no majority hashpower.
- Crafting 2,000 minimum-difficulty Eaglesong headers takes microseconds; the attacker can pre-compute many batches offline.
- The `HEADER_VALID` early-exit means the attacker must use fresh headers per batch, but this is trivially cheap at minimum difficulty.
- The node is not in IBD (the question's stated precondition), so the IBD-only disconnect logic does not apply.
- No existing guard (rate limiter, difficulty floor, or per-peer message quota) prevents this on the sync protocol path.

---

### Recommendation

1. **Add a rate limiter to `Synchronizer`** mirroring the one in `Relayer` — key by `(PeerIndex, message_item_id)`, capped at a reasonable RPS (e.g., 10–30/s).
2. **Validate `compact_target` in `HeaderVerifier`** against the epoch's expected difficulty, or at minimum enforce a consensus-defined floor on `compact_target` so that headers with trivially low difficulty are rejected before PoW verification.
3. **Consider banning peers** that send headers whose `compact_target` deviates from the expected epoch difficulty, as this is unambiguously malicious on mainnet.

---

### Proof of Concept

```
1. Connect to victim node (out of IBD) via the Sync P2P protocol.
2. Obtain the current chain tip hash T and its epoch E.
3. Craft header H1: parent_hash=T, number=tip+1, epoch=successor(E),
   compact_target=0x207fffff, timestamp=now+1ms.
   Mine a valid Eaglesong nonce (trivial at 0x207fffff).
4. Craft H2..H2000 chaining from H1, each with compact_target=0x207fffff,
   incrementing timestamps by 1ms.
5. Send SendHeaders{headers: [H1..H2000]} → victim runs 2000 Eaglesong verifications.
6. Craft a new batch (different nonces/hashes) and repeat in a tight loop.
7. Measure victim CPU utilization and block relay latency; assert both increase
   proportionally to message rate.
```

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

**File:** sync/src/synchronizer/headers_process.rs (L132-179)
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
            ValidationState::TemporaryInvalid => {
                debug!(
                    "HeadersProcess accept_first result is temporary invalid, first header = {:?}",
                    headers[0]
                );
                return Status::ok();
            }
            ValidationState::Valid => {
                // Valid, do nothing
            }
        };

        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }
```

**File:** verification/src/header_verifier.rs (L32-50)
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
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
    }
```

**File:** verification/src/header_verifier.rs (L123-149)
```rust
pub struct EpochVerifier<'a> {
    parent: EpochNumberWithFraction,
    header: &'a HeaderView,
}

impl<'a> EpochVerifier<'a> {
    pub fn new(parent: EpochNumberWithFraction, header: &'a HeaderView) -> Self {
        EpochVerifier { parent, header }
    }

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
