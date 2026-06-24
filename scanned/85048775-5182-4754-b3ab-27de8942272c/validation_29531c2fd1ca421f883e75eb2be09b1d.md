Audit Report

## Title
`get_overview` Permanently Fails on Non-Mining Nodes Due to Unconditional `get_block_template` Call — (`rpc/src/module/terminal.rs`)

## Summary

`TerminalRpcImpl::get_tx_pool_info` unconditionally calls `self.shared.get_block_template(None, None, None)` to populate the `committing` field of `TerminalPoolInfo`. When `block_assembler` is not configured (the default for non-mining nodes), `TxPoolService::get_block_template` returns `Err(InternalErrorKind::Config, "BlockAssembler disabled")`. This error propagates through `get_tx_pool_info` → `get_overview` as a `CKBInternalError`, and because the cache is only written on success, every subsequent call repeats the same failure.

## Finding Description

**Root cause:** `get_tx_pool_info` at line 590–600 of `rpc/src/module/terminal.rs` calls `get_block_template` with no guard for the `block_assembler` being absent:

```rust
let block_template = self
    .shared
    .get_block_template(None, None, None)
    .map_err(|err| { ... RPCError::ckb_internal_error(err) })?
    .map_err(|err| { ... RPCError::from_any_error(err) })?;
```

**Failure source:** `TxPoolService::get_block_template` in `tx-pool/src/process.rs` lines 66–74 returns `Err` immediately when `self.block_assembler` is `None`:

```rust
pub(crate) async fn get_block_template(&self) -> Result<BlockTemplate, AnyError> {
    if let Some(ref block_assembler) = self.block_assembler {
        Ok(block_assembler.get_current().await)
    } else {
        Err(InternalErrorKind::Config.other("BlockAssembler disabled").into())
    }
}
```

**Propagation:** The `?` at line 596 converts this into a `CKBInternalError` and returns it. The call at line 452 (`let pool = self.get_tx_pool_info(refresh)?;`) propagates it to the caller of `get_overview`.

**Cache never warms:** `self.cache.set_tx_pool_info(...)` at line 620 is only reached on success. Since `get_block_template` always fails without a block assembler, the cache remains permanently cold. Even calls with `refresh=null` (which would use the cache) fail because the cache is never populated.

**Existing guard is insufficient:** The cache bypass at lines 574–578 only helps if the cache was previously populated — which it never is on a non-mining node.

## Impact Explanation

Any call to the `get_overview` RPC endpoint on a non-mining node returns a `CKBInternalError`. The node process itself is unaffected; only this RPC method is broken. This matches the allowed bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

Certain and immediate. The default CKB node configuration does not include `block_assembler`. Any node operator or monitoring tool calling `get_overview` on a standard full node will observe the failure on the very first call, with no special conditions required.

## Recommendation

Guard the `get_block_template` call in `get_tx_pool_info` to handle the absent block assembler case gracefully:

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

Alternatively, return `0` for `committing` when `block_assembler` is absent rather than propagating a fatal error.

## Proof of Concept

```bash
# Start a node without block_assembler in ckb.toml (default config)
ckb run

# Call get_overview — fails on every call (cache never warms)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_overview","params":[null],"id":1}'

# Expected response:
# {"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"CKBInternalError BlockAssembler disabled"}}
```

The failure is reproducible on any default CKB node installation without modifying any configuration.