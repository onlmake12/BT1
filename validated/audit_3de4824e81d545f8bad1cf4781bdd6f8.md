### Title
Unauthenticated RPC Callers Can Reset the Entire Transaction Pool via `clear_tx_pool` and `clear_tx_verify_queue` — (`rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` RPC methods in `rpc/src/module/pool.rs` perform destructive, irreversible state resets on the node's transaction pool with zero access control. Any process that can reach the RPC port — including any local process on the same host, or any remote client if the operator has exposed the RPC — can silently wipe all pending transactions and the entire verification queue. There is no authentication token, caller identity check, or privilege gate of any kind on these methods.

---

### Finding Description

**Root cause — no access control on destructive RPC methods:**

`PoolRpcImpl::clear_tx_pool` and `PoolRpcImpl::clear_tx_verify_queue` are implemented as plain, unconditional calls with no caller verification:

```rust
// rpc/src/module/pool.rs
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

These delegate directly to `TxPoolController::clear_pool` and `clear_verify_queue`, which in turn call `TxPool::clear` — wiping every pending and proposed transaction and resetting the block assembler:

```rust
// tx-pool/src/process.rs
pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.clear(Arc::clone(&new_snapshot));
    // reset block_assembler
    ...
}
``` [2](#0-1) 

The RPC server has **no authentication layer**. The only protection is the default bind address of `127.0.0.1:8114`:

```toml
# resource/ckb.toml
listen_address = "127.0.0.1:8114"
``` [3](#0-2) 

The documentation itself warns that exposing this port is dangerous but provides no mechanism to restrict which callers can invoke which methods:

> "Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged." [4](#0-3) 

There is no API token, session credential, or per-method privilege check anywhere in the RPC stack. The `Pool` module is enabled by default in the standard node configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [5](#0-4) 

**Analogy to the external report:**

| External Report (`multiSign.sol`) | CKB (`rpc/src/module/pool.rs`) |
|---|---|
| `executeSetterFunction()` callable by anyone | `clear_tx_pool()` / `clear_tx_verify_queue()` callable by any RPC client |
| Resets admin approval state | Wipes entire tx pool and verify queue |
| Blocks admins from executing setters | Blocks users' transactions from being processed or mined |

---

### Impact Explanation

An attacker who can reach the RPC port can:

1. **Continuously wipe the tx pool** — all pending user transactions are silently discarded. Users must resubmit, and the attacker can loop the call to prevent any transaction from ever being mined.
2. **Wipe the verify queue** — transactions currently undergoing script verification are dropped, wasting the CPU cycles already spent and forcing re-entry.
3. **Disrupt block assembly** — `clear_pool` also sends a `BlockAssemblerMessage::Reset`, resetting the block assembler's state, which can interfere with the miner's ability to produce blocks. [6](#0-5) 

The impact is **sustained transaction-pool DoS**: any user who submits a transaction to a targeted node will have it silently dropped before it can be relayed or mined.

---

### Likelihood Explanation

**Realistic attacker paths:**

1. **Same-host process** (always reachable): Any unprivileged process running on the same machine as the CKB node can call `http://127.0.0.1:8114` with no credentials. This includes malicious scripts, compromised co-located services, or any user-level process on a shared host.

2. **Exposed RPC** (common in practice): Many node operators, exchanges, and dApp backends expose the RPC port to their internal network or the internet for convenience. The documentation warns against this but provides no enforcement. Once exposed, any remote attacker can call these methods with a single HTTP POST.

The attack requires no cryptographic material, no privileged keys, and no special protocol knowledge — just a standard JSON-RPC call.

---

### Recommendation

Implement per-method access control on the RPC server. Concretely:

- Introduce an **API token / bearer token** mechanism: the node operator configures a secret token in `ckb.toml`; the RPC server rejects requests that do not present it in the `Authorization` header.
- Mark destructive methods (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) as **operator-only** and enforce the token check before dispatching them.
- Alternatively, split the RPC into a **read-only public port** and a **privileged management port** (similar to how Ethereum clients separate `--http` from `--authrpc`), so destructive methods are never reachable on the public-facing socket.

---

### Proof of Concept

**Preconditions:** CKB node running with default config (`127.0.0.1:8114`), `Pool` module enabled (default). Attacker has shell access to the same host (or the operator has exposed the RPC port).

**Step 1 — User submits a transaction:**
```bash
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[{...}],"id":1}'
# Returns: {"result": "0xabc...txhash..."}
```

**Step 2 — Attacker (any local process, no credentials) wipes the pool:**
```bash
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"result": null}
```

**Step 3 — Verify the transaction is gone:**
```bash
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":3}'
# Returns: {"result": {"pending": {}, "proposed": {}}}
```

The user's transaction has been silently discarded. The attacker can loop Step 2 at any frequency to prevent any transaction from ever accumulating in the pool. The `clear_tx_verify_queue` call can be interleaved to also drop transactions mid-verification. [7](#0-6) [8](#0-7)

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

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/README.md (L5-5)
```markdown
Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.
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
