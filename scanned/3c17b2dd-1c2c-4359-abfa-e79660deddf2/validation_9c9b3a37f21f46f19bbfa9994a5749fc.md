Audit Report

## Title
Unbounded GetHeaders Loop via Sequential Header Batches Bypassing `pending_get_headers` Deduplication — (`sync/src/types/mod.rs`)

## Summary
An unprivileged remote peer can sustain a perpetual GetHeaders request loop against a victim node by repeatedly sending full 2000-header batches from a pre-mined private chain with minimum `compact_target`. The `pending_get_headers` LRU cache is keyed on `(peer, tip_hash)`, but sequential batches naturally end on different hashes, so the deduplication guard is structurally inert for the normal sequential-sync case. The victim verifies PoW on 2000 headers and emits a new GetHeaders for every batch received, indefinitely, with no rate limit or total-difficulty gate in `HeadersProcess::execute`.

## Finding Description

**Root cause — unconditional GetHeaders trigger:**
In `HeadersProcess::execute`, when exactly `MAX_HEADERS_LEN` (2000) headers are received and all pass validation, the node unconditionally calls `send_getheaders_to_peer` with the last header of the batch as the new starting point: [1](#0-0) 

**Root cause — deduplication keyed on tip hash:**
`send_getheaders_to_peer` suppresses duplicate requests only when the exact same `(peer, hash)` pair was seen within `GET_HEADERS_TIMEOUT`: [2](#0-1) 

Because the cache key is the **last header's hash** of each batch, and sequential batches end on different hashes (H1, H2, H3, …), every new batch inserts a fresh cache entry and triggers a new outbound GetHeaders. The deduplication is structurally inert for the normal sequential-sync case.

**Root cause — no `min_chain_work` gate in `HeadersProcess`:**
`min_chain_work` is declared in `SyncState` and checked in `BlockFetchCMD::can_start()` for block downloading, but it is entirely absent from `HeadersProcess::execute`. Headers from a low-difficulty private chain pass through without any total-difficulty gate: [3](#0-2) 

**Root cause — trivially cheap PoW:**
`HeaderVerifier::verify()` calls `PowVerifier` first, which checks the nonce against the header's own `compact_target`: [4](#0-3) 

`EpochVerifier::verify()` only validates epoch number/index/length via `is_successor_of`, not the `compact_target` value itself: [5](#0-4) 

An attacker can set `compact_target` to minimum difficulty (`0x207fffff`), making pre-mining millions of headers computationally trivial. The `TimestampVerifier` only requires each timestamp to exceed the median of the previous N blocks and not exceed `now + ALLOWED_FUTURE_BLOCKTIME`, which is easily satisfied with monotonically increasing past timestamps.

**Exploit flow:**
1. Attacker pre-mines a chain of N headers with `compact_target = 0x207fffff` (one-time offline cost).
2. Attacker connects to victim node as an unprivileged peer.
3. Attacker sends headers 1–2000; victim validates PoW on all 2000, inserts cache entry `(peer, H2000)`, and emits GetHeaders.
4. Attacker sends headers 2001–4000; victim looks up `(peer, H4000)` — cache miss — validates PoW on 2000 more headers, inserts `(peer, H4000)`, emits another GetHeaders.
5. Repeat indefinitely. No suppression ever fires.

## Impact Explanation

For each 2000-header batch the attacker sends, the victim performs PoW verification on 2000 headers (CPU cost) and emits a GetHeaders message (bandwidth cost). With a pre-mined chain of N headers, the attacker drives N/2000 round-trips. With minimum `compact_target`, N can be arbitrarily large at negligible cost. Targeting many nodes simultaneously produces network-wide CPU and bandwidth exhaustion.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The attack path is entirely P2P, requires no credentials or special privileges, and the bypass is structural (not a timing race or edge case). Pre-mining a long low-difficulty chain is a one-time offline cost. The attacker can replay the same pre-mined chain against many victims simultaneously. There is no per-peer rate limit on inbound `SendHeaders` messages at the sync protocol level.

## Recommendation

1. **Key `pending_get_headers` on `(peer, number)` or a per-peer request counter** rather than on the tip hash, so sequential batches from the same peer are rate-limited regardless of hash rotation.
2. **Add a per-peer rate limit on inbound `SendHeaders` messages** (e.g., max N batches per time window per peer).
3. **Enforce a minimum `compact_target` (tied to `min_chain_work`) inside `HeadersProcess::execute`** before accepting headers and triggering further GetHeaders — reject or ignore header batches whose cumulative difficulty falls below the configured `min_chain_work` threshold.

## Proof of Concept

Pre-mine a chain of 200,000 headers with `compact_target = 0x207fffff` and monotonically increasing valid timestamps. Connect to a victim node as an unprivileged peer. Send headers 1–2000; observe victim replies with GetHeaders (cache entry `(peer, H2000)` inserted). Send headers 2001–4000; observe victim replies again (cache entry `(peer, H4000)` inserted — different key, no suppression). Repeat 100 times. Assert that the victim sent exactly 100 outbound GetHeaders messages — one per batch — with zero suppression from `pending_get_headers`. Measure victim CPU during the run to confirm exhaustion proportional to batch count. [1](#0-0) [6](#0-5)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L183-186)
```rust
        if headers.len() == MAX_HEADERS_LEN {
            let start = headers.last().expect("empty checked").into();
            self.active_chain
                .send_getheaders_to_peer(self.nc, self.peer, start);
```

**File:** sync/src/types/mod.rs (L1340-1340)
```rust
    min_chain_work: U256,
```

**File:** sync/src/types/mod.rs (L1923-1951)
```rust
    pub fn send_getheaders_to_peer(
        &self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        block_number_and_hash: BlockNumberAndHash,
    ) {
        if let Some(last_time) = self
            .state()
            .pending_get_headers
            .write()
            .get(&(peer, block_number_and_hash.hash()))
        {
            if Instant::now() < *last_time + GET_HEADERS_TIMEOUT {
                debug!(
                    "Last get_headers request to peer {} is less than {:?}; Ignore it.",
                    peer, GET_HEADERS_TIMEOUT,
                );
                return;
            } else {
                debug!(
                    "Can not get headers from {} in {:?}, retry",
                    peer, GET_HEADERS_TIMEOUT,
                );
            }
        }
        self.state()
            .pending_get_headers
            .write()
            .put((peer, block_number_and_hash.hash()), Instant::now());
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
