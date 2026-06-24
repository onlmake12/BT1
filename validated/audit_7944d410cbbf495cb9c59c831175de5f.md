Audit Report

## Title
O(FILTER_SIZE) Linear Scan Under Shared Mutex on Every `RelayTransactionHashes` Message — (`sync/src/relayer/transaction_hashes_process.rs`)

## Summary
`TransactionHashesProcess::execute()` acquires the global `tx_filter` mutex and unconditionally calls `remove_expired()`, which iterates all 50,000 entries of the `LruCache` on every incoming `RelayTransactionHashes` P2P message with no cooldown or early-exit. Because the rate limiter is keyed per-peer rather than globally, up to 128 peers × 30 msg/s = 3,840 forced O(50,000) scans per second can be induced by unprivileged remote peers, serializing on the shared mutex and degrading relay message processing throughput.

## Finding Description
**Root cause — `remove_expired()` in `sync/src/types/mod.rs` (L359–377):**

`TtlFilter::remove_expired()` unconditionally calls `self.inner.iter()` over the full `LruCache<T, u64>` (capacity `FILTER_SIZE = 50000`), collects all expired keys, then removes them. There is no timestamp-ordered side-index, no early-exit, and no cooldown guard tracking when expiry was last run. [1](#0-0) [2](#0-1) 

**Trigger path — `execute()` in `sync/src/relayer/transaction_hashes_process.rs` (L38–47):**

On every incoming `RelayTransactionHashes` message, `execute()` acquires the `tx_filter` mutex guard and immediately calls `remove_expired()` before filtering the incoming hashes. The mutex is held for the entire O(50,000) scan. [3](#0-2) 

**Rate limiter is per-peer, not global — `sync/src/relayer/mod.rs` (L81, L89–92, L116–123):**

The `RateLimiter` is keyed by `(PeerIndex, u32)`. Each peer independently gets 30 msg/s for each message type. With up to 128 relay peers, the global ceiling is 3,840 `RelayTransactionHashes` messages/second, each triggering a full O(50,000) scan under the mutex. [4](#0-3) [5](#0-4) 

**Same pattern in `send_bulk_of_tx_hashes()` — `sync/src/relayer/mod.rs` (L677–685):**

The identical `tx_filter.lock() → remove_expired()` pattern appears in the outbound relay path, compounding mutex contention. [6](#0-5) 

**Existing guards are insufficient:** The only guard is the per-peer rate limit (30/s), which does not bound the global scan rate. There is no global rate limit, no cooldown on `remove_expired()`, and no time-ordered structure to make expiry sub-linear.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

At 3,840 forced O(50,000) LruCache iterations per second (pointer-chasing through a doubly-linked list of ~2 MB of `Byte32` data), the `tx_filter` mutex becomes a serialization bottleneck. All concurrent relay message handlers queue behind attacker-induced scans. This degrades transaction hash relay throughput on targeted nodes, slowing transaction propagation across the network. The cost to the attacker is negligible: maintain 128 connections and send empty or minimal `RelayTransactionHashes` messages at 30/s each. [7](#0-6) 

## Likelihood Explanation
Any unprivileged peer can send `RelayTransactionHashes` messages — no PoW, no key, no special role required. The filter fills to capacity through normal network operation (legitimate tx hash announcements over 4-hour TTL windows), so the O(50,000) worst case is met organically. The attack is trivially reproducible with a modified CKB client or a raw P2P message sender maintaining connections to the target node. [8](#0-7) 

## Recommendation
1. **Decouple expiry from message handling**: Move `remove_expired()` to a periodic background timer (e.g., the existing `TX_HASHES_TOKEN` notify interval) rather than calling it on every incoming message.
2. **Add a cooldown guard**: Track the last expiry timestamp inside `TtlFilter` and skip `remove_expired()` if called within a minimum interval (e.g., 60 seconds).
3. **Use a time-ordered side-index**: Replace the full-scan approach with a `BTreeMap<expiry_time, key>` so expiry is O(expired_count) not O(FILTER_SIZE).
4. **Add a global rate limit** on `remove_expired()` invocations in addition to the per-peer message rate limit. [9](#0-8) 

## Proof of Concept
```rust
// 1. Connect 128 peers to the target node (or simulate with a benchmark)
// 2. Fill tx_filter to capacity (50,000 entries) via normal tx hash announcements
// 3. Each peer sends RelayTransactionHashes at 30 msg/s with a single hash
// 4. Measure: relay message processing latency with 0 vs 50,000 filter entries
//    Expected: latency proportional to FILTER_SIZE, not message content size

let state = relayer.shared().state();
{
    let mut f = state.tx_filter();
    for i in 0..50_000u64 {
        f.insert(Byte32::from_slice(&i.to_le_bytes().repeat(4)).unwrap());
    }
}
// Time a single execute() call with 1-hash message:
// Assert: time(50k entries) >> time(0 entries) for identical 1-hash messages
// Assert: mutex contention increases with peer count
``` [3](#0-2) [1](#0-0)

### Citations

**File:** sync/src/types/mod.rs (L51-54)
```rust
const FILTER_SIZE: usize = 50000;
// 2 ** 13 < 6 * 1800 < 2 ** 14
const ONE_DAY_BLOCK_NUMBER: u64 = 8192;
pub(crate) const FILTER_TTL: u64 = 4 * 60 * 60;
```

**File:** sync/src/types/mod.rs (L358-377)
```rust
    /// Removes expired items.
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

**File:** sync/src/relayer/transaction_hashes_process.rs (L25-50)
```rust
    pub fn execute(self) -> Status {
        let state = self.relayer.shared().state();
        {
            let relay_transaction_hashes = self.message;
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
        }

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

        state.add_ask_for_txs(self.peer, tx_hashes)
    }
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
