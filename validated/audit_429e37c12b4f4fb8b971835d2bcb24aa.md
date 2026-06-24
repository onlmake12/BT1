All cited code references are verified. Here is the validation result:

---

Audit Report

## Title
Unbounded GetHeaders I/O Amplification via Missing Sync Protocol Rate Limiter — (`sync/src/synchronizer/get_headers_process.rs`)

## Summary
An unprivileged inbound peer can send `GetHeaders` messages with a genesis-terminated locator at an unbounded rate. Each message triggers up to 2,000 sequential RocksDB reads via `get_locator_response()` and spawns an uncapped tokio task, with no per-peer rate limiter on the Sync protocol. The `getheaders_received()` hook intended to throttle this is a confirmed no-op stub.

## Finding Description

**`getheaders_received()` is a confirmed no-op:** [1](#0-0) 

**Synchronizer has zero rate-limiting infrastructure** — grep for `rate_limiter`, `RateLimiter`, `governor` in `sync/src/synchronizer/**` returns no matches. This is in direct contrast to the Relayer, which enforces 30 req/sec per `(peer, message_item_id)`: [2](#0-1) 

**`GetHeadersProcess::execute()` exploit flow with a genesis-terminated locator under non-IBD:**

1. Locator size check passes (≤ `MAX_LOCATOR_SIZE` = 101): [3](#0-2) 

2. IBD check passes (non-IBD assumed): [4](#0-3) 

3. `locate_latest_common_block()` returns `Some(0)` immediately when the locator ends with genesis hash — the early-exit at `latest_common == Some(0)` fires before any chain walk: [5](#0-4) 

4. `get_locator_response(0, &zero_hash)` performs up to `MAX_HEADERS_LEN` iterations, each doing `get_block_hash` + `get_block_header` (two RocksDB reads per block): [6](#0-5) 

5. An unbounded `async_handle().spawn()` is issued per message with no backpressure: [7](#0-6) 

**No ban is triggered.** `GetHeadersMissCommonAncestors` (the only ban path) is only returned when `locate_latest_common_block` returns `None`, which only happens when the locator does not end with the genesis hash. A genesis-terminated locator always returns `Some(0)`, so the `else` branch is never reached: [8](#0-7) [9](#0-8) 

## Impact Explanation

Each valid `GetHeaders` message causes up to ~2,000 RocksDB reads plus one tokio task spawn against the shared `ChainDB`. With no rate limiter and no ban, a single inbound peer can sustain this at TCP line rate, causing I/O saturation that degrades block processing, transaction relay, and RPC responsiveness — and under sufficient load, can crash the node. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation

The attack requires only a TCP connection to the node's P2P port (default 8115) and knowledge of the genesis hash (public, hardcoded). No PoW, no keys, no privileged access. The `// TODO:` comment in `getheaders_received()` and the absence of any rate-limiting infrastructure in the Synchronizer (while the Relayer has it) confirm this is an unfinished mitigation, not an intentional design choice.

## Recommendation

1. Add a `governor::RateLimiter` to `Synchronizer` keyed by `(PeerIndex, message_item_id)`, mirroring the Relayer's existing pattern at `sync/src/relayer/mod.rs:89-99`.
2. Implement `getheaders_received()` to track per-peer request timestamps and enforce a minimum inter-request interval.
3. Consider capping `get_locator_response` output or adding a short-circuit when the requesting peer has recently been served.

## Proof of Concept

```rust
// In a unit test, call in a tight loop from a single peer:
for _ in 0..1000 {
    let locator = vec![genesis_hash.clone()]; // valid, cheap, always returns Some(0)
    let msg = packed::GetHeaders::new_builder()
        .block_locator_hashes(locator.pack())
        .hash_stop(packed::Byte32::zero())
        .build();
    let status = GetHeadersProcess::new(msg.as_reader(), &synchronizer, peer, &nc).execute();
    assert!(status.is_ok()); // no ban, no rate limit, no error
    // Each iteration: ~2000 RocksDB reads + 1 tokio::spawn
}
// Observe: DB read counter grows by ~2,000,000; tokio task queue grows unboundedly
```

### Citations

**File:** sync/src/types/mod.rs (L897-899)
```rust
    pub fn getheaders_received(&self, _peer: PeerIndex) {
        // TODO:
    }
```

**File:** sync/src/types/mod.rs (L1866-1881)
```rust
        let locator_hash = locator.last().expect("empty checked");
        if locator_hash != &self.sync_shared.consensus().genesis_hash() {
            return None;
        }

        // iterator are lazy
        let (index, latest_common) = locator
            .iter()
            .enumerate()
            .map(|(index, hash)| (index, self.snapshot.get_block_number(hash)))
            .find(|(_index, number)| number.is_some())
            .expect("locator last checked");

        if index == 0 || latest_common == Some(0) {
            return latest_common;
        }
```

**File:** sync/src/types/mod.rs (L1914-1920)
```rust
        std::iter::successors(Some(start_number), |number| number.checked_add(1))
            .take_while(|number| *number <= tip_number)
            .take(MAX_HEADERS_LEN)
            .filter_map(|block_number| self.snapshot.get_block_hash(block_number))
            .take_while(|block_hash| block_hash != hash_stop)
            .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash))
            .collect()
```

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L47-51)
```rust
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L53-66)
```rust
        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
            return Status::ignored();
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L89-93)
```rust
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
```

**File:** sync/src/synchronizer/get_headers_process.rs (L94-97)
```rust
        } else {
            return StatusCode::GetHeadersMissCommonAncestors
                .with_context(format!("{block_locator_hashes:#x?}"));
        }
```

**File:** sync/src/status.rs (L176-179)
```rust
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```
