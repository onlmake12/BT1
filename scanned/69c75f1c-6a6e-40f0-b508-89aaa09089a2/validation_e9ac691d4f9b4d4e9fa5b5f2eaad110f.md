Now I have enough to trace the full call chain. Let me verify the exact `TxPoolService::get_block_template` path and how the error is returned through the controller.

The call chain is fully confirmed. Here is the assessment:

---

### Title
`get_overview` Permanently Fails with `CKBInternalError` on Non-Mining Nodes Due to Unconditional `get_block_template` Call — (`rpc/src/module/terminal.rs`)

### Summary

`TerminalRpcImpl::get_tx_pool_info` unconditionally calls `shared.get_block_template(None, None, None)` to populate the `committing` field of `TerminalPoolInfo`. When `block_assembler` is not configured (the default for non-mining nodes), `TxPoolService::get_block_template` returns `Err(BlockAssemblerError::Disabled)`. This error propagates through `get_tx_pool_info` → `get_overview` as a `CKBInternalError`. Because the cache is only written on success, it is never populated, so every call to `get_overview` on a non-mining node permanently fails.

### Finding Description

**Entry point:** Any caller with access to the RPC endpoint sends `get_overview` with `refresh=0x4` (or any value, since the cache is never warm).

**Step 1 — `get_overview` dispatches to `get_tx_pool_info`:** [1](#0-0) 

**Step 2 — Cache bypass:** The cache guard at lines 574–578 is skipped when `TX_POOL_INFO` is set in `refresh`, or when the cache is cold (which it always is, since it is never successfully populated). [2](#0-1) 

**Step 3 — Unconditional `get_block_template` call:** After fetching basic pool info, the function calls `get_block_template` solely to read `block_template.transactions.len()` for the `committing` field: [3](#0-2) 

**Step 4 — `TxPoolService::get_block_template` returns `Err` when `block_assembler` is `None`:** [4](#0-3) 

**Step 5 — Error propagates:** The second `.map_err(...)?` at line 597–600 converts `BlockAssemblerError::Disabled` into a `CKBInternalError` and returns it. The `?` at line 452 in `get_overview` propagates it to the caller.

**Step 6 — Cache is never written:** `self.cache.set_tx_pool_info(...)` at line 620 is only reached on success. Since `get_block_template` always fails without a block assembler, the cache remains permanently cold, causing every subsequent call to repeat the same failure. [5](#0-4) 

### Impact Explanation

`get_overview` is permanently broken on any node without `block_assembler` configured. This is the default state for all non-mining full nodes. The `Terminal` module is documented as a general-purpose monitoring endpoint ("TUI Monitoring Dashboards", "System Administration"), not a miner-only API, yet it is silently unusable on the majority of deployed nodes. Every call returns `CKBInternalError` with message `"BlockAssembler disabled"`.

### Likelihood Explanation

Certain. Any node operator or monitoring tool calling `get_overview` on a standard (non-mining) CKB node will immediately observe the failure. No special conditions are required — the default node configuration is sufficient to trigger it.

### Recommendation

In `get_tx_pool_info`, guard the `get_block_template` call:

```rust
let committing = if self.shared.tx_pool_controller().block_assembler_is_some() {
    self.shared
        .get_block_template(None, None, None)
        .ok()
        .and_then(|r| r.ok())
        .map(|t| t.transactions.len() as u64)
        .unwrap_or(0)
} else {
    0
};
```

Or return `None`/`0` for `committing` when `block_assembler` is absent, rather than propagating a fatal error.

### Proof of Concept

```bash
# Start a node without block_assembler in ckb.toml (default config)
ckb run

# Immediately call get_overview — fails on first call (cache cold)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_overview","params":[4],"id":1}'

# Expected (broken) response:
# {"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"CKBInternalError ..."}}

# Also fails with null (after 2-second TTL, cache never warms):
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_overview","params":[null],"id":1}'
```

### Citations

**File:** rpc/src/module/terminal.rs (L452-452)
```rust
        let pool = self.get_tx_pool_info(refresh)?;
```

**File:** rpc/src/module/terminal.rs (L574-578)
```rust
        if !refresh.contains(RefreshKind::TX_POOL_INFO)
            && let Some(cached) = self.cache.get_tx_pool_info()
        {
            return Ok(cached);
        }
```

**File:** rpc/src/module/terminal.rs (L590-611)
```rust
        let block_template = self
            .shared
            .get_block_template(None, None, None)
            .map_err(|err| {
                error!("Send get_block_template request error {}", err);
                RPCError::ckb_internal_error(err)
            })?
            .map_err(|err| {
                error!("Get_block_template result error {}", err);
                RPCError::from_any_error(err)
            })?;

        let total_recent_reject_num = tx_pool.get_total_recent_reject_num().map_err(|err| {
            error!("Get_total_recent_reject_num result error {}", err);
            RPCError::from_any_error(err)
        })?;

        let tx_pool_info = TerminalPoolInfo {
            pending: (info.pending_size as u64).into(),
            proposed: (info.proposed_size as u64).into(),
            orphan: (info.orphan_size as u64).into(),
            committing: (block_template.transactions.len() as u64).into(),
```

**File:** rpc/src/module/terminal.rs (L619-621)
```rust
        // Cache the result
        self.cache.set_tx_pool_info(tx_pool_info.clone());
        Ok(tx_pool_info)
```

**File:** tx-pool/src/process.rs (L66-74)
```rust
    pub(crate) async fn get_block_template(&self) -> Result<BlockTemplate, AnyError> {
        if let Some(ref block_assembler) = self.block_assembler {
            Ok(block_assembler.get_current().await)
        } else {
            Err(InternalErrorKind::Config
                .other("BlockAssembler disabled")
                .into())
        }
    }
```
