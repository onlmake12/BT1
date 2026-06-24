Audit Report

## Title
O(FILTER_SIZE) Linear Scan Under Shared Mutex on Every RelayTransactionHashes Message — (`sync/src/relayer/transaction_hashes_process.rs`)

## Summary
Every `RelayTransactionHashes` P2P message causes `TransactionHashesProcess::execute()` to acquire the global `tx_filter` mutex and call `remove_expired()`, which unconditionally iterates all 50,000 entries of the `LruCache` regardless of how many are actually expired. The rate limiter is keyed per-peer, not globally, allowing up to 128 peers × 30 msg/s = 3,840 forced full-filter scans per second, all serializing on the shared mutex and degrading relay message processing throughput.

## Finding Description
`TtlFilter::remove_expired()` at `sync/src/types/mod.rs` lines 359–377 iterates the entire `inner: LruCache<T, u64>` (capacity `FILTER_SIZE = 50000`), clones expired keys into a `Vec`, then calls `self.remove()` for each. There is no early-exit, no timestamp-ordered side-index, and no cooldown guard — the full scan always runs regardless of filter occupancy or time since last expiry.

`TransactionHashesProcess::execute()` at `sync/src/relayer/transaction_hashes_process.rs` lines 38–47 acquires the `tx_filter` mutex, immediately calls `tx_filter.remove_expired()`, and holds the mutex for the entire O(50,000) scan before filtering the incoming hashes. The same pattern is repeated in `send_bulk_of_tx_hashes()` at `sync/src/relayer/mod.rs` lines 677–684.

The rate limiter at `sync/src/relayer/mod.rs` lines 89–92 and 116–123 is keyed by `(PeerIndex, message.item_id())` — per-peer, not global. With `MAX_RELAY_PEERS = 128` (line 59) and a quota of 30/s per peer, the global ceiling is 3,840 `RelayTransactionHashes` messages/second, each triggering a full O(50,000) LruCache iteration under the mutex. The `lru` crate's `LruCache` uses a doubly-linked list internally, making iteration pointer-chasing across ~1.6 MB of `Byte32` data — cache-unfriendly and slow.

Existing guards are insufficient: the `MAX_RELAY_TXS_NUM_PER_BATCH` check (line 29–35) only limits hash count per message, not the frequency of `remove_expired()` invocations. The per-peer rate limiter does not bound the aggregate scan rate.

## Impact Explanation
At 3,840 forced scans/second, if each O(50,000) LruCache iteration takes 100–500 µs (realistic for pointer-chasing through a linked list), the mutex is held for 384–1,920 ms per second of wall time. This serializes all concurrent relay message processing threads on the shared mutex, causing increased latency for transaction propagation and degraded relay throughput across the node. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the attacker cost is simply connecting as a peer and sending messages at the allowed rate.

## Likelihood Explanation
Any unprivileged peer can send `RelayTransactionHashes` messages up to the per-peer rate limit (30/s) with no PoW, no key, and no special role. The `tx_filter` fills to capacity through normal network operation (legitimate tx hash announcements over 4-hour TTL windows), so the precondition of a near-full filter is met organically. The attack is trivially reproducible with a modified CKB client or a raw P2P message sender. Multiple attacker-controlled peers compound the effect linearly.

## Recommendation
1. **Decouple expiry from message handling**: Move `remove_expired()` to a periodic background timer (e.g., the existing `TX_HASHES_TOKEN` notify at 300 ms intervals in `sync/src/relayer/mod.rs`) rather than calling it on every incoming message.
2. **Add a cooldown guard**: Track the last expiry timestamp inside `TtlFilter` and skip `remove_expired()` if called within a minimum interval (e.g., 60 seconds).
3. **Use a time-ordered structure**: Replace the full-scan approach with a `BTreeMap<expiry_time, key>` side-index so expiry is O(expired_count) not O(FILTER_SIZE).
4. **Add a global rate limit** on `remove_expired()` invocations per second, independent of per-peer limits.

## Proof of Concept
```rust
// Fill tx_filter to capacity (50,000 entries)
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
// Repeat 30x/second from a single peer to observe mutex contention on relay threads.
```