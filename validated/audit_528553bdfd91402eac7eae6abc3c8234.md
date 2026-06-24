Audit Report

## Title
Orphan Pool Permanently Saturated via Silent Drop of Exhausted Parent-Request Entries — (`sync/src/types/mod.rs`, `tx-pool/src/component/orphan.rs`)

## Summary
When `pop_ask_for_txs` processes an `unknown_tx_hashes` entry whose only announcing peer has already been asked once, `next_request_peer()` returns `None` and the entry is silently discarded with no re-queue and no corresponding orphan eviction. An unprivileged relay peer can exploit this to permanently saturate the 100-slot orphan pool for up to `100 * MAX_BLOCK_INTERVAL` seconds per wave, causing random eviction of legitimate orphan transactions and disrupting dependent transaction chains for honest users.

## Finding Description

**Root cause — silent drop in `pop_ask_for_txs`:**

In `sync/src/types/mod.rs`, `pop_ask_for_txs` pops entries from `unknown_tx_hashes` and calls `next_request_peer()`. When `next_request_peer()` returns `Some(peer)`, the entry is re-pushed back into the queue. When it returns `None`, the entry is simply not re-pushed — it is permanently discarded: [1](#0-0) 

`next_request_peer()` returns `None` exactly when `requested == true` and `peers.len() <= 1` — i.e., the transaction was announced by exactly one peer and that peer has already been asked once: [2](#0-1) 

`RETRY_ASK_TX_TIMEOUT_INCREASE` is 30 seconds, so the entry is dropped approximately 60 seconds after admission (first request at t=0, retry window expires at t=30s, second tick drops it). [3](#0-2) 

**Orphan admission path:**

When a relayed transaction fails with a missing-input error, it is added to `OrphanPool` and the announcing peer is queued in `unknown_tx_hashes` via `add_ask_for_txs`: [4](#0-3) [5](#0-4) 

**Orphan expiry and eviction:**

The orphan pool entry created above persists until `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`. When the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100), a random entry is evicted: [6](#0-5) [7](#0-6) 

**No per-peer limit on orphan pool slots:**

`OrphanPool` tracks entries only by `ProposalShortId` with no per-peer accounting, so a single peer can occupy all 100 slots: [8](#0-7) 

**Why existing checks are insufficient:**

- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` limits entries in `unknown_tx_hashes` per peer, but once those entries are dropped (after the single retry), the orphan pool slots they correspond to remain occupied with no recovery path.
- `limit_size()` only evicts expired entries or entries beyond the 100-slot cap — it does not evict orphans whose parent requests have been abandoned.
- There is no mechanism to re-queue a dropped `unknown_tx_hashes` entry when a new peer later announces the same parent hash, because the orphan is already in the pool and the parent hash is not re-added to `unknown_tx_hashes` from the orphan side.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single attacker peer can saturate the 100-slot orphan pool of any reachable CKB node for up to `100 * MAX_BLOCK_INTERVAL` seconds per wave. During this window, every legitimate orphan transaction submitted to that node is subject to random eviction, breaking dependent transaction chains for honest users. The attack is repeatable and can be sustained continuously by sending new orphan transactions as old ones expire, keeping the pool permanently at capacity. Applied to multiple nodes simultaneously, this degrades network-wide transaction relay with negligible cost to the attacker.

## Likelihood Explanation

- Any peer that opens the `RelayV3` protocol can send `RelayTransactions` messages — no special role required.
- Constructing orphan transactions requires only referencing non-existent `OutPoint`s; the node cannot verify the lock script before the missing-input error fires, so no valid signatures are needed.
- Only 100 such transactions are needed to saturate the pool.
- The attacker's only required action is to ignore `GetRelayTransactions` responses.
- The attack is fully repeatable with no cooldown beyond the 80-minute expiry window, which can itself be bypassed by continuously injecting new orphan transactions.

## Recommendation

1. **Re-queue on exhausted peers**: In `pop_ask_for_txs`, when `next_request_peer()` returns `None`, re-insert the entry into `unknown_tx_hashes` with a significantly longer timeout (e.g., 5 minutes) rather than silently dropping it, so that a future peer announcement can still resolve it.
2. **Evict orphan on request abandonment**: When a `unknown_tx_hashes` entry is permanently dropped (all peers exhausted), proactively remove the corresponding orphan transaction(s) from `OrphanPool` rather than letting them occupy slots until expiry.
3. **Per-peer orphan slot accounting**: Limit the number of orphan pool slots attributable to a single peer to prevent one peer from monopolizing the 100-slot pool.

## Proof of Concept

```
1. Attacker connects to victim node, opens RelayV3 protocol.
2. Attacker generates 100 transactions each spending a distinct non-existent OutPoint
   (no valid signatures required — missing-input fires before lock script verification).
3. Attacker sends RelayTransactions for all 100 to the victim.
4. Victim: adds all 100 to OrphanPool (pool now 100/100);
   calls add_ask_for_txs(attacker_peer, parent_hashes) for each.
5. At t≈0s: pop_ask_for_txs fires; next_request_peer() returns attacker_peer
   (requested=false → sets requested=true); entries re-queued.
6. Attacker ignores all GetRelayTransactions messages.
7. At t≈30s: pop_ask_for_txs fires again; next_request_peer() returns None
   (requested=true, peers.len()==1); all 100 entries are silently dropped.
8. OrphanPool remains at 100/100 for ~80 minutes with no recovery path.
9. Any legitimate orphan tx submitted during this window triggers random eviction
   (limit_size() evicts a random entry), breaking dependent tx chains.
10. Attacker can sustain the attack indefinitely by injecting new orphan txs
    as old ones expire, keeping the pool permanently saturated.
```

### Citations

**File:** sync/src/types/mod.rs (L1276-1289)
```rust
    pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
        if self.requested {
            if self.peers.len() > 1 {
                self.request_time = Instant::now();
                self.peers.swap_remove(0);
                self.peers.first().cloned()
            } else {
                None
            }
        } else {
            self.requested = true;
            self.peers.first().cloned()
        }
    }
```

**File:** sync/src/types/mod.rs (L1466-1474)
```rust
        while let Some((tx_hash, mut priority)) = unknown_tx_hashes.pop() {
            if priority.should_request(now) {
                if let Some(peer_index) = priority.next_request_peer() {
                    result
                        .entry(peer_index)
                        .and_modify(|hashes| hashes.push(tx_hash.clone()))
                        .or_insert_with(|| vec![tx_hash.clone()]);
                    unknown_tx_hashes.push(tx_hash, priority);
                }
```

**File:** util/constant/src/sync.rs (L57-57)
```rust
pub const RETRY_ASK_TX_TIMEOUT_INCREASE: Duration = Duration::from_secs(30);
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** sync/src/relayer/mod.rs (L676-685)
```rust
                    TxVerificationResult::UnknownParents { peer, parents } => {
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

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L42-45)
```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```
