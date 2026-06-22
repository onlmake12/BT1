### Title
Any RPC Caller Can Remove Another User's Pending Transaction Without Authorization — (`rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method in `rpc/src/module/pool.rs` accepts only a `tx_hash` and removes the matching transaction (plus all descendants) from the tx pool with no check that the caller is the original submitter. Any local or network-reachable RPC caller can silently evict another user's pending or proposed transaction. This is a direct structural analog to the `repayLoan` bug: a state-changing operation that should be restricted to the resource owner is callable by anyone, and it acts on the victim's resource without their consent.

### Finding Description

`remove_transaction` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
// rpc/src/module/pool.rs, line 254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;

// rpc/src/module/pool.rs, line 662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The implementation forwards directly to `TxPoolController::remove_local_tx`, which calls `TxPool::remove_tx` → `PoolMap::remove_entry_and_descendants`. There is no identity check, no ownership record, and no proof that the caller submitted the transaction. The RPC server itself (`rpc/src/server.rs`) has no authentication middleware — it uses `CorsLayer::permissive()` and accepts any HTTP POST to the configured address. The Pool module is enabled by default in `resource/ckb.toml` (`modules = ["Net", "Pool", "Miner", ...]`).

**Attack path:**
1. Victim submits a transaction via `send_transaction`; it enters the pending or proposed pool.
2. Attacker (any process with RPC access) calls `remove_transaction` with the victim's `tx_hash`.
3. The pool removes the transaction and all its descendants immediately, with no notification to the original submitter.
4. Attacker can repeat this every time the victim resubmits, indefinitely.

The tx hash is public: it is returned by `send_transaction`, visible in `get_raw_tx_pool`, and broadcast over the P2P network.

### Impact Explanation

- **Unauthorized state change**: Any RPC caller can evict any other user's pending or proposed transaction from the pool without their consent.
- **Financial harm for time-sensitive transactions**: CKB DAO withdrawals require the withdrawal transaction to be committed within a specific epoch window. If an attacker repeatedly removes the victim's withdrawal transaction each time it is resubmitted, the victim can miss the epoch window entirely, forfeiting the DAO interest for that cycle (a concrete, quantifiable loss).
- **Cascading removal**: `remove_entry_and_descendants` removes the target transaction and all dependent child transactions, so a single call can evict an entire transaction chain built by the victim.
- **No recourse**: The victim has no way to distinguish a legitimate pool eviction (e.g., pool size limit) from a targeted attack.

### Likelihood Explanation

Medium. The RPC is localhost-bound by default, but:
- Many operators expose the RPC to external networks (the documentation warns against this but does not enforce it).
- Any co-located process (wallet software, dApp backend, malicious script) on the same host can reach the RPC without any credential.
- The Pool module is enabled by default; no opt-in is required.
- The tx hash needed to target a specific transaction is publicly observable from `get_raw_tx_pool` or P2P relay.

### Recommendation

Add caller-identity tracking to `remove_transaction`. The simplest fix is to record the submitter's identity (e.g., a session token or IP address) when a transaction is accepted via `submit_local_tx`, and reject `remove_transaction` calls that do not present a matching identity. Alternatively, restrict `remove_transaction` to a separate, authenticated administrative RPC module (analogous to how `Debug` and `IntegrationTest` modules are gated), so it is not co-exposed with the general `Pool` module that end-users rely on.

### Proof of Concept

```
# Terminal 1 — victim submits a transaction
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<victim_tx>],"id":1}'
# Returns: {"result": "0xVICTIM_TX_HASH", ...}

# Terminal 2 — attacker (any local process) removes it immediately
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xVICTIM_TX_HASH"],"id":2}'
# Returns: {"result": true, ...}
# Victim's transaction and all descendants are gone from the pool.
# Attacker repeats on every resubmission.
```

The tx hash `0xVICTIM_TX_HASH` is obtained from `get_raw_tx_pool` (also a public, unauthenticated Pool RPC method). No privileged access is required. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
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
