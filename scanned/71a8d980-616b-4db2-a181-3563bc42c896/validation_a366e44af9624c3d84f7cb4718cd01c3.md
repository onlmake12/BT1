Audit Report

## Title
Tx-Pool Re-verifies All Persisted Transactions on Every Startup Without Caching Verification Results - (File: `tx-pool/src/service.rs`)

## Summary
On every node restart, `TxPoolServiceBuilder::start` loads raw transactions from the persisted file and re-submits each one through the full verification pipeline via `load_persisted_data` → `submit_local_tx` → `process_tx` → `_process_tx` → `verify_rtx`. The in-memory `TxVerificationCache` is not persisted to disk and starts empty on each boot, so every persisted transaction unconditionally triggers full CKB-VM script execution. An attacker with RPC access who fills the pool with cycle-heavy transactions can cause significant CPU saturation and delayed block template availability on every subsequent node restart.

## Finding Description
`TxPoolServiceBuilder::start` loads raw transactions from disk and immediately calls `load_persisted_data` with them:

- `tx_pool.load_from_file()` deserializes only raw `TransactionVec` bytes — no cycle counts, no verification results (`persisted.rs` L57–90).
- `load_persisted_data` (L433–453) iterates every transaction and calls `self.submit_local_tx(tx)` for each.
- `submit_local_tx` sends a `Message::SubmitLocalTx` which is handled by `service.process_tx(tx, None)` (L809), which calls `_process_tx`.
- Inside `_process_tx` (L719), `fetch_tx_verify_cache` is called. The `TxVerificationCache` is a pure in-memory LRU (`lru::LruCache<Byte32, CacheEntry>`, `CACHE_SIZE = 30_000`) that is never written to disk. On restart it is always empty, so `verify_cache` is always `None` for all persisted transactions.
- `verify_rtx` (util.rs L96–131): when `cache_entry` is `None`, it falls through to `ContextualTransactionVerifier::verify(max_tx_verify_cycles, false)` — full CKB-VM script execution — for every transaction.

There is no guard, cap, or fast-path that skips re-execution for previously verified transactions on startup.

## Impact Explanation
This matches **Low (501–2000 points): Any other important performance improvements for CKB**. With `max_tx_pool_size = 180_000_000` bytes (180 MB) and `max_tx_verify_cycles = 70_000_000` cycles per transaction, an attacker can fill the pool with many small but cycle-heavy transactions. On every restart, the node must re-execute all scripts before the tx-pool is fully operational, saturating CPU and delaying block template generation. The node does not crash and eventually recovers, so this does not meet the threshold for High or Critical impacts (no crash, no consensus deviation, no network-wide congestion from a single node).

## Likelihood Explanation
The attack requires calling `send_transaction` RPC. By default the RPC is bound to `127.0.0.1:8114` (localhost only), so the attacker needs local access or an operator who has exposed the RPC externally. The attacker must also pay `min_fee_rate` (default 1000 shannons/KB) for all submitted transactions. Node restarts are routine (upgrades, maintenance, crash recovery), and the persisted pool state survives graceful shutdowns, so the cost is paid once but the re-verification penalty is incurred on every subsequent restart.

## Recommendation
**Short term**: Extend the persisted data format to include verified cycle counts alongside raw transaction bytes. On reload, when a cached cycle count is present and the transaction's inputs are still live in the current chain snapshot, skip full script re-execution and use `TimeRelativeTransactionVerifier` only (the same lightweight path already taken on in-memory cache hits in `verify_rtx` L96–100).

**Long term**: Add a startup benchmark test measuring tx-pool reload time with a pool filled to capacity with max-cycle transactions and enforce a time bound. Consider a configurable `max_pool_reload_cycles` cap to bound worst-case startup cost independently of pool size.

## Proof of Concept
1. Expose or access the CKB node's RPC (default `127.0.0.1:8114`).
2. Submit N transactions via `send_transaction`, each with a lock script that consumes close to `max_tx_verify_cycles` (70M) cycles. Keep total byte size under `max_tx_pool_size` (180 MB).
3. Trigger a graceful shutdown (`ckb stop`). `save_into_file` persists only raw transaction bytes to disk.
4. Restart the node. Observe `TxPoolServiceBuilder::start` → `load_persisted_data` → `submit_local_tx` × N → `_process_tx` → `verify_rtx` with `cache_entry = None` → full `ContextualTransactionVerifier` for every transaction.
5. Measure time until `tx_pool_ready` returns `true` and block templates include all pool transactions. Delay scales linearly with N × cycles-per-tx. With 180 MB of ~500-byte transactions at 70M cycles each, this represents a substantial re-verification workload on every restart.