Audit Report

## Title
Tight Infinite Spin Loop on Persistent `indexer.tip()` or `indexer.append()` Error in `try_loop_sync` — (`util/indexer-sync/src/lib.rs`)

## Summary
`IndexerSyncService::try_loop_sync` contains a `loop {}` with two error arms — for `indexer.tip()` returning `Err` and for `indexer.append()` returning `Err` — that log the error and fall through with no `break`, no sleep, and no backoff. A persistent RocksDB error (e.g., from disk exhaustion) causes the function to spin at 100% CPU on its `spawn_blocking` thread indefinitely, permanently stalling the indexer's sync progress and rendering the Indexer RPC stale. The claim's secondary assertion that the Tokio blocking thread pool is progressively exhausted is not supported by the code, as the `.await` on each `spawn_blocking` call prevents concurrent spinning tasks from accumulating.

## Finding Description
In `util/indexer-sync/src/lib.rs`, `try_loop_sync` drives the indexer forward one block at a time inside an unbounded `loop {}`.

**Case 1 — `indexer.tip()` error (lines 195–197):**
```rust
Err(e) => {
    error!("Failed to get tip: {}", e);
    // no break, no sleep — falls through to next iteration
}
```
When `indexer.tip()` returns `Err`, the arm logs and exits. Control returns immediately to the top of the loop, calls `indexer.tip()` again, gets the same error, and repeats forever.

**Case 2 — `indexer.append()` error (lines 160–162 and 186–188):**
```rust
if let Err(e) = indexer.append(&block) {
    error!("Failed to append block: {}. Will attempt to retry.", e);
}
```
When `append` fails, the indexer tip has not advanced. The next iteration calls `indexer.tip()` (succeeds), fetches the same block, and calls `append` again — tight spin.

Both error variants originate from `rocksdb::Error` wrapped as `Error::DB` in `util/indexer-sync/src/error.rs` lines 25–28.

The function is dispatched via `spawn_blocking` at lines 214–216 (initial sync) and lines 240–244 (follow-up sync). Critically, the follow-up sync path `.await`s each `spawn_blocking` result before the `tokio::select!` loop can proceed to the next event. Therefore, if `try_loop_sync` spins forever, the async loop is blocked at the `.await` and does **not** spawn additional blocking tasks for subsequent new-block notifications. The thread pool exhaustion claim in the submission is incorrect; only one blocking thread is consumed at a time per indexer instance.

## Impact Explanation
A persistent RocksDB error causes `try_loop_sync` to spin at 100% CPU on one Tokio blocking thread indefinitely. The indexer's sync tip freezes, and all Indexer RPC methods (`get_indexer_tip`, `get_cells`, `get_transactions`, `get_cells_capacity`) return permanently stale data or fail. This matches **Note (0–500 points): Any local RPC API crash**. The claim's assertion of High impact via blocking thread pool exhaustion is not supported because the `.await` pattern at lines 241–244 and 249–253 serializes `spawn_blocking` invocations, preventing accumulation of concurrent spinning tasks. Core chain processing (block validation, consensus) is architecturally independent of the indexer and is unaffected.

## Likelihood Explanation
The indexer is **not enabled by default**: the default `modules` list in `resource/ckb.toml` line 190 is `["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]` — `"Indexer"` is absent. A user must explicitly pass `--indexer` or add `"Indexer"` to the config. For users who do enable it, disk exhaustion (via sustained valid transaction submission) is a realistic trigger: once disk is full, RocksDB write and metadata-read operations return errors, causing `indexer.tip()` or `indexer.append()` to return `Error::DB`. The attacker requires no special privileges — `send_transaction` via RPC or P2P tx-relay is sufficient to drive disk usage, though filling an entire node's disk requires sustained effort and cost.

## Recommendation
**For the `tip()` error arm:** add a `break` (or sleep + bounded retry) to exit the loop on persistent failure:
```rust
Err(e) => {
    error!("Failed to get tip: {}", e);
    break;
}
```
**For both `append()` error arms:** similarly break or introduce exponential backoff with a maximum retry count:
```rust
if let Err(e) = indexer.append(&block) {
    error!("Failed to append block: {}", e);
    break; // or: sleep + bounded retry counter
}
```
At minimum, a `sleep` of even 1 second between retries would prevent CPU saturation and is consistent with the pattern already used in `apply_init_tip` (line 131) and `check_index_tx_pool_ready` (line 293).

## Proof of Concept
1. Start a CKB node with `--indexer` enabled.
2. Fill the node's disk to capacity (e.g., via sustained `send_transaction` RPC calls with valid transactions, or by other means).
3. Once disk is full, RocksDB returns `rocksdb::Error` on write/metadata operations.
4. The next new-block notification triggers `spawn_poll` → `spawn_blocking(try_loop_sync)`.
5. Inside `try_loop_sync`, `indexer.tip()` calls into RocksDB and returns `Err(Error::DB(...))`.
6. The `Err(e)` arm at lines 195–197 logs and falls through; the loop immediately retries.
7. The blocking thread spins at 100% CPU indefinitely; the indexer tip freezes.
8. All Indexer RPC calls return stale data for the duration of the error condition.
9. Confirm: `get_indexer_tip` returns a block number that no longer advances while the node's chain tip continues to grow.