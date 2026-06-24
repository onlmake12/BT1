**Code verification summary before ruling:**

All primary code claims check out against the actual source:

- `FILTER_SIZE = 50000` confirmed [1](#0-0) 
- `remove_expired()` unconditionally iterates the full `LruCache` with no early-exit or cooldown guard [2](#0-1) 
- `execute()` acquires `tx_filter` and calls `remove_expired()` while holding it on every incoming message [3](#0-2) 
- Rate limiter is keyed by `(PeerIndex, u32)` — per-peer, not global — at 30 req/s [4](#0-3) 
- The same pattern is duplicated in `send_bulk_of_tx_hashes()` [5](#0-4) 
- `MAX_RELAY_PEERS = 128` was **not found** in the codebase via search, so the exact global ceiling of 3,840/s is unverified, but the per-peer structure and the O(FILTER_SIZE)-under-mutex pattern are confirmed regardless of the exact peer cap.

---

Audit Report

## Title
O(FILTER_SIZE) Linear Scan Under Shared Mutex on Every `RelayTransactionHashes` Message — (`sync/src/relayer/transaction_hashes_process.rs`)

## Summary
`TransactionHashesProcess::execute()` acquires the global `tx_filter` mutex and unconditionally calls `remove_expired()` on every incoming `RelayTransactionHashes` P2P message. `remove_expired()` iterates all up to 50,000 entries of the `LruCache` with no early-exit, no cooldown, and no time-ordered index. Because the rate limiter is keyed per-peer (not globally), every connected peer can independently drive this O(50,000) scan at 30 msg/s, serializing all callers on the shared mutex and degrading relay throughput proportionally to peer count × message rate.

## Finding Description
In `execute()` (lines 38–47 of `transaction_hashes_process.rs`), the call sequence is:

```
state.tx_filter()  →  tx_filter.remove_expired()  →  .inner.iter() over all LruCache entries  →  .cloned().collect()  →  per-entry .pop()
```

`TtlFilter::remove_expired()` (lines 359–377 of `sync/src/types/mod.rs`) has no guard: it always calls `.inner.iter()` over the full `LruCache<T, u64>`, regardless of how many entries are expired or how recently expiry was last run. The `lru` crate's `LruCache` is backed by a doubly-linked list, so iteration involves pointer-chasing across up to 50,000 nodes (~1.6 MB of `Byte32` data), which is cache-unfriendly.

The `tx_filter` mutex is held for the entire duration of this scan (the `MutexGuard` is live across the `remove_expired()` call and the subsequent `filter` pass). The rate limiter (`RateLimiter::hashmap` keyed by `(PeerIndex, u32)`) enforces 30 msg/s per peer independently, so N connected peers each contribute N × 30 forced scans per second, all serializing on the same mutex.

The identical pattern appears in `send_bulk_of_tx_hashes()` (lines 677–685 of `mod.rs`), compounding contention from a second code path.

Existing checks that are insufficient:
- The `MAX_RELAY_TXS_NUM_PER_BATCH` check (line 29–35) bounds message payload size but does nothing to bound the cost of `remove_expired()`, which is independent of message content.
- The per-peer rate limiter prevents per-peer flooding but does not cap the aggregate scan rate across all peers.

## Impact Explanation
This maps to **High: bad designs which could cause CKB network congestion with few costs**. At realistic peer counts and the confirmed 30 msg/s per-peer rate, the mutex serializes O(FILTER_SIZE) work at a rate that grows linearly with connected peers. If each 50,000-entry LruCache iteration takes ~100–500 µs (plausible for pointer-chasing through a linked list), even a modest number of peers can hold the mutex for a significant fraction of wall-clock time per second, causing legitimate relay message processing to queue and increasing transaction propagation latency across the node. Because the `tx_filter` is shared across relay processing, this degrades the node's ability to participate in normal transaction relay, which at scale affects network-wide propagation.

## Likelihood Explanation
Any unprivileged peer can send `RelayTransactionHashes` messages up to the per-peer rate limit — no proof-of-work, no key, no special role required. The filter fills to capacity through normal network operation (legitimate tx hash announcements over 4-hour TTL windows), so the precondition of a near-full filter is met organically without attacker effort. The attack is trivially reproducible with a modified CKB client or a raw P2P message sender targeting the relay protocol.

## Recommendation
1. **Decouple expiry from message handling**: Move `remove_expired()` to a periodic background timer (e.g., the existing `TX_HASHES_TOKEN` notify loop) rather than calling it on every incoming message.
2. **Add a cooldown guard**: Track the last expiry timestamp inside `TtlFilter` and skip `remove_expired()` if called within a minimum interval (e.g., 60 seconds).
3. **Use a time-ordered side-index**: Replace the full-scan approach with a `BTreeMap<expiry_time, key>` so expiry is O(expired_count) rather than O(FILTER_SIZE).
4. **Add a global rate limit** on top of the per-peer limit to cap total `remove_expired()` invocations per second across all peers.

## Proof of Concept
```rust
// Fill tx_filter to capacity (50,000 entries, none expired — TTL is 4 hours)
let state = relayer.shared().state();
{
    let mut f = state.tx_filter();
    for i in 0..50_000u64 {
        f.insert(Byte32::from_slice(&i.to_le_bytes().repeat(4)).unwrap());
    }
}

// Time execute() with a single-hash RelayTransactionHashes message
// Expected: wall-clock time >> time with empty filter for identical 1-hash messages
// The difference is attributable entirely to the O(FILTER_SIZE) scan in remove_expired(),
// not to message content.

// Reproduce contention: connect N peers, each sending 30 RelayTransactionHashes/s.
// Observe: relay processing latency grows linearly with N due to mutex serialization.
```

### Citations

**File:** sync/src/types/mod.rs (L51-51)
```rust
const FILTER_SIZE: usize = 50000;
```

**File:** sync/src/types/mod.rs (L359-377)
```rust
    pub fn remove_expired(&mut self) {
        let now = ckb_systemtime::unix_time().as_secs();
        let expired_keys: Vec<T> = self
            .inner
            .iter()
            .filter_map(|(key, time)| {
                if *time + self.ttl < now {
                    Some(key)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        for k in expired_keys {
            self.remove(&k);
        }
    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-47)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };
```

**File:** sync/src/relayer/mod.rs (L81-92)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L677-685)
```rust
                        let tx_hashes: Vec<_> = {
                            let mut tx_filter = self.shared.state().tx_filter();
                            tx_filter.remove_expired();
                            parents
                                .into_iter()
                                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                                .collect()
                        };
                        self.shared.state().add_ask_for_txs(peer, tx_hashes);
```
