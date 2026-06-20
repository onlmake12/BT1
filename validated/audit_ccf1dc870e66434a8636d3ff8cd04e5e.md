### Title
Missing Access Control on `remove_transaction` Allows Any RPC Caller to Grief Transaction Submitters — (`File: rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method (and the related `clear_tx_pool` / `clear_tx_verify_queue` methods) in the Pool RPC module are destructive node-management operations that carry no caller authentication or authorization check. The CKB RPC server itself implements no per-method access control and no HTTP authentication middleware. Any process that can reach the RPC port — including any local user on the same machine, or any remote caller if the operator has exposed the port — can silently evict any pending transaction (and all its dependents) from the mempool, repeatedly and without limit.

### Finding Description

**Root cause — no authentication on the RPC server**

`rpc/src/server.rs` builds an Axum router and starts the HTTP/WS/TCP listeners with no authentication middleware of any kind. [1](#0-0) 

`rpc/src/service_builder.rs` mounts every enabled module's methods directly into the `IoHandler` with no per-method guard. [2](#0-1) 

A grep for `authorization`, `auth`, `token`, `password`, or `credential` inside `rpc/src/**/*.rs` returns zero matches, confirming the server never inspects the HTTP `Authorization` header. (The miner *client* sends Basic Auth credentials, but the server never validates them.) [3](#0-2) 

**Root cause — `remove_transaction` has no caller check**

The Pool module's `remove_transaction` implementation directly forwards the call to the tx-pool controller with no identity or permission check:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [4](#0-3) 

`clear_tx_pool` and `clear_tx_verify_queue` share the same absence of access control: [5](#0-4) 

**The Pool module is enabled by default in production**

The default production config includes `"Pool"` in the enabled modules list, making `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` reachable on every standard node: [6](#0-5) 

### Impact Explanation

An attacker with access to the RPC port (any local process by default; any remote caller if the operator has exposed the port, which is common for mining pools and dApp backends) can:

1. **Targeted griefing**: Watch `get_raw_tx_pool`, identify a victim's transaction hash, and immediately call `remove_transaction` with that hash. Because `remove_local_tx` also removes all descendant transactions, a single call can evict an entire dependency chain. The attacker can repeat this loop indefinitely, preventing the victim's transaction from ever being included in a block.
2. **Wholesale mempool wipe**: Call `clear_tx_pool` to atomically evict every pending transaction from every user, disrupting the entire node's transaction processing.
3. **Verify-queue drain**: Call `clear_tx_verify_queue` to silently drop all transactions currently awaiting script verification, causing them to disappear without error feedback to submitters.

The impact is permanent denial of transaction confirmation for targeted users for as long as the attacker maintains access to the port.

### Likelihood Explanation

- The RPC port defaults to `127.0.0.1:8114`. Any process running on the same host (another user, a compromised dependency, a co-located service) qualifies as a valid attacker under the "supported local CLI/RPC user" profile.
- Many production deployments — mining pools, dApp nodes, exchange nodes — deliberately expose the RPC port to internal networks or the public internet. The documentation warns against this but provides no enforcement mechanism.
- No special knowledge is required: the attacker only needs to know the tx hash (observable from `get_raw_tx_pool` or from watching the P2P relay network) and the RPC endpoint.
- The attack is trivially scriptable and requires no cryptographic material.

### Recommendation

1. **Add HTTP Basic Authentication to the RPC server.** Introduce an optional `rpc.username` / `rpc.password` (or token) config field. In `rpc/src/server.rs`, add an Axum middleware layer that rejects requests lacking valid credentials before they reach any handler.

2. **Separate destructive methods into a restricted module.** Move `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` into a new `Admin` module (similar to how `IntegrationTest` is gated). Require explicit opt-in and, ideally, a separate listen address bound only to loopback.

3. **Per-method capability flags.** Allow operators to enable read-only access for untrusted callers while restricting write/destructive methods to authenticated callers.

### Proof of Concept

```bash
# Step 1: Victim submits a transaction
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<TX_DATA>,"passthrough"]}'
# Returns: {"result": "0xABCD..."}  <-- tx_hash

# Step 2: Attacker polls the mempool and removes the transaction immediately
TX_HASH="0xABCD..."
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d "{\"id\":2,\"jsonrpc\":\"2.0\",\"method\":\"remove_transaction\",\"params\":[\"$TX_HASH\"]}"
# Returns: {"result": true}  -- transaction and all dependents silently evicted

# Step 3: Attacker loops steps 1-2 to permanently deny confirmation
# Victim's transaction never appears in any block.

# Nuclear variant: wipe the entire mempool
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"result": null}  -- all pending transactions from all users evicted
```

The attack requires no credentials, no special protocol knowledge, and no on-chain assets. It is directly analogous to the `SDLVesting::stakeReleasableTokens` griefing vector: a function intended for a trusted operator role (node administrator) carries no access control, allowing any reachable caller to trigger a destructive state change that harms other users.

### Citations

**File:** rpc/src/server.rs (L52-68)
```rust
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();
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

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
