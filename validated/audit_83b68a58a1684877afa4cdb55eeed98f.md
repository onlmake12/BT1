### Title
Rhai Filter Evaluation Panics on Runtime Error, Permanently Stalling the Indexer Sync Loop — (`util/indexer-sync/src/custom_filters.rs`)

---

### Summary

`CustomFilters::is_block_filter_match()` and `is_cell_filter_match()` call `.expect()` on the result of `Engine::eval_ast_with_scope()`. If the Rhai script evaluation returns a runtime error for any reason — including attacker-controlled cell data that triggers a type error in the script — the thread panics. Because the indexer sync loop retries the same block indefinitely, the panic recurs on every retry, permanently stalling the indexer. No further blocks are indexed, and all RPC endpoints backed by the indexer return stale data.

---

### Finding Description

`CustomFilters` in `util/indexer-sync/src/custom_filters.rs` evaluates operator-configured Rhai scripts against every block and every cell output during indexing. Both evaluation sites use `.expect()` instead of returning a `Result`:

```rust
// is_block_filter_match — line 91-92
self.engine
    .eval_ast_with_scope(&mut scope, block_filter)
    .expect("eval block_filter should be ok")
```

```rust
// is_cell_filter_match — line 111-112
self.engine
    .eval_ast_with_scope(&mut scope, cell_filter)
    .expect("eval cell_filter should be ok")
```

`eval_ast_with_scope` returns `Result<T, Box<EvalAltResult>>`. It returns `Err` whenever the script encounters a runtime error: accessing a field on a `()` value, a type mismatch, hitting an execution limit, or any other Rhai runtime condition. The `.expect()` call converts that `Err` into a panic.

This panic propagates out of `Indexer::append()`, through `try_loop_sync()`, and terminates the `spawn_blocking` task. The sync loop in `IndexerSyncService::spawn_poll()` catches the `JoinError` and logs it, but does **not** advance the indexer tip. On the next trigger (new block notification or poll interval), a new `spawn_blocking` task is spawned, calls `try_loop_sync()`, attempts to append the same block, hits the same panic, and the cycle repeats indefinitely.

The concrete attacker path: if the operator has configured a `cell_filter` such as:

```
output.type.args == "0x..."
```

(without the `?.` optional-chaining operator), an attacker submits a transaction whose output has no type script. The Rhai engine evaluates `output.type` as `()`, then attempts to access `.args` on `()`, which is a Rhai runtime error. The `.expect()` panics, and the indexer stalls permanently on that block.

The same class of failure applies to `is_block_filter_match` if the block-level filter script can produce a runtime error on attacker-influenced block data (e.g., a block containing a transaction whose fields cause a type error in the script).

Both the basic indexer (`util/indexer`) and the rich indexer (`util/rich-indexer`) share this `CustomFilters` implementation, so both are affected.

---

### Impact Explanation

Once the panic is triggered:

- The indexer tip is never advanced past the offending block.
- Every subsequent sync attempt panics on the same block.
- All indexer RPC methods (`get_cells`, `get_transactions`, `get_cells_capacity`) return permanently stale data.
- The node itself continues to run and participate in consensus; only the indexer subsystem is broken.
- Recovery requires operator intervention (restarting the node, resetting the indexer database, or modifying the filter script).

---

### Likelihood Explanation

The attack requires two preconditions:

1. The node operator has configured a `block_filter` or `cell_filter` in `ckb.toml`. This is a documented, officially supported feature with examples in the default config and README.
2. The configured filter script can produce a Rhai runtime error on attacker-controlled cell data. A filter script that accesses optional fields without `?.` (e.g., `output.type.args`) is sufficient; this is a natural mistake for operators writing their first filter.

An attacker who observes that a node's indexer is running (via RPC responses) and can infer or guess the filter logic (e.g., from public documentation or the node's announced purpose) can craft a single transaction to trigger the stall. The transaction itself is valid on-chain and will be committed to a block normally; only the indexer is affected.

---

### Recommendation

Replace `.expect()` with proper error handling in both filter methods. On evaluation error, log the error and return a safe default (`false` to exclude the item, or `true` to include it conservatively), rather than panicking:

```rust
// is_block_filter_match
self.engine
    .eval_ast_with_scope::<bool>(&mut scope, block_filter)
    .unwrap_or_else(|e| {
        error!("block_filter eval error: {}", e);
        false
    })

// is_cell_filter_match
self.engine
    .eval_ast_with_scope::<bool>(&mut scope, cell_filter)
    .unwrap_or_else(|e| {
        error!("cell_filter eval error: {}", e);
        false
    })
```

Similarly, the `.unwrap()` on `engine.parse_json(...)` at lines 86–87 and 104–105 should be replaced with error handling for the same reason.

---

### Proof of Concept

**Setup**: Configure `ckb.toml` with a cell filter that accesses `output.type.args` without optional chaining:

```toml
[indexer_v2]
cell_filter = "output.type.args == \"0x1234\""
```

**Attack**: Submit a valid CKB transaction whose output has no type script (a plain transfer). This is a standard, unrestricted operation for any CKB user.

**Result**: When the indexer processes this block, `is_cell_filter_match` evaluates `output.type` as `()` and then attempts `.args` on it, producing a Rhai `EvalAltResult`. The `.expect()` panics. The `spawn_blocking` task terminates with a panic. The sync loop retries the same block on every subsequent poll interval, panicking each time. The indexer tip is permanently stuck at the block before the offending one. All indexer RPC queries return stale results indefinitely. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** util/indexer-sync/src/custom_filters.rs (L79-95)
```rust
    pub fn is_block_filter_match(&self, block: &BlockView) -> bool {
        self.block_filter
            .as_ref()
            .map(|block_filter| {
                let json_block: ckb_jsonrpc_types::BlockView = block.clone().into();
                let parsed_block = self
                    .engine
                    .parse_json(serde_json::to_string(&json_block).unwrap(), true)
                    .unwrap();
                let mut scope = Scope::new();
                scope.push("block", parsed_block);
                self.engine
                    .eval_ast_with_scope(&mut scope, block_filter)
                    .expect("eval block_filter should be ok")
            })
            .unwrap_or(true)
    }
```

**File:** util/indexer-sync/src/custom_filters.rs (L98-115)
```rust
    pub fn is_cell_filter_match(&self, output: &CellOutput, output_data: &Bytes) -> bool {
        self.cell_filter
            .as_ref()
            .map(|cell_filter| {
                let json_output: ckb_jsonrpc_types::CellOutput = output.clone().into();
                let parsed_output = self
                    .engine
                    .parse_json(serde_json::to_string(&json_output).unwrap(), true)
                    .unwrap();
                let mut scope = Scope::new();
                scope.push("output", parsed_output);
                scope.push("output_data", format!("{output_data:#x}"));
                self.engine
                    .eval_ast_with_scope(&mut scope, cell_filter)
                    .expect("eval cell_filter should be ok")
            })
            .unwrap_or(true)
    }
```

**File:** util/indexer-sync/src/lib.rs (L155-165)
```rust
                                    "{} append {}, {}",
                                    indexer.get_identity(),
                                    block.number(),
                                    block.hash()
                                );
                                if let Err(e) = indexer.append(&block) {
                                    error!("Failed to append block: {}. Will attempt to retry.", e);
                                }
                            } else {
                                info!(
                                    "{} rollback {}, {}",
```

**File:** util/indexer-sync/src/lib.rs (L236-254)
```rust
                let indexer = indexer_service.clone();
                tokio::select! {
                    Ok(_) = new_block_watcher.changed() => {
                        let service = poll_service.clone();
                        if let Err(e) = async_handle.spawn_blocking(move || {
                            service.try_loop_sync(indexer)
                        }).await {
                            error!("{} syncing join error {:?}", indexer_service.get_identity(), e);
                        }
                        new_block_watcher.borrow_and_update();
                    },
                    _ = interval.tick() => {
                        let service = poll_service.clone();
                        if let Err(e) = async_handle.spawn_blocking(move || {
                            service.try_loop_sync(indexer)
                        }).await {
                            error!("{} syncing join error {:?}", indexer_service.get_identity(), e);
                        }
                    }
```

**File:** util/indexer/src/indexer.rs (L317-330)
```rust
    fn append(&self, block: &BlockView) -> Result<(), Error> {
        let mut batch = self.store.batch()?;
        let transactions = block.transactions();
        let pool = self.pool.as_ref().map(|p| p.write().expect("acquire lock"));
        if !self.custom_filters.is_block_filter_match(block) {
            batch.put_kv(Key::Header(block.number(), &block.hash(), true), vec![])?;
            batch.commit()?;

            if let Some(mut pool) = pool {
                pool.transactions_committed(&transactions);
            }

            return Ok(());
        }
```
