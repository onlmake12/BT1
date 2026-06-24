Audit Report

## Title
VerifyQueue Lacks Per-Peer Admission Limit, Enabling Large-Cycle Transaction Flood to DOS Tx-Pool Admission — (`tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue` enforces only a global 256 MB byte-size cap with no per-peer accounting. Any unprivileged P2P peer can flood the queue with structurally valid large-cycle transactions, saturating it and causing all subsequent calls to `submit_remote_tx`, `notify_tx`, and `process_orphan_tx` to return `Reject::Full`, blocking remote relay and local RPC `send_transaction` for the duration of the attack.

## Finding Description
`VerifyQueue` tracks a single global counter `total_tx_size` against `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` (L17-18). `add_tx` checks `is_full` before inserting and returns `Reject::Full` with no per-peer quota (L103-106, L215-220). The `is_large_cycle` flag is set from the **peer-declared** cycle count, not verified cycles (L212-214). Worker 0 is assigned `OnlySmallCycleTx` and skips all large-cycle entries (verify_mgr.rs L185-189), meaning large-cycle transactions drain only through `SubmitTimeFirst` workers.

Critically, transactions enter the queue **before** any UTXO/contextual verification. `resumeble_process_tx` calls only `non_contextual_verify` (structural check) before calling `enqueue_verify_queue` (process.rs L335-353). UTXO resolution occurs inside the worker via `pre_check` → `resolve_tx` after dequeuing. This means an attacker needs only structurally valid transactions — no valid UTXOs required.

All three admission paths funnel through `enqueue_verify_queue` → `add_tx`: `submit_remote_tx` (P2P relay, L371-379), `notify_tx` (local/RPC, L381-384), and `process_orphan_tx` large-cycle path (L604-624). `remove_txs_by_peer` exists (verify_queue.rs L159-168) but is only triggered from `ban_malformed`, which requires a malformed transaction — structurally valid transactions with invalid UTXOs never trigger a ban.

## Impact Explanation
When the queue is saturated, remote transaction relay is blocked (node stops participating in mempool propagation), local RPC `send_transaction` fails with `PoolIsFull`, and large-cycle orphan promotions are stranded. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer via the standard `RelayV3` protocol. With `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes (tx_pool.rs L309), filling the 256 MB queue requires approximately 512 transactions. A single peer can consume the entire queue budget. As workers drain invalid transactions (failing fast at UTXO resolution), the attacker continuously submits replacements, maintaining saturation indefinitely at very low cost. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation
1. **Add per-peer byte quota in `add_tx`**: Track `total_tx_size_by_peer: HashMap<PeerIndex, usize>` in `VerifyQueue` and reject submissions from a peer exceeding `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / MAX_PEERS`.
2. **Evict large-cycle transactions from slow peers** when the queue approaches capacity, extending the existing `remove_txs_by_peer` mechanism to trigger on queue pressure, not only on ban.
3. **Apply a relay-layer rate limit per peer** before transactions reach `VerifyQueue`, analogous to rate limiters used in other P2P protocols.

## Proof of Concept
```
1. Attacker connects to target node as a P2P peer via RelayV3.
2. Attacker constructs ~512 structurally valid transactions (no valid UTXOs
   required), each ~512 KB serialized, declaring cycles = max_tx_verify_cycles + 1.
3. Attacker relays all 512 transactions via RelayTransactions messages.
4. Each transaction passes non_contextual_verify (structural only), enters
   VerifyQueue::add_tx(), is classified is_large_cycle = true.
   The OnlySmallCycleTx worker skips them; SubmitTimeFirst workers verify slowly.
5. total_tx_size approaches 256_000_000.
6. Any subsequent submit_remote_tx(), notify_tx(), or process_orphan_tx()
   (large-cycle path) returns Reject::Full.
7. Legitimate RPC send_transaction calls fail; orphan promotions are blocked.
8. Attacker continuously submits replacements as old ones drain, sustaining
   the saturated state indefinitely.
```

Reproducible via the existing test harness in `tx-pool/src/component/tests/chunk.rs` by extending `submit_remote_tx_notifies_relayer_when_verify_queue_is_full` (L376-402) to simulate concurrent peer submissions filling the queue from a single peer index, confirming `Reject::Full` is returned for all subsequent legitimate submissions.