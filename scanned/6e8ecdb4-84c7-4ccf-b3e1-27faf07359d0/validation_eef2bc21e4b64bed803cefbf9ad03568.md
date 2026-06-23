### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPCs Silently Evict Pool Transactions Without Emitting `rejected_transaction` Subscription Events — (File: `rpc/src/module/pool.rs`)

### Summary
The `remove_transaction` and `clear_tx_pool` RPC methods are publicly accessible in the default-enabled `Pool` module with no authentication. Any RPC caller can silently evict transactions from the pool. Unlike the normal rejection path — which fires `notify_reject_transaction` so that all `rejected_transaction` subscribers are notified — these two RPC methods bypass the notification system entirely. Off-chain services (wallets, exchanges, DApps) subscribed to the `rejected_transaction` topic receive no event, leaving them with permanently stale pending-transaction state that can be misinterpreted as a valid in-flight payment.

---

### Finding Description

**Normal rejection path (with notification):**

In `shared/src/shared_builder.rs`, the tx-pool reject callback is registered as:

```rust
tx_pool_builder.register_reject(Box::new(
    move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
        // ...
        notify_reject.notify_reject_transaction(notify_tx_entry, reject);
        // ...
    },
));
```

Every organic rejection (fee too low, double-spend, expiry, etc.) calls `notify_reject_transaction`, which fans out to all `rejected_transaction` subscribers via `NotifyController` and ultimately to every WebSocket/TCP client subscribed to that topic in `SubscriptionRpcImpl`.

**`remove_transaction` RPC path (no notification):**

`rpc/src/module/pool.rs` exposes:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(...)
}
```

This dispatches `Message::RemoveLocalTx` to the tx-pool service. The handler in `tx-pool/src/service.rs` calls `service.remove_tx(tx_hash).await`, implemented in `tx-pool/src/process.rs`:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    // removes from verify_queue, orphan pool, or main pool
    // NO call to notify_reject_transaction
}
```

No subscription event is emitted.

**`clear_tx_pool` RPC path (no notification):**

`rpc/src/module/pool.rs` exposes:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot).map_err(...)?;
    Ok(())
}
```

The handler calls `service.clear_pool(new_snapshot).await` in `tx-pool/src/process.rs`:

```rust
pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.clear(Arc::clone(&new_snapshot));
    // reset block_assembler
    // NO call to notify_reject_transaction for any evicted tx
}
```

Again, zero subscription events are emitted for any of the evicted transactions.

Both methods are in the `Pool` module, which is **enabled by default** in `resource/ckb.toml`:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

There is no per-method authentication in the RPC layer.

---

### Impact Explanation

Off-chain services that subscribe to `rejected_transaction` events (wallets, exchange hot-wallet monitors, DApp backends) rely on this topic to track the full lifecycle of a pending transaction. The expected lifecycle is:

1. `send_transaction` → `new_transaction` event emitted → service marks tx as pending.
2. Organic rejection → `rejected_transaction` event emitted → service marks tx as failed.

When an attacker (or any RPC caller) calls `remove_transaction` or `clear_tx_pool`, step 2 never fires. The off-chain service permanently believes the transaction is still pending. Concrete consequences:

- **Exchange crediting**: An exchange that credits a deposit only after seeing a `rejected_transaction` event (to cancel the credit) will never cancel it, enabling a double-spend-like scenario where the attacker submits a tx, gets credited, then has the tx silently removed before it confirms.
- **Wallet UX / stuck state**: Wallets show the transaction as perpetually pending, blocking the user from resubmitting.
- **DApp state machines**: Smart-contract interaction flows that gate on pending-tx state will stall indefinitely.

This is the direct CKB analog of the `voluntaryExit` visibility bug: a function that should be restricted to the node operator is publicly callable, and calling it causes off-chain services to misinterpret the pool state.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, but the documentation explicitly warns: *"Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged"* — acknowledging that operators routinely expose it. Any caller with RPC access (a co-located process, a misconfigured firewall, a compromised local service) can invoke `remove_transaction` or `clear_tx_pool` with no credentials. The `Pool` module is on by default; no opt-in is required.

---

### Recommendation

1. **Restrict destructive pool-management RPCs**: Move `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` to a separate, non-default module (e.g., `Admin`) that operators must explicitly enable, analogous to how `Debug` and `IntegrationTest` are opt-in.
2. **Emit rejection events on explicit removal**: When `remove_tx` or `clear_pool` evicts a transaction that was previously admitted to the pool, call `notify_reject_transaction` with an appropriate `Reject` reason (e.g., `Expiry` or a new `ExplicitlyRemoved` variant) so that all subscribers maintain consistent state.

---

### Proof of Concept

```
# 1. Open a WebSocket subscription to rejected_transaction
wscat -c ws://localhost:28114
> {"id":1,"jsonrpc":"2.0","method":"subscribe","params":["rejected_transaction"]}
< {"jsonrpc":"2.0","result":"0x0","id":1}

# 2. Submit a valid transaction — triggers new_transaction event
curl -X POST http://localhost:8114 -d '{"id":2,"jsonrpc":"2.0","method":"send_transaction","params":[<tx>]}'
# WebSocket subscriber sees new_transaction event for <tx_hash>

# 3. Silently remove the transaction via the public RPC
curl -X POST http://localhost:8114 -d '{"id":3,"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash>"]}'
< {"jsonrpc":"2.0","result":true,"id":3}

# 4. WebSocket subscriber receives NO rejected_transaction event.
#    Off-chain service still believes the transaction is pending.
#    get_transaction confirms the tx is gone from the pool with no trace.
```

The subscriber never receives a `rejected_transaction` notification, leaving any off-chain service in a permanently incorrect pending state.