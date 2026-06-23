### Title
Unauthenticated `remove_transaction` / `clear_tx_pool` RPCs Allow Any Caller to Evict Any Pending Transaction — (File: rpc/src/module/pool.rs)

### Summary
The `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` RPC endpoints in `rpc/src/module/pool.rs` perform no authorization check before mutating the tx pool. Any RPC caller who knows a transaction hash can silently evict a transaction submitted by a different user. The tx pool stores no record of which caller submitted each transaction, so per-submitter ownership cannot be enforced at removal time. This is the direct structural analog to `supplyERC721FromNToken`: a state-mutating function intended for a specific authorized party is callable by any unprivileged client because the system tracks no ownership metadata.

### Finding Description

`remove_transaction` (line 662) calls `tx_pool.remove_local_tx(tx_hash.into())` with zero check that the caller is the entity that originally submitted the transaction:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| { ... })
}
``` [1](#0-0) 

`clear_tx_pool` (line 684) wipes the entire pending pool in one call, also with no caller identity check:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [2](#0-1) 

`clear_tx_verify_queue` (line 694) does the same for the verification queue: [3](#0-2) 

All three are registered as standard JSON-RPC methods on the `PoolRpc` trait with no authentication middleware in the module: [4](#0-3) [5](#0-4) [6](#0-5) 

The tx pool itself stores no submitter identity alongside each entry. When `submit_local_tx` is called via `send_transaction`, no caller token is recorded, so there is no data available at removal time to enforce ownership: [7](#0-6) 

The root cause mirrors the ERC721 report exactly: the function is intended to be called by a specific authorized party (the original submitter or a privileged operator), but the system tracks no ownership metadata, so any caller can invoke it freely.

### Impact Explanation

- Any RPC caller can remove any pending transaction submitted by any other user of the same node.
- `clear_tx_pool` allows a single unauthenticated RPC call to wipe every pending transaction on the node, causing complete loss of all unconfirmed work for every user.
- Time-sensitive transactions — DAO withdrawals, HTLC claims, or Replace-By-Fee replacements — that are evicted may miss their validity window, causing permanent loss of funds or opportunity for the original submitter.
- In a shared-node environment (mining pool, hosted node service, or any node with RPC reachable by more than one local user), an attacker with RPC access can selectively grief specific users or competitors.

### Likelihood Explanation

- The RPC is bound to `127.0.0.1` by default, but "supported local CLI/RPC user" is an explicitly listed valid attacker profile per the scope rules.
- In multi-user deployments — shared nodes, mining pools, hosted RPC services — multiple parties have RPC access without being the node operator.
- The attack requires only knowledge of a transaction hash, which is publicly observable once the transaction is relayed to the P2P network via `TransactionsProcess`: [8](#0-7) 

### Recommendation

- Record the submitter identity (e.g., a session token, connection ID, or IP address) when a transaction is inserted via `submit_local_tx`, and require that `remove_transaction` callers prove they are the original submitter.
- Alternatively, restrict `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` to an authenticated operator role (e.g., a separate privileged RPC port or a bearer token), consistent with how other node management operations are protected.
- At minimum, document prominently that these endpoints are privileged operations and must not be exposed to untrusted callers.

### Proof of Concept

1. User A calls `send_transaction` and receives transaction hash `0xabc…`. The transaction enters the pending pool and is relayed to peers, making the hash publicly observable.
2. Attacker B (any RPC caller on the same node — e.g., another user of a shared mining-pool node) calls `remove_transaction("0xabc…")`.
3. The pool silently removes the transaction and returns `true`. User A receives no notification.
4. If the transaction was a time-sensitive DAO withdrawal or HTLC claim, User A misses the validity window and loses funds.
5. For maximum impact, Attacker B calls `clear_tx_pool()` in a single RPC call, wiping every pending transaction on the node simultaneously — the direct analog to the unauthorized mass-minting scenario in the ERC721 report.

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

**File:** rpc/src/module/pool.rs (L349-350)
```rust
    #[rpc(name = "clear_tx_verify_queue")]
    fn clear_tx_verify_queue(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L612-635)
```rust
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

**File:** rpc/src/module/pool.rs (L694-700)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
```

**File:** sync/src/relayer/transactions_process.rs (L37-57)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };
```
