All cited code references check out against the actual source. Let me verify the key claims:

1. **Service loop spawns one task per message** — confirmed at `service.rs:619-621`
2. **`pre_check` acquires and releases read lock before `verify_rtx`** — confirmed at `process.rs:276-314`; `with_tx_pool_read_lock` returns after the closure, dropping the lock before `verify_rtx` is called at line 724
3. **`verify_rtx` runs with no lock held** — confirmed at `process.rs:724-732`
4. **`submit_entry` is the first write guard** — confirmed at `process.rs:753`
5. **`verify_queue_contains` and `orphan_contains` each acquire and immediately release their own read locks** — confirmed at `process.rs:237-245`; both are point-in-time checks with no lock held afterward
6. **`block_in_place` used for local path** — confirmed at `util.rs:117-130`
7. **`enqueue_verify_queue` holds a write lock** — confirmed at `process.rs:866`; the remote path holds the write lock atomically, preventing duplicate enqueue
8. **`DEFAULT_CHANNEL_SIZE = 512`** — confirmed at `service.rs:53`

The race window is real: from the moment `pre_check` drops its read lock to the moment `submit_entry` acquires its write lock, the transaction is invisible to all duplicate guards. Multiple concurrent tasks can all observe an empty pool, all proceed to `verify_rtx`, and all race to `submit_entry`. The `resumeble_process_tx` path has a similar TOCTOU between `verify_queue_contains` and `enqueue_verify_queue`, but `enqueue_verify_queue`'s write lock at least serializes the final insertion. The local `process_tx` path has no equivalent serialization point before the expensive `verify_rtx`.

---

Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Concurrent Duplicate Script Verification — (File: `tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the duplicate-membership check (`check_txid_collision`) is performed under a short-lived read lock that is released before the expensive `verify_rtx` script-execution step. Because the service loop spawns an independent Tokio task per message, two or more concurrent `SubmitLocalTx` submissions of the same transaction can each pass all duplicate guards and each execute full RISC-V script verification before any one of them commits the transaction to the pool. With a channel capacity of 512, an attacker with local RPC access can trigger up to 512 concurrent full-cycle verifications of a single transaction, exhausting Tokio worker threads and rendering the node unresponsive to block relay and sync for the duration of the attack.

## Finding Description

**Service loop spawns one task per message** (`service.rs:619-621`): each `SubmitLocalTx` message is dispatched as an independent `tokio::spawn`'d task, so all 512 concurrent submissions run fully in parallel.

**`_process_tx` execution order** (`process.rs:705-753`):
1. `pre_check` at line 715 calls `with_tx_pool_read_lock`, which acquires a read lock, runs `check_txid_collision` inside the closure, then **drops the lock when the closure returns** (lines 276-314). The lock is gone before `pre_check` returns.
2. `verify_rtx` at lines 724-732 runs with **no lock held**. For the local path (`command_rx = None`), this calls `block_in_place`, blocking a Tokio worker thread for the full duration of RISC-V script execution.
3. `submit_entry` at line 753 acquires a write lock and performs the first write that prevents a duplicate.

**Earlier guards in `process_tx` also release their locks before `_process_tx` is entered** (`process.rs:409-411`): `verify_queue_contains` (`process.rs:237-240`) and `orphan_contains` (`process.rs:242-245`) each acquire and immediately release their own read locks. Neither check covers the window between lock release and `submit_entry` completion.

**Race window**: From the moment `pre_check` releases its read lock to the moment `submit_entry` acquires its write lock, the transaction is invisible to all duplicate guards. This window spans the entire duration of `verify_rtx`. N concurrent tasks all observe an empty pool, all proceed to `verify_rtx`, and all race to `submit_entry`. Only the first insertion succeeds; all others are wasted CPU.

**Contrast with the remote path** (`process.rs:860-868`): `enqueue_verify_queue` acquires a write lock on `verify_queue` before returning, preventing duplicate enqueuing. The local `process_tx` path has no equivalent atomic guard before the expensive verification step.

## Impact Explanation

`verify_rtx` for the local path uses `block_in_place` (`util.rs:117-130`), which blocks a Tokio worker thread per concurrent verification. Tokio's worker thread pool is bounded by CPU core count (typically 8-16 threads). With `DEFAULT_CHANNEL_SIZE = 512` (`service.rs:53`), an attacker can enqueue 512 concurrent verifications of a single max-cycle transaction (~3.5 billion RISC-V cycles on mainnet). Each verification blocks a worker thread for hundreds of milliseconds. This exhausts the Tokio thread pool, preventing the node from processing block relay, sync, and mining template generation for the duration of script execution. The attacker can repeat the attack continuously to sustain the outage. This matches **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The `send_transaction` RPC endpoint is the standard interface for wallets and SDKs. Any caller with local RPC access (default: `127.0.0.1:8114`) can issue 512 concurrent HTTP requests with no special privileges, keys, or network position. The race window is wide — hundreds of milliseconds for a max-cycle transaction — making concurrent exploitation straightforward with a simple shell loop or script. No victim mistakes or external context are required. The attack is repeatable and sustainable.

## Recommendation

Apply the check-effects-interactions pattern to close the race window:

1. **Add an "in-verification" set** (e.g., `Arc<RwLock<HashSet<Byte32>>>`) to `TxPoolService`.
2. **Before entering `verify_rtx`** in `_process_tx`, acquire a write lock on this set, check if the tx hash is already present (return `Reject::Duplicated` if so), insert it, then release the lock.
3. **Remove the entry** from the set on completion (success or failure) of `verify_rtx` + `submit_entry`.
4. Extend `check_txid_collision` or add a new guard in `process_tx` to check this set atomically before entering `_process_tx`.

This mirrors the protection already present in `enqueue_verify_queue` (`process.rs:866`), which holds a write lock on `verify_queue` before returning, preventing duplicate enqueuing on the remote path.

## Proof of Concept

```bash
# Submit a max-cycle transaction 512 times concurrently via local RPC
for i in $(seq 1 512); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All 512 tasks pass `verify_queue_contains` and `orphan_contains` (point-in-time read locks, immediately released), all 512 pass `pre_check`/`check_txid_collision` (pool is empty, read lock dropped before `verify_rtx`), all 512 invoke `verify_rtx` concurrently via `block_in_place`, exhausting Tokio worker threads. Only one `submit_entry` succeeds; 511 verifications are wasted. The node becomes unresponsive to block relay and sync for the duration of script execution. Observable via node logs showing 511 `Duplicated` rejections after all verifications complete, and via block relay latency spikes during the attack window. The attack can be repeated immediately after each wave completes to sustain the outage indefinitely.