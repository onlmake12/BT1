The code path is fully traceable. Here is the analysis:

**Default initialization** — `RichIndexerService::new` sets `request_limit` to `usize::MAX` when the config field is absent: [1](#0-0) 

**The guard** in `AsyncRichIndexerHandle::get_cells`: [2](#0-1) 

On a 64-bit system, `u32::MAX as usize` = `4294967295`, while `usize::MAX` = `18446744073709551615`. The condition `4294967295 > 18446744073709551615` is always **false**, so the guard never fires when `request_limit` is at its default.

**The SQL LIMIT** is set directly from the unchecked `limit` value: [3](#0-2) 

**`fetch_all` loads the entire result set into memory:** [4](#0-3) 

The config comment explicitly acknowledges the risk and recommends `request_limit = 400`, but the default remains `usize::MAX`: [5](#0-4) 

The same unchecked pattern exists in `get_transactions`: [6](#0-5) 

---

### Title
Unbounded `get_cells`/`get_transactions` RPC limit causes OOM node crash when `request_limit` is unconfigured — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`)

### Summary
When `request_limit` is not set in `ckb.toml`, `RichIndexerService::new` defaults it to `usize::MAX`. The only guard — `limit as usize > self.request_limit` — is permanently false on 64-bit systems because `u32::MAX as usize` (4,294,967,295) is always less than `usize::MAX` (18,446,744,073,709,551,615). An unauthenticated RPC caller can send `get_cells(limit=4294967295)`, causing the node to issue `SELECT … LIMIT 4294967295` and load the entire result set into process memory via `fetch_all`, crashing the node via OOM.

### Finding Description
- `RichIndexerService::new` (`util/rich-indexer/src/service.rs:51`): `config.request_limit.unwrap_or(usize::MAX)` — default is effectively unlimited.
- `AsyncRichIndexerHandle::get_cells` (`get_cells.rs:29`): guard `limit as usize > self.request_limit` is never true when `request_limit = usize::MAX`.
- `get_cells.rs:156`: `query_builder.limit(limit)` embeds the attacker-controlled value directly into SQL as `LIMIT 4294967295`.
- `get_cells.rs:229–239`: `fetch_all` materializes the entire result set into a `Vec` in process memory.
- Identical flaw in `get_transactions` (`get_transactions.rs:27`).

### Impact Explanation
A single unauthenticated JSON-RPC call to `get_cells` or `get_transactions` with `limit=4294967295` against a node with rich-indexer enabled and no `request_limit` configured will cause the node process to attempt to allocate memory proportional to the number of indexed cells/transactions. On a fully-synced mainnet node (tens of millions of cells), this exhausts process memory and crashes the node. The RPC module is network-accessible with no authentication by default.

### Likelihood Explanation
The default configuration (`request_limit` absent) is the out-of-the-box state for any operator who enables rich-indexer without reading the commented-out recommendation in `ckb.toml`. The attack requires one HTTP POST. No keys, hashpower, or privileged access are needed.

### Recommendation
Change the default in `RichIndexerService::new` from `usize::MAX` to a safe bounded value (e.g., 400 as the documentation already recommends), or enforce a hard cap independent of configuration:

```rust
// service.rs:51
request_limit: config.request_limit.unwrap_or(400),
```

Alternatively, add a secondary hard cap in `get_cells`/`get_transactions` that rejects any `limit` exceeding a compile-time constant (e.g., 4096) regardless of `request_limit`.

### Proof of Concept
```bash
# Against a node with rich-indexer enabled, no request_limit set, large index
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0","id":1,"method":"get_cells",
    "params":[
      {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000","hash_type":"data","args":"0x"},"script_type":"lock"},
      "asc",
      "0xffffffff",
      null
    ]
  }'
# Monitor: watch -n1 'ps aux | grep ckb | awk "{print \$6}"'
# Expected: RSS grows until OOM killer terminates the process
```

### Citations

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L25-34)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L156-156)
```rust
        query_builder.limit(limit);
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L229-239)
```rust
        let cells = self
            .store
            .fetch_all(query)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .iter()
            .map(|row| {
                last_cursor = row.get::<i64, _>("id").to_le_bytes().to_vec();
                build_indexer_cell(row)
            })
            .collect::<Vec<_>>();
```

**File:** resource/ckb.toml (L286-291)
```text
# # By default, there is no limitation on the size of indexer request
# # However, because serde json serialization consumes too much memory(10x),
# # it may cause the physical machine to become unresponsive.
# # We recommend a consumption limit of 2g, which is 400 as the limit,
# # which is a safer approach
# request_limit = 400
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L23-32)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```
