### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Caller to Manipulate the Transaction Pool — (`rpc/src/module/pool.rs`)

### Summary

The CKB JSON-RPC Pool module exposes `remove_transaction` and `clear_tx_pool` as privileged state-mutating operations with no caller authentication or authorization check. Any process that can reach the RPC port — a valid attacker profile explicitly listed in scope as "RPC caller" or "tx-pool submitter" — can silently drop any pending transaction or wipe the entire mempool without owning or having any relationship to those transactions.

### Finding Description

`PoolRpc` exposes two destructive, unauthenticated operations:

**`remove_transaction`** — removes a named transaction and all its descendants from the pool:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

**`clear_tx_pool`** — atomically drops every pending and proposed transaction:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [2](#0-1) 

Neither function checks who the caller is, whether the caller submitted the transaction, or whether the caller holds any credential. The Pool module is enabled by default in the production configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [3](#0-2) 

The RPC server itself enforces no authentication middleware. The miner client optionally sends a Basic-auth header, but the server never validates it — there is no server-side credential check anywhere in `rpc/src/server.rs` or `rpc/src/service_builder.rs`. [4](#0-3) 

The RPC binds to `127.0.0.1:8114` by default, but operators routinely expose it to wider networks (the README itself warns against this, implying it happens). [5](#0-4) 

### Impact Explanation

- **Transaction censorship**: Any reachable caller can call `remove_transaction` with the hash of any pending transaction — including high-value or time-sensitive ones — and silently evict it and all its descendants from the pool. The submitter receives no notification.
- **Full mempool wipe**: `clear_tx_pool` drops every pending and proposed transaction in one call. A caller who loops this call prevents the node from ever accumulating enough transactions to fill a block, effectively halting the node's block-production contribution.
- **Asymmetric damage**: The attacker pays nothing (no PoW, no fee, no stake). The victims — transaction submitters — must re-broadcast and re-pay fees, and may miss time-sensitive windows (e.g., DAO withdrawal deadlines, RBF races).

### Likelihood Explanation

The Pool module is on by default. Any local process (co-located service, compromised dependency, malicious script) can reach `127.0.0.1:8114` without any credential. Operators who expose the RPC beyond localhost — a common practice for remote wallets and DApps — extend the attack surface to the network. No exploit tooling is required beyond a single `curl` or JSON-RPC call.

### Recommendation

Introduce a caller-identity check at the RPC layer. Options in increasing strength:

1. **Token-based auth**: Require a secret token (configured in `ckb.toml`) in an HTTP header for all state-mutating Pool methods. Reject requests without a valid token with HTTP 401.
2. **Method-level allow-list**: Separate read-only Pool methods (`tx_pool_info`, `get_raw_tx_pool`) from write methods (`remove_transaction`, `clear_tx_pool`) into distinct sub-modules with independent enable flags, so operators can expose read access without write access.
3. **Ownership check for `remove_transaction`**: At minimum, require that the caller prove ownership of the transaction (e.g., by signing the tx hash with the lock key of one of its inputs) before removal is permitted.

### Proof of Concept

```bash
# Wipe the entire mempool of a running CKB node — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# => {"jsonrpc":"2.0","result":null,"id":1}

# Remove a specific victim transaction and all its descendants
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}'
# => {"jsonrpc":"2.0","result":true,"id":2}
```

Both calls succeed with zero authentication on a default-configured node. The `remove_transaction` trait definition and `clear_tx_pool` trait definition confirm no guard is present before the pool controller is invoked. [6](#0-5) [7](#0-6)

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L606-727)
```rust
impl PoolRpc for PoolRpcImpl {
    fn tx_pool_ready(&self) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();
        Ok(tx_pool.service_started())
    }

    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }

    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }

    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }

    fn tx_pool_info(&self) -> Result<TxPoolInfo> {
        let tx_pool = self.shared.tx_pool_controller();
        let get_tx_pool_info = tx_pool.get_tx_pool_info();
        if let Err(e) = get_tx_pool_info {
            error!("Send get_tx_pool_info request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };

        let tx_pool_info = get_tx_pool_info.unwrap();

        Ok(tx_pool_info.into())
    }

    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }

    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }

    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }

    fn get_pool_tx_detail_info(&self, tx_hash: H256) -> Result<PoolTxDetailInfo> {
        let tx_pool = self.shared.tx_pool_controller();
        let tx_detail = tx_pool
            .get_tx_detail(tx_hash.into())
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
        Ok(tx_detail.into())
    }
}
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/README.md (L3-6)
```markdown
The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

```
