All code references verified against the actual source. Every claim in the report checks out exactly:

- `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` confirmed. [1](#0-0) 
- `add_orphan_tx` has no per-peer quota check before inserting. [2](#0-1) 
- `limit_size()` evicts via `HashMap::keys().next()` — arbitrary, no fairness. [3](#0-2) 
- `transactions_process.rs` accepts a tx if not in filter AND was requested from that peer. [4](#0-3) 
- `is_missing_input` path unconditionally calls `add_orphan`. [5](#0-4) 
- `add_orphan` sends `TxVerificationResult::Reject` for each evicted hash. [6](#0-5) 
- `Reject` arm calls only `remove_from_known_txs`, no re-request. [7](#0-6) 
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767`, 327× the orphan cap. [8](#0-7) 

---

Audit Report

## Title
Orphan Transaction Pool Flooding Allows Attacker to Evict Legitimate Orphan Transactions - (File: tx-pool/src/component/orphan.rs)

## Summary
`OrphanPool` enforces a global hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer contribution limit. A single relay peer can saturate the pool with 100 structurally valid but semantically invalid transactions (spending non-existent inputs), causing legitimate in-flight child transactions to be evicted via non-deterministic `HashMap` iteration. Evicted transactions are silently removed from the node's known-tx filter with no re-request and no notification to the original sender, permanently breaking transaction chains on that node.

## Finding Description
**Root cause — no per-peer accounting in `add_orphan_tx`:**

`tx-pool/src/component/orphan.rs` L134–158: `add_orphan_tx` inserts any transaction that passes the duplicate check directly into `self.entries` without inspecting how many entries the submitting peer already holds. After insertion it calls `limit_size()`.

`tx-pool/src/component/orphan.rs` L96–132: `limit_size()` first expires timed-out entries, then evicts via:
```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```
`HashMap::keys().next()` yields an arbitrary key — there is no fairness guarantee and no preference for entries from the peer that caused the overflow.

**Attack entry path — confirmed in `transactions_process.rs`:**

`sync/src/relayer/transactions_process.rs` L37–96: The relay handler accepts a transaction from peer P if (a) it is not already in the tx filter and (b) it was previously requested from P via `unknown_tx_hashes`. The attacker satisfies both conditions by first advertising fake hashes (`RelayTransactionHashes`), waiting for the node to issue `GetRelayTransactions`, then responding with 100 transactions each spending a non-existent `OutPoint`. Since the attacker constructs the transactions, they can pre-compute hashes and advertise them exactly.

**Missing-input path — confirmed in `process.rs`:**

`tx-pool/src/process.rs` L507–512: When `_process_tx` returns `is_missing_input`, the transaction is unconditionally forwarded to `add_orphan`. No per-peer quota is checked here either.

`tx-pool/src/process.rs` L557–572: `add_orphan` collects the evicted hashes returned by `add_orphan_tx` and sends each as `TxVerificationResult::Reject`.

**Silent drop — confirmed in `sync/src/relayer/mod.rs`:**

`sync/src/relayer/mod.rs` L673–674: The `Reject` arm calls only `remove_from_known_txs`. The node removes the evicted tx from its bloom filter but issues no re-request and sends no signal to the original sender.

**Existing guard is insufficient:**

`util/constant/src/sync.rs` L68–72: `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` (equal to `MAX_RELAY_TXS_NUM_PER_BATCH`). This limit governs the `unknown_tx_hashes` queue used to track pending parent requests, not the orphan pool itself. It is 327× larger than the 100-entry orphan pool cap and therefore provides zero protection against orphan pool saturation.

**Full exploit flow:**
1. Attacker connects as a standard relay peer.
2. Sends `RelayTransactionHashes` with 100 fake hashes → node adds them to `unknown_tx_hashes` and issues `GetRelayTransactions`.
3. Attacker responds with 100 transactions each spending a random non-existent `OutPoint` (hashes match what was advertised).
4. Each transaction fails `_process_tx` with `is_missing_input` → `add_orphan_tx` is called 100 times → pool is at capacity.
5. A legitimate user's child transaction (parent in-flight) arrives → `add_orphan_tx` inserts it, `limit_size()` evicts an arbitrary entry (possibly the child itself).
6. Evicted hash → `TxVerificationResult::Reject` → `remove_from_known_txs`. Child is gone from the node's filter and orphan pool.
7. When the legitimate parent arrives, `process_orphan_tx` finds no children registered for its outputs. The child transaction is permanently lost on this node.
8. Attacker re-sends fake orphans every `ORPHAN_TX_EXPIRE_TIME` (`100 * MAX_BLOCK_INTERVAL`) seconds to maintain saturation.

## Impact Explanation
This matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single unprivileged peer can continuously disrupt transaction propagation on any targeted node at the cost of 100 small transactions per cycle. Scaled across multiple nodes simultaneously, legitimate transaction chains are broken network-wide without any error surfaced to users or wallets. The original sender has no eviction signal and no automatic retry path, so affected child transactions may never confirm on targeted nodes.

## Likelihood Explanation
The attack requires only a standard peer connection and the ability to construct structurally valid transactions with non-existent inputs — no proof-of-work, no privileged keys, no Sybil attack. The 100-entry cap means a single request-response round trip saturates the pool. The attacker can sustain the attack indefinitely by re-flooding before entries expire. Cost per cycle is negligible (100 small transactions, no fees required since they never enter the mempool).

## Recommendation
1. **Per-peer orphan accounting**: Track a per-peer entry count inside `OrphanPool`. In `add_orphan_tx`, reject insertion if the submitting peer already holds `DEFAULT_MAX_ORPHAN_TRANSACTIONS / expected_max_peers` entries.
2. **Peer-weighted eviction**: In `limit_size()`, identify the peer with the most entries and evict one of its entries first, rather than using arbitrary `HashMap` iteration order.
3. **Increase or make the cap configurable**: The hard cap of 100 is very small for a network with many concurrent in-flight transaction chains; raising it reduces the blast radius of a single-peer flood.

## Proof of Concept
```
1. Attacker connects to victim node as relay peer P.

2. Attacker sends RelayTransactionHashes { tx_hashes: [h1, ..., h100] }.
   Victim adds h1..h100 to unknown_tx_hashes and sends GetRelayTransactions back to P.

3. Attacker responds with RelayTransactions containing 100 transactions T1..T100,
   each with a single input spending OutPoint { tx_hash: random_bytes(32), index: 0 }.
   Hashes of T1..T100 match h1..h100 (attacker pre-computes them).

4. transactions_process.rs accepts each Ti (was requested from P, not in filter).
   Each Ti fails _process_tx with is_missing_input.
   add_orphan_tx inserts Ti; after 100 insertions OrphanPool.len() == 100.

5. Legitimate child_tx (parent in-flight) arrives from honest peer Q.
   add_orphan_tx inserts child_tx → len == 101 → limit_size() evicts
   entries.keys().next() (arbitrary, may be child_tx itself or any Ti).

6. Evicted hash → TxVerificationResult::Reject → remove_from_known_txs.
   child_tx is silently dropped from the node.

7. Legitimate parent_tx arrives → process_orphan_tx finds no children
   for parent_tx's outputs → child_tx is permanently lost on this node.

8. Attacker repeats step 2–3 every ORPHAN_TX_EXPIRE_TIME seconds to
   maintain pool saturation indefinitely.

Verification: add a unit test to OrphanPool that inserts 100 attacker
entries then one legitimate entry and asserts the legitimate entry is
present after limit_size(); it will fail non-deterministically,
confirming the eviction fairness bug.
```

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
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

**File:** tx-pool/src/component/orphan.rs (L134-158)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

**File:** sync/src/relayer/transactions_process.rs (L49-55)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
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

**File:** tx-pool/src/process.rs (L557-572)
```rust
    pub(crate) async fn add_orphan(
        &self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) {
        let evicted_txs = self
            .orphan
            .write()
            .await
            .add_orphan_tx(tx, peer, declared_cycle);
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
```

**File:** sync/src/relayer/mod.rs (L673-674)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
