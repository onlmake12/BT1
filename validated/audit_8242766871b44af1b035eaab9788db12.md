### Title
Unauthenticated `clear_tx_pool` and `clear_tx_verify_queue` RPC Methods Allow Any Local Caller to Wipe the Mempool and Trigger Spurious Rejection Notifications - (File: rpc/src/module/pool.rs)

---

### Summary

The `Pool` RPC module exposes `clear_tx_pool` and `clear_tx_verify_queue` with no authentication or caller-identity check. Any process that can reach the RPC port — including any local user, co-located service, or malicious software on the same host — can invoke these methods to atomically destroy the entire pending transaction pool and/or verification queue, triggering spurious `reject_transaction` notifications to all active subscribers.

---

### Finding Description

`clear_tx_pool` and `clear_tx_verify_queue` are registered as standard `Pool` module RPC methods. The `Pool` module is enabled by default in `ckb.toml`.

```
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

Neither method performs any authentication, rate-limiting, or caller-identity check before executing:

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

The `Pool` module is mounted unconditionally when `pool_enable()` returns true, with no secondary access-control layer: [2](#0-1) 

The underlying `TxPoolController::clear_pool` and `clear_verify_queue` dispatch directly to the tx-pool service actor with no privilege check: [3](#0-2) 

The service actor executes the clear unconditionally on receipt: [4](#0-3) 

The `NotifyController` is wired to emit `reject_transaction` events for every transaction evicted from the pool. Any WebSocket/TCP subscriber listening on the `rejected_transaction` topic will receive a flood of spurious rejection notifications for every transaction that was in the pool at the time of the call — events that downstream systems (wallets, dApps, monitoring tools) may act upon as if those transactions were organically rejected by consensus rules. [5](#0-4) 

---

### Impact Explanation

1. **Mempool wipe**: A single unauthenticated HTTP POST to `127.0.0.1:8114` destroys all pending and proposed transactions. Miners lose their assembled block candidates; users' submitted transactions are silently dropped and must be resubmitted.

2. **Spurious rejection event flood**: Every evicted transaction causes a `reject_transaction` notification to be pushed to all active subscribers. Downstream systems that treat these events as authoritative (e.g., wallets marking transactions as "rejected", monitoring dashboards, Layer-2 bridges) will misinterpret the mass-eviction as organic consensus rejection, potentially causing incorrect state transitions in those systems.

3. **Verification queue disruption**: `clear_tx_verify_queue` silently discards all transactions currently being verified. These transactions are not re-queued and are not notified as rejected; they simply vanish, creating an inconsistency between what the submitter believes is pending and what the node actually holds.

---

### Likelihood Explanation

The RPC server binds to `127.0.0.1:8114` by default, which limits the attack surface to processes on the same host. However:

- Many production deployments proxy or expose the RPC to broader networks for dApp/wallet integration.
- Any co-located process (another service, a compromised dependency, a malicious script) can reach localhost without any credential.
- The CKB RPC has no authentication layer whatsoever — there is no token, API key, or session mechanism. The only "protection" is network-level binding.
- The attack requires a single HTTP request with no prior knowledge beyond the port number. [6](#0-5) 

---

### Recommendation

Add an authentication or authorization layer to destructive Pool RPC methods (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`). Options include:

- Require a configurable secret token in the `Authorization` HTTP header for write-class RPC methods.
- Introduce a separate `admin` RPC listen address (e.g., a Unix domain socket) for destructive operations, distinct from the general-purpose RPC port.
- At minimum, document these methods as operator-only and gate them behind a separate opt-in module flag (analogous to how `IntegrationTest` is a separate module), so they are not silently enabled in default deployments.

---

### Proof of Concept

With a default CKB node running (`ckb run`), any process on the same host can execute:

```bash
# Wipe the entire mempool — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'

# Wipe the verification queue — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_verify_queue","params":[],"id":2}'
```

Both calls return `{"result":null}` immediately. All pending transactions are destroyed. Any WebSocket subscriber on the `rejected_transaction` topic receives a notification for every evicted transaction, with no indication that the rejection was operator-initiated rather than consensus-driven. [7](#0-6) [8](#0-7)

### Citations

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L349-350)
```rust
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

**File:** tx-pool/src/service.rs (L370-378)
```rust
    /// Clears the tx-pool, removing all txs, update snapshot.
    pub fn clear_pool(&self, new_snapshot: Arc<Snapshot>) -> Result<(), AnyError> {
        send_message!(self, ClearPool, new_snapshot)
    }

    /// Clears the tx-verify-queue.
    pub fn clear_verify_queue(&self) -> Result<(), AnyError> {
        send_message!(self, ClearVerifyQueue, ())
    }
```

**File:** tx-pool/src/service.rs (L972-986)
```rust
        Message::ClearPool(Request {
            responder,
            arguments: new_snapshot,
        }) => {
            service.clear_pool(new_snapshot).await;
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_pool failed {:?}", e)
            };
        }
        Message::ClearVerifyQueue(Request { responder, .. }) => {
            service.verify_queue.write().await.clear();
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_verify_queue failed {:?}", e)
            };
        }
```

**File:** notify/src/lib.rs (L548-556)
```rust
    /// Notifies all subscribers of a rejected transaction.
    pub fn notify_reject_transaction(&self, tx_entry: PoolTransactionEntry, reject: Reject) {
        let reject_transaction_notifier = self.reject_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = reject_transaction_notifier.send((tx_entry, reject)).await {
                error!("notify_reject_transaction channel is closed: {}", e);
            }
        });
    }
```

**File:** resource/ckb.toml (L177-193)
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
```
