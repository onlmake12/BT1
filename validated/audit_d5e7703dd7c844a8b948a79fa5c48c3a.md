Audit Report

## Title
O(n) Full Linear Scan of `unknown_tx_hashes` Under Global Mutex on Every Inbound `RelayTransactionHashes` Message When Queue Is Saturated — (`sync/src/types/mod.rs`)

## Summary

`add_ask_for_txs` in `sync/src/types/mod.rs` inserts up to 32,767 hashes into `unknown_tx_hashes` before checking the queue size limit. Once the queue exceeds `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000), every subsequent call performs an O(n) full linear scan of the entire queue while holding the `unknown_tx_hashes` mutex. Because the non-offending path returns `Status::ignored()` without evicting the just-inserted hashes, the queue grows monotonically, compounding the scan cost with each new call. This blocks `pop_ask_for_txs` — which holds the same mutex to dispatch `GetTransactions` requests — stalling the entire relay transaction-fetching pipeline for all peers.

## Finding Description

**Entry point:** `TransactionHashesProcess::execute` (`sync/src/relayer/transaction_hashes_process.rs`, line 49) calls `state.add_ask_for_txs(self.peer, tx_hashes)` after filtering already-known hashes.

**Mutex acquisition and held for entire body:** `add_ask_for_txs` acquires `self.unknown_tx_hashes.lock()` at line 1484 and does not release it until the function returns.

**Insertion before guard:** Lines 1486–1504 unconditionally insert up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32,767) hashes into the queue before any size check.

**Post-insertion size check:** Lines 1506–1509 check `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` (50,000) only after insertion has already occurred.

**O(n) scan under held mutex:** When the threshold is exceeded, lines 1516–1523 iterate over every `(_hash, priority)` pair in the queue and walk each `priority.peers` vector to count entries belonging to `peer_index`. The cost is O(total queue depth × average peers per entry).

**No eviction on non-offending path:** Line 1528 returns `Status::ignored()` without removing any entries. The just-inserted hashes remain, so the queue grows monotonically after saturation.

**`pop_ask_for_txs` blocked:** `pop_ask_for_txs` (line 1454) acquires the same `self.unknown_tx_hashes.lock()`. While `add_ask_for_txs` holds the mutex for the O(n) scan, `pop_ask_for_txs` is fully blocked, stalling all outbound `GetTransactions` dispatches.

**Attack path:**
1. Peer A sends 32,767 unique hashes → queue = 32,767 (below threshold, no scan, `Status::ok()`).
2. Peer B sends 32,767 unique hashes → queue = 65,534 (≥ 50,000); O(65,534) scan fires; peer B has 32,767 entries ≥ `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, so it is disconnected, but its hashes remain.
3. Peer C sends any hashes → queue still ≥ 50,000; O(65,534+) scan fires; returns `Status::ignored()`; hashes stay.
4. Repeat step 3 indefinitely with new or reconnected peers; each call adds more hashes and triggers a larger scan.

**Existing guards are insufficient:** The per-batch cap (`MAX_RELAY_TXS_NUM_PER_BATCH = 32,767`) at `transaction_hashes_process.rs` line 29 only limits a single message's hash count, not the cumulative queue depth. The per-peer disconnect at line 1524–1525 only fires for the offending peer and does not evict its hashes.

## Impact Explanation

Stalling `pop_ask_for_txs` under the mutex prevents the node from dispatching `GetTransactions` requests to any peer for the duration of the scan. As the queue grows unboundedly, each subsequent call takes longer, progressively degrading and eventually halting transaction relay for the affected node. Because the attack is cheap and repeatable against any reachable CKB node, it constitutes a practical denial-of-service against the transaction relay pipeline, matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10,001–15,000 points)**.

## Likelihood Explanation

The attack requires only two cooperating unprivileged P2P peers to saturate the queue past the threshold. No proof-of-work, no privileged role, no leaked key, and no victim mistake is required. After initial saturation, a single additional peer suffices to trigger the O(n) scan on every subsequent call. The attack is fully repeatable and the cost to the attacker is negligible (sending P2P messages with unique hash values).

## Recommendation

1. **Check before inserting:** Move the global-size and per-peer guard to the top of `add_ask_for_txs`, before the insertion loop, so hashes from an over-quota peer are never added to the queue.
2. **Eliminate the O(n) scan:** Maintain a `HashMap<PeerIndex, usize>` side-structure updated incrementally during insertion and removal, so the per-peer count check is O(1) rather than O(queue depth).
3. **Evict on overflow:** When the global limit is reached, evict the lowest-priority entries (e.g., oldest `request_time`) rather than silently accepting new hashes and returning `ignored`.

## Proof of Concept

```rust
// Minimal unit test sketch
let state = SyncState::new(...);

// Step 1: saturate queue past MAX_UNKNOWN_TX_HASHES_SIZE
let hashes_a: Vec<Byte32> = (0..32767).map(make_hash).collect();
let hashes_b: Vec<Byte32> = (32767..65534).map(make_hash).collect();
state.add_ask_for_txs(peer_a, hashes_a); // queue=32767, no scan
state.add_ask_for_txs(peer_b, hashes_b); // queue=65534, O(65534) scan, peer_b disconnected, hashes remain

// Step 2: measure latency of a subsequent call from peer_c
let hashes_c: Vec<Byte32> = (65534..65566).map(make_hash).collect();
let t0 = Instant::now();
state.add_ask_for_txs(peer_c, hashes_c); // triggers O(65534+) scan
let elapsed = t0.elapsed();

// Step 3: verify pop_ask_for_txs was blocked for the same duration
// (spawn a thread calling pop_ask_for_txs concurrently and measure its wait time)

assert!(elapsed < Duration::from_micros(100),
    "O(n) scan detected: {:?}", elapsed);
```

The test can be extended to a fuzz harness that drives queue depth to arbitrary sizes and measures `pop_ask_for_txs` latency as a function of queue depth, confirming linear growth.