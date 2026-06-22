### Title
Unauthenticated Destructive Pool RPC Methods Allow Any Caller to Clear or Censor Pending Transactions — (`File: rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC server exposes destructive pool-management methods (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) with **zero caller authentication or authorization**. Any process or user that can reach the RPC port can instantly destroy all pending transactions or surgically remove a specific transaction from the pool. The RPC server has no authentication middleware, no token check, and no per-method access control.

---

### Finding Description

The CKB RPC server (`rpc/src/server.rs`) starts HTTP, TCP, and WebSocket listeners with no authentication layer. The `Pool` module is enabled by default in production configuration. Within that module, three methods perform irreversible destructive operations with no caller identity check:

**`clear_tx_pool`** — removes every transaction from the pool:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [1](#0-0) 

**`clear_tx_verify_queue`** — removes every transaction from the verification queue:

```rust
fn clear_tx_verify_queue(&self) -> Result<()> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_verify_queue()
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [2](#0-1) 

**`remove_transaction`** — removes any specific transaction by hash, with no check that the caller submitted it:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [3](#0-2) 

The RPC server setup confirms there is no authentication middleware at any layer: [4](#0-3) 

The `Pool` module is enabled by default in the production config: [5](#0-4) 

The RPC config has no authentication field — no token, no IP allowlist, no per-method access control: [6](#0-5) 

The trait definitions confirm these methods carry no authorization parameter or guard: [7](#0-6) 

---

### Impact Explanation

**`clear_tx_pool` / `clear_tx_verify_queue`:** A single unauthenticated RPC call drops every pending and queued transaction on the node. Transactions submitted by users are silently lost and must be resubmitted. For a node serving as a public relay or dApp backend, this is a complete service disruption. The pool is reset to empty state with no record of what was dropped.

**`remove_transaction`:** An attacker who knows (or can enumerate) a target transaction hash can surgically remove it from the pool before it is mined. This enables targeted transaction censorship — e.g., preventing a specific user's withdrawal, liquidation, or time-sensitive operation from being confirmed. There is no ownership check: the caller does not need to be the original submitter.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, which limits exposure to local processes. However:

1. Many operators expose the RPC to the network for dApp integration — the documentation warns against this but does not prevent it.
2. Any malicious process running on the same host (compromised dependency, co-located service, SSRF in a local web service) can reach the localhost port.
3. The `Pool` module is enabled by default; no operator action is required to expose these methods.
4. The attack requires a single HTTP POST with no credentials — trivially scriptable.

The scope explicitly includes "RPC caller" and "supported local CLI/RPC user" as valid attacker profiles.

---

### Recommendation

Implement per-method authorization at the RPC layer. At minimum:

- Introduce a configurable `rpc_secret_token` in `ckb.toml`; require it as a Bearer token or HTTP header for all state-mutating methods.
- Classify methods as read-only vs. mutating and enforce the token only on mutating methods (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`, `set_ban`, `clear_banned_addresses`, `add_node`, `remove_node`, `set_network_active`).
- Alternatively, split the RPC into two listeners: a public read-only port and a privileged management port bound only to localhost with a token requirement.

---

### Proof of Concept

With the `Pool` module enabled (default), any caller with RPC access can destroy the entire pending transaction pool:

```bash
# Drop all pending transactions — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"id":1,"jsonrpc":"2.0","result":null}

# Remove a specific transaction by hash — no ownership check
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"remove_transaction","params":["0xTARGET_TX_HASH"]}'
# Returns: {"id":1,"jsonrpc":"2.0","result":true}
```

The `clear_tx_pool` call internally invokes `tx_pool.clear_pool(snapshot)` which calls `tx_pool.clear(Arc::clone(&new_snapshot))` — a full wipe with no reversibility: [8](#0-7)

### Citations

**File:** rpc/src/module/pool.rs (L322-350)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;

    /// Removes all transactions from the verification queue.
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "clear_tx_verify_queue",
    ///   "params": []
    /// }
    /// ```
    ///
    /// Response
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "result": null
    /// }
    /// ```
    #[rpc(name = "clear_tx_verify_queue")]
    fn clear_tx_verify_queue(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L662-669)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }
```

**File:** rpc/src/module/pool.rs (L684-692)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L694-701)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/server.rs (L52-95)
```rust
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();

        let ws_address = if let Some(addr) = config.ws_listen_address {
            let local_addr =
                Self::start_server(&rpc, addr, handler.clone(), true).inspect(|&addr| {
                    info!("Listen WebSocket RPCServer on address: {}", addr);
                });
            local_addr.ok()
        } else {
            None
        };

        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
            local_addr.ok()
        } else {
            None
        };

        Self {
            http_address,
            tcp_address,
            ws_address,
        }
    }
```

**File:** resource/ckb.toml (L177-208)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760

# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}

# By default RPC only binds to HTTP service, you can bind it to TCP and WebSocket.
# tcp_listen_address = "127.0.0.1:18114"
# ws_listen_address = "127.0.0.1:28114"
reject_ill_transactions = true

# By default deprecated rpc methods are disabled.
enable_deprecated_rpc = false # {{
# integration => enable_deprecated_rpc = true
# }}

# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** tx-pool/src/process.rs (L916-930)
```rust
    pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
        {
            let mut tx_pool = self.tx_pool.write().await;
            tx_pool.clear(Arc::clone(&new_snapshot));
        }
        // reset block_assembler
        if self
            .block_assembler_sender
            .send(BlockAssemblerMessage::Reset(new_snapshot))
            .await
            .is_err()
        {
            error!("block_assembler receiver dropped");
        }
    }
```
