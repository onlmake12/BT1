Audit Report

## Title
Tight Infinite Spin Loop on Persistent `indexer.tip()` or `indexer.append()` Error in `try_loop_sync` — (File: util/indexer-sync/src/lib.rs)

## Summary
`IndexerSyncService::try_loop_sync` contains an unbounded `loop {}` with error arms for both `indexer.tip()` and `indexer.append()` that log the error and fall through with no `break`, no sleep, and no backoff. A persistent RocksDB error (e.g., from disk exhaustion) causes the function to spin at 100% CPU on its Tokio blocking thread indefinitely, freezing the indexer's sync tip and rendering all Indexer RPC responses permanently stale. The secondary claim of blocking thread pool exhaustion is not supported by the code, as the `.await` on each `spawn_blocking` call serializes invocations.

## Finding Description
In `util/indexer-sync/src/lib.rs`, `try_loop_sync` drives the indexer forward one block at a time inside an unbounded `loop {}`.

**Case 1 — `indexer.tip()` error (lines 195–197):**
```rust
Err(e) => {
    error!("Failed to get tip: {}", e);
    // no break, no sleep — falls through to next iteration
}
```
When `indexer.tip()` returns `Err`, the arm logs and exits the match. Control returns immediately to the top of the loop, calls `indexer.tip()` again, receives the same error, and repeats forever with no yield point.

**Case 2 — `indexer.append()` error (lines 160–162 and 186–188):**
```rust
if let Err(e) = indexer.append(&block) {
    error!("Failed to append block: {}. Will attempt to retry.", e);
}
```
When `append` fails, the indexer tip has not advanced. The next iteration calls `indexer.tip()` (succeeds), fetches the same block, and calls `append` again — tight spin.

Both error variants originate from `rocksdb::Error` wrapped as `Error::DB` in `util/indexer-sync/src/error.rs` lines 25–28.

The function is dispatched via `spawn_blocking` at lines 214–216 (initial sync) and lines 240–244 (follow-up sync). The follow-up sync path `.await`s each `spawn_blocking` result before the `tokio::select!` loop can proceed to the next event. Therefore, if `try_loop_sync` spins forever, the async loop is blocked at the `.await` and does **not** spawn additional blocking tasks. The thread pool exhaustion claim is incorrect; only one blocking thread is consumed at a time per indexer instance.

Existing guards are insufficient: the only loop exit conditions are `has_received_stop_signal()` (line 144) and `get_block_by_number` returning `None` (line 180). Neither fires on a persistent RocksDB error.

## Impact Explanation
A persistent RocksDB error causes `try_loop_sync` to spin at 100% CPU on one Tokio blocking thread indefinitely. The indexer's sync tip freezes, and all Indexer RPC methods (`get_indexer_tip`, `get_cells`, `get_transactions`, `get_cells_capacity`) return permanently stale data for the duration of the error condition. This matches **Note (0–500 points): Any local RPC API crash**. Core chain processing is architecturally independent of the indexer and is unaffected.

## Likelihood Explanation
The indexer is not enabled by default: `resource/ckb.toml` line 190 shows `modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]` — `"Indexer"` is absent. A user must explicitly add `"Indexer"` to the config. For users who do enable it, disk exhaustion is a realistic trigger: once disk is full, RocksDB write and metadata-read operations return errors, causing `indexer.tip()` or `indexer.append()` to return `Error::DB`. An unprivileged attacker can drive disk usage via sustained `send_transaction` RPC calls or P2P tx-relay, though filling an entire node's disk requires sustained effort.

## Recommendation
**For the `tip()` error arm (line 195–197):** add a `break` or sleep to exit or throttle the loop on persistent failure:
```rust
Err(e) => {
    error!("Failed to get tip: {}", e);
    break;
}
```
**For both `append()` error arms (lines 160–162 and 186–188):** similarly break or introduce exponential backoff:
```rust
if let Err(e) = indexer.append(&block) {
    error!("Failed to append block: {}", e);
    break;
}
```
At minimum, a `sleep` of 1 second between retries would prevent CPU saturation, consistent with the pattern already used in `apply_init_tip` (line 131) and `check_index_tx_pool_ready` (line 293).

## Proof of Concept
1. Start a CKB node with `"Indexer"` added to `modules` in `ckb.toml`.
2. Fill the node's disk to capacity (e.g., via sustained `send_transaction` RPC calls with valid transactions).
3. Once disk is full, RocksDB returns `rocksdb::Error` on write/metadata operations.
4. The next new-block notification triggers `spawn_poll` → `spawn_blocking(try_loop_sync)`.
5. Inside `try_loop_sync`, `indexer.tip()` calls into RocksDB and returns `Err(Error::DB(...))`.
6. The `Err(e)` arm at lines 195–197 logs and falls through; the loop immediately retries.
7. The blocking thread spins at 100% CPU indefinitely; the indexer tip freezes.
8. All Indexer RPC calls return stale data for the duration of the error condition.
9. Confirm: `get_indexer_tip` returns a block number that no longer advances while the node's chain tip continues to grow.