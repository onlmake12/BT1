I have verified all claims against the actual code. Here is my finding:

Audit Report

## Title
VerifyQueue Lacks Time-Based Expiry, Enabling Persistent Resource Exhaustion via P2P Relay — (`tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue` records an `added_time` field on every entry but has no TTL enforcement or `remove_expired` method. The sole growth guard is a 256 MB byte-size cap. Once that cap is reached via P2P-relayed transactions, all subsequent relay submissions are rejected with `Reject::Full`, and no per-block sweep ever clears stale entries from the queue, allowing the condition to persist indefinitely.

## Finding Description
`VerifyEntry.added_time` (line 43 of `verify_queue.rs`) is used only for FIFO ordering via `iter_by_added_time`; it is never compared against a TTL. `VerifyQueue` exposes no `remove_expired` method. Its only cleanup paths are `remove_tx` / `remove_txs` (explicit), `remove_txs_by_peer` (peer disconnect), `pop_front` (worker processing), and `clear` (full wipe).

The async `update_tx_pool_for_reorg` (lines 802–858 of `process.rs`) calls `queue.remove_txs(attached.iter()...)` (lines 855–857) to drop committed transactions from the verify queue, but performs no time-based sweep. The inner `_update_tx_pool_for_reorg` (lines 1039–1114) calls `tx_pool.remove_expired(callbacks)` at line 1110, which operates only on `pool_map` entries via `TxPool::remove_expired` (lines 271–288 of `pool.rs`), comparing `entry.inner.timestamp + self.expiry` against `now_ms`. The verify queue is entirely outside this sweep.

By contrast, `OrphanPool::add_orphan_tx` (line 158 of `orphan.rs`) calls `self.limit_size()` on every insertion, which evicts entries whose `expires_at` has passed before checking the count cap.

Exploit path:
1. A P2P peer calls `submit_remote_tx` → `resumeble_process_tx` → `non_contextual_verify` (format-only check) → `enqueue_verify_queue` → `queue.add_tx`.
2. The attacker submits many syntactically valid transactions with high declared cycle counts. `add_tx` sets `is_large_cycle = true` (line 212–214), causing the `OnlySmallCycleTx` worker to skip them, leaving only `SubmitTimeFirst` workers to drain them.
3. `total_tx_size` grows toward `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000` (line 18).
4. Once the cap is hit, `is_full` (lines 104–106) returns true and all subsequent `add_tx` calls return `Reject::Full`.
5. On each new block, `_update_tx_pool_for_reorg` calls `tx_pool.remove_expired(callbacks)` (line 1110) on `pool_map` only. The verify queue is not swept. Entries that have been waiting past `expiry_hours` remain.
6. Legitimate relay peers cannot enqueue transactions until the attacker's transactions are individually popped by workers.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The verify queue is the sole ingress path for P2P-relayed transactions (`submit_remote_tx` → `enqueue_verify_queue`). Filling it blocks all relay-based transaction propagation on the targeted node at zero on-chain cost: the attacker's transactions never confirm and pay no fees. The node continues consensus participation but is effectively isolated from the transaction relay network.

## Likelihood Explanation
The attack is reachable by any unprivileged P2P peer. No key material, special privilege, or majority hashpower is required. Transactions need only pass `non_contextual_verify` (format checks). The attacker is not banned: `ban_malformed` (line 702) is triggered only for `Reject::Malformed`, not for `Reject::DeclaredWrongCycles` or script-failure rejects. The attack is repeatable: once the queue drains naturally, it can be refilled immediately.

## Recommendation
1. `VerifyEntry.added_time` already exists (line 43 of `verify_queue.rs`) — no new field is needed.
2. Implement `VerifyQueue::remove_expired(expiry_ms: u64)` that iterates `inner.iter_by_added_time()` and removes entries where `added_time + expiry_ms < unix_time_as_millis()`.
3. Call this method from `update_tx_pool_for_reorg` in `process.rs` (after line 856) alongside the existing `queue.remove_txs(...)` call, using `self.tx_pool_config.expiry_hours as u64 * 3_600_000` as the expiry duration, mirroring the `TxPool::expiry` field (line 57 of `pool.rs`).

## Proof of Concept
1. Connect to a CKB node as a P2P peer.
2. Repeatedly relay syntactically valid transactions (passing `non_contextual_verify`) with maximum serialized size and high declared cycle counts.
3. Observe `verify_queue.total_tx_size` growing toward 256 MB.
4. Once the cap is hit, all subsequent relay attempts return `Reject::Full("verify_queue total_tx_size exceeded")`.
5. Mine or wait for blocks: `_update_tx_pool_for_reorg` calls `tx_pool.remove_expired(callbacks)` (line 1110 of `process.rs`) but performs no expiry sweep on `verify_queue` — the queue remains full.
6. Confirm that legitimate relay peers cannot submit transactions until the attacker's transactions are individually processed by workers.