All core code claims check out against the actual source. Here is the validation result:

---

Audit Report

## Title
O(FILTER_SIZE) Linear Scan Under Shared Mutex on Every RelayTransactionHashes Message — (`sync/src/relayer/transaction_hashes_process.rs`)

## Summary
`TransactionHashesProcess::execute()` acquires the `tx_filter` mutex on every incoming `RelayTransactionHashes` P2P message and immediately calls `TtlFilter::remove_expired()`, which unconditionally iterates the entire `LruCache` (capacity `FILTER_SIZE`) to collect and remove expired entries. The per-peer rate limiter (30 msg/s, keyed by `(PeerIndex, item_id())`) does not bound the aggregate scan rate across all peers, allowing up to `MAX_RELAY_PEERS × 30` forced full-filter scans per second, all serializing on the shared mutex.

## Finding Description
**Root cause — `TtlFilter::remove_expired()`** at `sync/src/types/mod.rs` lines 359–377: iterates the entire `inner: LruCache<T, u64>`, clones all expired keys into a `Vec`, then calls `self.remove()` for each. There is no early-exit, no timestamp-ordered side-index, and no cooldown guard — the full O(FILTER_SIZE) scan always runs regardless of filter occupancy or elapsed time since the last expiry pass. [1](#0-0) 

**Trigger path — `TransactionHashesProcess::execute()`** at `sync/src/relayer/transaction_hashes_process.rs` lines 38–47: acquires the `tx_filter` mutex via `state.tx_filter()`, immediately calls `tx_filter.remove_expired()`, and holds the mutex for the entire scan before filtering the incoming hashes. [2](#0-1) 

**Same pattern repeated** in the `TxVerificationResult::UnknownParents` branch at `sync/src/relayer/mod.rs` lines 677–684. [3](#0-2) 

**Rate limiter is per-peer, not global**: keyed by `(PeerIndex, message.item_id())` at lines 116–123, with a quota of 30/s per peer set at lines 89–92. [4](#0-3) [5](#0-4) 

`MAX_RELAY_PEERS = 128` at line 59 gives a global ceiling of 3,840 `RelayTransactionHashes` messages/second, each triggering a full O(FILTER_SIZE) LruCache iteration under the mutex. [6](#0-5) 

**Existing guard is insufficient**: the `MAX_RELAY_TXS_NUM_PER_BATCH` check at lines 29–35 only limits hash count per message; it does not reduce the frequency of `remove_expired()` invocations. [7](#0-6) 

## Impact Explanation
At 3,840 forced scans/second, if each O(FILTER_SIZE) LruCache iteration (pointer-chasing through a doubly-linked list) takes 100–500 µs, the mutex is held for 384–1,920 ms per second of wall time. This serializes all concurrent relay message processing threads on the shared mutex, causing degraded relay throughput and increased transaction propagation latency across any node under attack. An attacker operating 128 peers (or coordinating with others) can sustain this continuously at minimal cost, matching **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
Any unprivileged peer can send `RelayTransactionHashes` messages up to the per-peer rate limit with no PoW, no key, and no special role. The `tx_filter` fills to capacity through normal network operation (legitimate tx hash announcements over the TTL window), so the precondition of a near-full filter is met organically. The attack is trivially reproducible with a modified CKB client or a raw P2P message sender. Multiple attacker-controlled peers compound the effect linearly up to `MAX_RELAY_PEERS`.

## Recommendation
1. **Decouple expiry from message handling**: Move `remove_expired()` to the existing `TX_HASHES_TOKEN` periodic timer (300 ms intervals in `sync/src/relayer/mod.rs`) rather than calling it on every incoming message.
2. **Add a cooldown guard**: Track the last expiry timestamp inside `TtlFilter` and skip `remove_expired()` if called within a minimum interval (e.g., 60 seconds).
3. **Use a time-ordered structure**: Replace the full-scan approach with a `BTreeMap<expiry_time, key>` side-index so expiry is O(expired_count) not O(FILTER_SIZE).
4. **Add a global rate limit** on `remove_expired()` invocations per second, independent of per-peer limits.

## Proof of Concept
```rust
// Fill tx_filter to capacity (FILTER_SIZE entries)
{
    let mut f = state.tx_filter();
    for i in 0u64..50_000 {
        f.insert(Byte32::from_slice(&i.to_le_bytes().repeat(4)).unwrap());
    }
}
// Benchmark: time execute() with a 1-hash RelayTransactionHashes message
// at filter capacity vs. empty filter.
// Expected: wall-clock time with 50k entries >> time with 0 entries
// for identical 1-hash messages, proving cost is O(FILTER_SIZE) not O(message_size).
// Repeat 30x/second from a single peer; scale to 128 peers to observe
// mutex contention serializing relay processing threads.
```

### Citations

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

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
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

**File:** sync/src/relayer/mod.rs (L59-60)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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

**File:** sync/src/relayer/mod.rs (L677-684)
```rust
                        let tx_hashes: Vec<_> = {
                            let mut tx_filter = self.shared.state().tx_filter();
                            tx_filter.remove_expired();
                            parents
                                .into_iter()
                                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                                .collect()
                        };
```
