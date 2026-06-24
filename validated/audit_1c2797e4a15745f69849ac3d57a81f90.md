The code matches the claims exactly. The vulnerability is real and confirmed.

Audit Report

## Title
`get_overview` Permanently Fails with `CKBInternalError` on Non-Mining Nodes Due to Unconditional `get_block_template` Call — (`rpc/src/module/terminal.rs`)

## Summary

`TerminalRpcImpl::get_tx_pool_info` unconditionally calls `self.shared.get_block_template(None, None, None)` and propagates its error via `?`. When `block_assembler` is not configured (the default for non-mining nodes), `TxPoolService::get_block_template` returns `Err(InternalErrorKind::Config.other("BlockAssembler disabled"))`, which converts to a `CKBInternalError` and is returned immediately. Because the cache is only written on success, every call to `get_overview` on a non-mining node permanently fails.

## Finding Description

`get_tx_pool_info` at `rpc/src/module/terminal.rs:590–600` calls `get_block_template` with no guard for the case where `block_assembler` is absent: [1](#0-0) 

`TxPoolService::get_block_template` at `tx-pool/src/process.rs:66–74` returns an error when `self.block_assembler` is `None`: [2](#0-1) 

The two chained `.map_err(...)?` operators at lines 593–600 convert this into a `CKBInternalError` and return immediately, bypassing the cache write at line 620: [3](#0-2) 

The cache bypass check at lines 574–578 never finds a cached value, so every subsequent call re-enters the same failing path: [4](#0-3) 

`get_overview` at line 452 calls `get_tx_pool_info` with `?`, propagating the error directly to the caller: [5](#0-4) 

## Impact Explanation

`get_overview` is permanently broken on any node without `block_assembler` configured, which is the default state for all non-mining full nodes. Every call returns `CKBInternalError` with message `"BlockAssembler disabled"`. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

Certain and immediate. No special conditions are required beyond running a standard non-mining CKB node (the default configuration). Any node operator or monitoring tool calling `get_overview` will observe the failure on the very first call. The condition is deterministic and repeatable.

## Recommendation

Guard the `get_block_template` call in `get_tx_pool_info` (`rpc/src/module/terminal.rs`) so it is only invoked when a block assembler is present, and default `committing` to `0` otherwise:

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

Alternatively, replace the `?` propagation with `.ok().unwrap_or_default()` on the `get_block_template` result so the error is silently swallowed and `committing` defaults to `0`.

## Proof of Concept

```bash
# Start a node without block_assembler in ckb.toml (default config)
ckb run

# Call get_overview — fails on every call (cache never warms)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_overview","params":[null],"id":1}'

# Expected (broken) response:
# {"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"CKBInternalError ..."}}
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

**File:** rpc/src/module/terminal.rs (L590-600)
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
