Audit Report

## Title
Orphan Pool Random Eviction Enables DoS via Pool Saturation - (File: `tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` evicts transactions using `HashMap::keys().next()` — an arbitrary, non-fee-ordered selection — when the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. Any unprivileged P2P peer can fill the pool with 100 zero-fee orphan transactions and continuously re-submit evicted ones, causing legitimate orphan transactions to be permanently lost from the node's mempool with no recovery path.

## Finding Description
In `tx-pool/src/component/orphan.rs`, `limit_size()` (lines 119–125) evicts entries using:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

`self.entries` is a `HashMap<ProposalShortId, Entry>`, so `keys().next()` yields entries in an arbitrary, non-deterministic order — effectively random. The `Entry` struct stores a `cycle` field (line 25) but it is never consulted during eviction. There is no fee-rate ordering, no `EvictKey`, and no per-peer slot cap.

The full entry path from the network is confirmed:
1. A remote peer sends `RelayTransactions` → `TransactionsProcess::execute()` calls `tx_pool.submit_remote_tx(tx, declared_cycles, peer)` (`sync/src/relayer/transactions_process.rs`, lines 85–92).
2. `submit_remote_tx` calls `resumeble_process_tx` → `enqueue_verify_queue` → verification runs.
3. If verification fails with a missing input, `after_process()` calls `self.add_orphan(tx, peer, declared_cycle)` (`tx-pool/src/process.rs`, lines 507–512).
4. `add_orphan()` calls `orphan.add_orphan_tx()` which calls `limit_size()` with random eviction (`tx-pool/src/process.rs`, lines 557–573).
5. For each evicted transaction, `TxVerificationResult::Reject` is sent to the relayer, marking the tx as "unknown" in the relay filter — the node will not re-request it from peers.
6. When the parent later arrives and `process_orphan_tx()` is called (`tx-pool/src/process.rs`, lines 591–671), the evicted child is absent and is silently lost — never promoted to the pending pool.

Crucially, fee verification does not occur before orphan admission; it only happens after the parent is resolved. This means zero-fee orphans are admitted freely, and there is no cost barrier to saturating the pool.

## Impact Explanation
This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. With a single P2P connection and 100 structurally valid but parentless transactions (zero fees, no hashpower required), an attacker can permanently occupy the entire orphan pool of any targeted node. Applied across multiple nodes simultaneously — trivially achievable since each attack requires only one connection — this degrades the network's ability to relay transactions whose parents are in-flight, silently dropping valid transactions from mempools and preventing their inclusion in blocks.

## Likelihood Explanation
The attack requires no privileges, no fees, no hashpower, and no key material. Any network participant can open a P2P connection and send 100 crafted transactions with non-existent parent inputs. The orphan pool limit of 100 is small enough that saturation is achieved in a single batch. The attacker does not need to monitor internal eviction signals — periodic blind re-submission of all 100 spam transactions is sufficient to maintain saturation, since `add_orphan_tx` is a no-op for duplicates already in the pool (line 140–142 of `orphan.rs`). The attack is repeatable, stateless, and requires no coordination.

## Recommendation
1. **Fee-rate-ordered eviction**: Replace `self.entries.keys().next()` in `limit_size()` with an iteration that finds and evicts the entry with the lowest declared fee rate (derivable from `cycle` and transaction size already stored in `Entry`). This mirrors the `EvictKey`-based strategy in `PoolMap::next_evict_entry()` (`tx-pool/src/component/pool_map.rs`, lines 380–385).
2. **Per-peer slot cap**: Limit the number of orphan slots any single peer can occupy (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / 4 = 25`) to prevent monopolization regardless of declared fee rate.
3. **Minimum fee rate for orphan admission**: Reject orphan transactions below a configurable minimum declared fee rate before inserting into the pool.

## Proof of Concept
1. Attacker connects to a CKB node as a P2P peer via the relay protocol.
2. Attacker generates 100 transactions (`spam_0`…`spam_99`), each spending a randomly generated non-existent outpoint as input. These pass `non_contextual_verify` (structurally valid) but fail resolution with `is_missing_input`.
3. Attacker sends all 100 via `RelayTransactions` messages. The node admits them into the orphan pool (`len == 100`).
4. Victim sends a legitimate orphan transaction `victim_tx` (child of a parent currently propagating on the network).
5. Node calls `add_orphan_tx(victim_tx)` → `limit_size()` → randomly evicts one entry. With probability 100/101 ≈ 99%, the evicted entry is one of the attacker's spam transactions; with probability 1/101, `victim_tx` itself is immediately evicted.
6. Attacker periodically re-submits all 100 spam transactions. Any that were evicted are re-inserted; duplicates are silently ignored. Pool returns to 100 attacker-controlled entries.
7. On the next legitimate orphan insertion, `victim_tx` (if still present) faces another 1/101 eviction probability. Over repeated cycles, `victim_tx` is eventually evicted.
8. When `victim_tx`'s parent arrives and `process_orphan_tx()` is called, `victim_tx` is absent from the orphan pool and is permanently lost — never submitted to the pending pool.