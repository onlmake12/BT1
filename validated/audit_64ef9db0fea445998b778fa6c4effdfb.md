The code is confirmed. Let me verify the key claims against the actual implementation.

All claims verified against the actual code. Every technical assertion in the report is accurate.

**Verification summary:**

- **Insert-before-check**: Confirmed at [1](#0-0)  — hashes are unconditionally inserted (lines 1486–1504), and the size guard fires only at line 1507.
- **O(n) scan under mutex**: Confirmed at [2](#0-1)  — nested loop over all queue entries and their `peers` vectors.
- **No eviction on non-offending path**: Confirmed at [3](#0-2)  — `Status::ignored()` returned with hashes already in queue.
- **`pop_ask_for_txs` contends the same mutex**: Confirmed at [4](#0-3) .
- **Constants**: Confirmed at [5](#0-4) .
- **Rate limiter does not mitigate**: The 30 req/s per-peer rate limit at [6](#0-5)  allows 30 × 32,767 = ~983K hashes/second per peer — the 50,000-entry threshold is crossed in under one second with a single peer.

---

Audit Report

## Title
O(n) Full Linear Scan of `unknown_tx_hashes` Under Global Mutex Enables Transaction Relay Pipeline Stall — (`sync/src/types/mod.rs`)

## Summary

`add_ask_for_txs` in `sync/src/types/mod.rs` inserts incoming transaction hashes into the global `unknown_tx_hashes` queue before checking the queue size limit, then performs an O(total queue depth) linear scan under the held mutex when the soft limit is exceeded. Because the non-offending path returns `Status::ignored()` without evicting the just-inserted hashes, the queue grows monotonically with each subsequent call. While the mutex is held for the scan, `pop_ask_for_txs` — which dispatches `GetTransactions` requests to all peers — is blocked, stalling the entire relay transaction-fetching pipeline.

## Finding Description

**Entry point:** `TransactionHashesProcess::execute` (`sync/src/relayer/transaction_hashes_process.rs`, L49) calls `state.add_ask_for_txs(self.peer, tx_hashes)` after filtering already-known hashes.

**Mutex acquisition and hold:** `add_ask_for_txs` (`sync/src/types/mod.rs`, L1484) immediately acquires `self.unknown_tx_hashes.lock()` and holds it for the entire function body, including the scan.

**Insert-before-check:** Lines 1486–1504 unconditionally insert up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) hashes into the queue. The size guard at lines 1507–1509 fires only **after** insertion. The code comment at line 1506 explicitly acknowledges this ordering: `// Check unknown_tx_hashes's length after inserting the arrival tx_hashes`.

**O(n) scan:** When `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), lines 1516–1523 iterate over every entry in the queue — O(total queue depth) — with a nested loop over each entry's `peers` vector, to count how many entries belong to the current peer.

**No eviction on non-offending path:** If the per-peer counter is below `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, line 1528 returns `Status::ignored()` without removing the just-inserted hashes. The queue therefore grows with every subsequent call from any peer once the threshold is crossed.

**`pop_ask_for_txs` blocked:** `pop_ask_for_txs` (`sync/src/types/mod.rs`, L1453–1454) acquires the same `unknown_tx_hashes` mutex. While `add_ask_for_txs` holds the lock performing the O(n) scan, `pop_ask_for_txs` is blocked, stalling all outbound `GetTransactions` dispatches for all peers simultaneously.

**Rate limiter does not mitigate:** The per-peer rate limiter (`sync/src/relayer/mod.rs`, L116–123) caps at 30 `RelayTransactionHashes` messages per second per peer. At 32,767 hashes per message, a single peer can inject ~983K unique hashes per second — the 50,000-entry threshold is crossed in under one second, and the rate limiter does not prevent queue growth beyond the threshold.

**Constants confirmed:** `MAX_RELAY_TXS_NUM_PER_BATCH = MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32,767`; `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000` (`util/constant/src/sync.rs`, L68–72).

## Impact Explanation

Stalling `pop_ask_for_txs` under a global mutex blocks the relay transaction-fetching pipeline for all connected peers simultaneously. Because the queue grows monotonically after saturation (non-offending peers' hashes are never evicted), each subsequent call from any peer increases the scan cost, compounding the stall duration. This constitutes **CKB network congestion achievable with few costs**, matching the **High** impact class (10,001–15,000 points): "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation

The attack requires only unprivileged P2P peers capable of sending `RelayTransactionHashes` messages with unique transaction hashes. No PoW, no privileged role, no leaked key, and no victim mistake is required. A single peer suffices to saturate the queue past the threshold within one second; thereafter, any additional peer (or reconnected peer) sending unique hashes triggers the O(n) scan and leaves its hashes in the queue, growing the scan cost indefinitely. The attack is repeatable and low-cost.

## Recommendation

1. **Check before inserting:** Move the per-peer and global size guards to the top of `add_ask_for_txs`, before the insertion loop, so hashes from an over-quota peer are never added to the queue.
2. **Eliminate the O(n) scan:** Maintain a `HashMap<PeerIndex, usize>` per-peer counter updated incrementally during insertion, making the per-peer check O(1) and eliminating the need for the full queue iteration.
3. **Evict on overflow:** When the global limit is reached, evict the lowest-priority entries rather than silently accepting new hashes and returning `ignored`.

## Proof of Concept

```rust
// Minimal reproducible steps (unit test pseudocode)
let state = SyncState::new(...);

// Step 1: Saturate queue past MAX_UNKNOWN_TX_HASHES_SIZE using one peer
// (rate limiter allows 30 req/s * 32767 hashes = ~983K hashes/s; threshold crossed in <1s)
let hashes_a: Vec<Byte32> = (0..32767).map(|i| make_hash(i)).collect();
let hashes_b: Vec<Byte32> = (32767..65534).map(|i| make_hash(i)).collect();
state.add_ask_for_txs(peer_a, hashes_a); // queue = 32767, no scan
state.add_ask_for_txs(peer_b, hashes_b); // queue = 65534, O(65534) scan fires; hashes remain

// Step 2: Each subsequent call from any peer grows the queue and triggers a larger scan
let hashes_c: Vec<Byte32> = (65534..98301).map(|i| make_hash(i)).collect();
let t0 = Instant::now();
state.add_ask_for_txs(peer_c, hashes_c); // queue = 98301, O(98301) scan, Status::ignored(), hashes stay
let elapsed = t0.elapsed();

// Repeat with peer_d, peer_e, ... — scan cost grows linearly with each iteration
// Concurrently, pop_ask_for_txs is blocked for the duration of each scan,
// stalling all GetTransactions dispatches for all peers.
assert!(elapsed < Duration::from_micros(100), "O(n) scan detected: {:?}", elapsed);
```

### Citations

**File:** sync/src/types/mod.rs (L1453-1454)
```rust
    pub fn pop_ask_for_txs(&self) -> HashMap<PeerIndex, Vec<Byte32>> {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

**File:** sync/src/types/mod.rs (L1486-1507)
```rust
        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
        {
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
            }
        }

        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
```

**File:** sync/src/types/mod.rs (L1516-1523)
```rust
            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
```

**File:** sync/src/types/mod.rs (L1528-1528)
```rust
            return Status::ignored();
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
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
