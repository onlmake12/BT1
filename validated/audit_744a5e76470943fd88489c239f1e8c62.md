### Title
Unauthenticated `clear_tx_pool` RPC Allows Any Local Caller to Silently Evict All Pending Transactions — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` RPC methods in the `Pool` module carry no authentication or authorization check. Any process that can reach the RPC endpoint — including any unprivileged local process on the same host — can call them at will, instantly removing every pending transaction from the pool and resetting the block-assembler state. This is the direct CKB analog of the `PrepareForProfitShare` pattern: a privileged-reset function that zeroes out accumulated state for all participants without their knowledge or consent.

---

### Finding Description

`clear_tx_pool` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl` with no caller-identity check:

```rust
// rpc/src/module/pool.rs  lines 322-323
#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
```

```rust
// rpc/src/module/pool.rs  lines 684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [1](#0-0) 

The underlying service handler in `TxPoolService` calls `tx_pool.clear(…)` on the live pool and then sends `BlockAssemblerMessage::Reset` to the block assembler:

```rust
// tx-pool/src/process.rs  lines 916-930
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
``` [2](#0-1) 

The `Pool` module is **enabled by default** in the production configuration:

```toml
# resource/ckb.toml  line 190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [3](#0-2) 

The module-enable check in `ServiceBuilder` is purely binary (module present in list or not); there is no per-method credential, token, or capability check:

```rust
// rpc/src/service_builder.rs  lines 36-46
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        let mut meta_io = MetaIoHandler::default();
        $add_methods(&mut meta_io, $methods);
        if $self.config.$check() {
            $self.add_methods(meta_io);
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
        $self
    }};
}
``` [4](#0-3) 

The same pattern applies to `clear_tx_verify_queue`, which drops every transaction waiting in the verification pipeline:

```rust
// rpc/src/module/pool.rs  lines 349-350
#[rpc(name = "clear_tx_verify_queue")]
fn clear_tx_verify_queue(&self) -> Result<()>;
``` [5](#0-4) 

```rust
// tx-pool/src/service.rs  lines 981-986
Message::ClearVerifyQueue(Request { responder, .. }) => {
    service.verify_queue.write().await.clear();
    ...
}
``` [6](#0-5) 

---

### Impact Explanation

A single unauthenticated JSON-RPC call:

```json
{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}
```

1. **Evicts every pending and proposed transaction** from the live pool — all work done by every transaction submitter on that node is discarded simultaneously.
2. **Resets the block assembler** (`BlockAssemblerMessage::Reset`), forcing the miner template to be rebuilt from scratch and potentially causing the miner to miss a block window.
3. **Silently drops transactions in the verify queue** (via `clear_tx_verify_queue`), meaning transactions that were accepted by the node but not yet admitted to the pool are lost with no notification to the submitter.
4. **Time-sensitive transactions** (those using `since`-based absolute or relative time locks) that are evicted may become permanently unsubmittable if their valid window expires before the submitter notices and resubmits.

This is the direct analog of `PrepareForProfitShare`: one caller resets accumulated state for every other participant, reducing their "claimable" position (pending transactions awaiting confirmation) to zero.

---

### Likelihood Explanation

- The RPC binds to `127.0.0.1:8114` by default, so the attacker must be a local process on the same host. The CKB scope explicitly includes "supported local CLI/RPC user" as a valid attacker profile.
- No credential, token, or capability is required — the HTTP endpoint accepts the call from any local process unconditionally.
- The `Pool` module is on by default; operators do not need to take any special action to expose this surface.
- The attack is a single HTTP POST with an empty params array — trivially scriptable and repeatable in a tight loop to continuously drain the pool.

---

### Recommendation

1. **Require explicit operator opt-in** for destructive pool-management methods. Move `clear_tx_pool` and `clear_tx_verify_queue` to a separate, non-default module (e.g., `Admin` or `Debug`) that operators must consciously enable.
2. **Add a shared-secret or token-based authentication layer** for any RPC method that mutates global node state, consistent with the existing warning in `rpc/README.md` that unrestricted RPC access is dangerous.
3. **Rate-limit or disable** `clear_tx_pool` on production builds entirely, analogous to how `IntegrationTest` methods (`truncate`, `process_block_without_verify`) are kept out of the default module list.

---

### Proof of Concept

```bash
# On the same host as a running CKB node (default config):
# 1. Submit a transaction
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# => pending: "0x1" (or more)

# 2. Any local process calls clear_tx_pool — no credentials needed
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# => {"result":null}

# 3. Pool is now empty — all submitters' transactions are gone
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# => pending: "0x0", proposed: "0x0", orphan: "0x0"
```

The call path is: HTTP POST → `PoolRpcImpl::clear_tx_pool` (`rpc/src/module/pool.rs:684`) → `TxPoolController::clear_pool` (`tx-pool/src/service.rs:371`) → `TxPoolService::clear_pool` (`tx-pool/src/process.rs:916`) → `TxPool::clear` + `BlockAssemblerMessage::Reset`. [7](#0-6) [8](#0-7) [2](#0-1)

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

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/src/service_builder.rs (L36-46)
```rust
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        let mut meta_io = MetaIoHandler::default();
        $add_methods(&mut meta_io, $methods);
        if $self.config.$check() {
            $self.add_methods(meta_io);
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
        $self
    }};
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

**File:** tx-pool/src/service.rs (L981-986)
```rust
        Message::ClearVerifyQueue(Request { responder, .. }) => {
            service.verify_queue.write().await.clear();
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_verify_queue failed {:?}", e)
            };
        }
```
