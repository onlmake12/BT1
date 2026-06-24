All code references check out exactly. The behavior is confirmed:

Audit Report

## Title
Orphan Transaction Pool Flooding Allows Attacker to Evict Legitimate Orphan Transactions - (File: tx-pool/src/component/orphan.rs)

## Summary
The `OrphanPool` enforces a global hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer contribution limit. Any relay peer can flood the pool with 100 structurally valid but semantically invalid transactions (referencing non-existent inputs), saturating the pool and causing legitimate in-flight child transactions to be evicted via non-deterministic HashMap iteration. Evicted transactions are silently removed from the node's known-tx filter with no re-request and no notification to the original sender, permanently breaking transaction chains on that node.

## Finding Description

**Root cause — no per-peer accounting in `add_orphan_tx`:**

`tx-pool/src/component/orphan.rs` L134–158: `add_orphan_tx` inserts any transaction that passes the duplicate check directly into `self.entries` without inspecting how many entries the submitting peer already has. After insertion it calls `limit_size()`.

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

`sync/src/relayer/transactions_process.rs` L37–96: The relay handler accepts a transaction from peer P if (a) it is not already in the tx filter and (b) it was previously requested from P via `unknown_tx_hashes`. The attacker satisfies both conditions by first advertising fake hashes (`RelayTransactionHashes`), waiting for the node to issue `GetRelayTransactions`, then responding with 100 transactions each spending a non-existent `OutPoint`.

**Missing-input path — confirmed in `process.rs`:**

`tx-pool/src/process.rs` L507–512: When `_process_tx` returns `is_missing_input`, the transaction is unconditionally forwarded to `add_orphan`. No per-peer quota is checked here either.

`tx-pool/src/process.rs` L557–572: `add_orphan` collects the evicted hashes returned by `add_orphan_tx` and sends each as `TxVerificationResult::Reject`.

**Silent drop — confirmed in `sync/src/relayer/mod.rs`:**

`sync/src/relayer/mod.rs` L673–674: The `Reject` arm calls only `remove_from_known_txs`. The node removes the evicted tx from its bloom filter but issues no re-request and sends no signal to the original sender.

**Existing guard is insufficient:**

`util/constant/src/sync.rs` L72: `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` (equal to `MAX_RELAY_TXS_NUM_PER_BATCH`). This limit governs the `unknown_tx_hashes` queue used to track pending parent requests, not the orphan pool itself. It is 327× larger than the 100-entry orphan pool cap and therefore provides zero protection against orphan pool saturation.

**Full exploit flow:**
1. Attacker connects as a standard relay peer.
2. Sends `RelayTransactionHashes` with 100 fake hashes → node adds them to `unknown_tx_hashes` and issues `GetRelayTransactions`.
3. Attacker responds with 100 transactions each spending a random non-existent `OutPoint`.
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
2. **Peer-weighted eviction**: In `limit_size()`, identify the peer with the most entries and evict one of its entries first, rather than using arbitrary HashMap iteration order.
3. **Increase or make the cap configurable**: The hard cap of 100 is very small for a network with many concurrent in-flight transaction chains; raising it reduces the blast radius of a single-peer flood.

## Proof of Concept

```
1. Attacker connects to victim node as relay peer P.

2. Attacker sends RelayTransactionHashes { tx_hashes: [h1, ..., h100] }.
   Victim adds h1..h100 to unknown_tx_hashes and sends GetRelayTransactions back to P.

3. Attacker responds with RelayTransactions containing 100 transactions T1..T100,
   each with a single input spending OutPoint { tx_hash: random_bytes(32), index: 0 }.

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