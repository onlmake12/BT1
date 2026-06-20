### Title
Unauthenticated `process_block_without_verify` with `Switch::DISABLE_ALL` Allows Any RPC Caller to Inject Unverified Blocks and Broadcast Them to P2P Peers — (`rpc/src/module/test.rs`)

---

### Summary

The `IntegrationTestRpc` module exposes `process_block_without_verify`, which processes an attacker-supplied block with **all consensus verification disabled** (`Switch::DISABLE_ALL`) and, when `broadcast: true`, immediately relays the unverified block to every connected P2P peer. The RPC layer has no per-caller authentication; the only gate is a static config flag (`integration_test_enable`). Any RPC client that can reach the endpoint when the module is enabled can exploit this to corrupt local chain state and propagate invalid blocks across the network.

---

### Finding Description

`process_block_without_verify` is defined in `rpc/src/module/test.rs` and implemented in `IntegrationTestRpcImpl`: [1](#0-0) 

The implementation calls `blocking_process_block_with_switch` with `Switch::DISABLE_ALL`, which disables every verification step (PoW, script execution, capacity checks, epoch rules, etc.). When `broadcast` is `true`, the resulting compact block is immediately pushed to all peers via `quick_broadcast_with_handle`. [2](#0-1) 

The only access control is the config flag checked in `ServiceBuilder::enable_integration_test`: [3](#0-2) 

The `assert_eq!(shared.consensus().pow, Pow::Dummy, ...)` guard at line 153–157 fires only when `integration_test_enable()` is already `true`; it prevents the module from starting on a real PoW chain, but it does **not** add any per-request authentication. Once the module is enabled, every caller — authenticated or not — has identical, unrestricted access to all methods including `truncate` (chain rollback) and `notify_transaction` (tx-pool bypass).

The same module also exposes `truncate`, which rolls back the canonical chain to any historical block and clears the tx-pool: [4](#0-3) 

The RPC server itself applies `CorsLayer::permissive()`, meaning cross-origin requests are accepted from any web origin: [5](#0-4) 

This mirrors the FujiERC1155 pattern exactly: a single coarse flag (`onlyPermit` / `integration_test_enable`) grants identical, unrestricted access to both low-risk read methods (`calculate_dao_field`) and high-risk state-mutating methods (`process_block_without_verify`, `truncate`), with no role distinction between callers.

---

### Impact Explanation

When `IntegrationTestRpc` is enabled:

1. **Chain corruption**: Any RPC caller submits a crafted block (e.g., with invalid scripts, wrong capacity, or a fabricated coinbase) via `process_block_without_verify`. `Switch::DISABLE_ALL` ensures it is accepted unconditionally and written to the database.
2. **Network propagation**: With `broadcast: true`, the invalid compact block is relayed to all connected peers via the RelayV3 protocol. Peers that have not disabled verification will reject it, but the originating node's chain state is already corrupted.
3. **Chain rollback**: `truncate` can rewind the chain to any ancestor block, erasing confirmed transactions and resetting the tx-pool — a complete denial of service for the node and any service depending on it.
4. **Tx-pool bypass**: `notify_transaction` inserts transactions into the pool via `notify_txs`, bypassing the normal `submit_tx` admission pipeline (fee-rate checks, ancestor limits, etc.).

---

### Likelihood Explanation

The `IntegrationTest` module is explicitly listed in the default `integration` config profile: [6](#0-5) 

Any operator running a node with `integration` config (common in CI, staging, or testnet environments) and with the RPC port reachable exposes these methods to every HTTP/TCP/WebSocket client. The `CorsLayer::permissive()` setting further means a malicious web page can trigger these calls from a victim's browser if the RPC is reachable from the victim's machine. No credentials, keys, or privileged access are required beyond network reachability.

---

### Recommendation

1. **Add per-method authentication**: Introduce an API-key or HTTP Basic Auth layer for the `IntegrationTest` module methods, checked at request time, not only at startup.
2. **Segregate duties within the module**: Separate the `broadcast` capability from block processing. Remove the `broadcast` parameter from `process_block_without_verify`; if network announcement is needed, require a separate, explicitly authenticated call.
3. **Bind IntegrationTest to loopback only**: Regardless of the global `listen_address`, force the IntegrationTest-capable endpoint to bind exclusively to `127.0.0.1`.
4. **Replace `CorsLayer::permissive()`**: Use a restrictive CORS policy (e.g., same-origin only) to prevent cross-origin RPC calls from browser contexts.

---

### Proof of Concept

**Precondition**: Node started with `integration` module list and RPC reachable (e.g., `listen_address = "0.0.0.0:8114"`).

```bash
# Step 1: Craft an arbitrary block (e.g., copy genesis block with modified fields)
BLOCK_DATA='{ "header": { ... }, "transactions": [...], "proposals": [], "uncles": [] }'

# Step 2: Submit with all verification disabled and broadcast to peers
curl -X POST http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "process_block_without_verify",
    "params": ['"$BLOCK_DATA"', true],
    "id": 1
  }'

# Expected result: {"result": "<block_hash>", ...}
# The invalid block is now in the node's chain and has been broadcast to all peers.

# Step 3: Roll back the chain to genesis
curl -X POST http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "truncate",
    "params": ["<genesis_hash>"],
    "id": 2
  }'
# Expected result: {"result": null, ...}
# Chain is now at genesis; all confirmed transactions are erased.
```

No authentication token, key, or privileged session is required. Any HTTP client reachable to the RPC port can execute both steps.

### Citations

**File:** rpc/src/module/test.rs (L606-627)
```rust
    fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
        let block: packed::Block = data.into();
        let block: Arc<BlockView> = Arc::new(block.into_view());
        let ret = self
            .chain
            .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
        if broadcast {
            let content = packed::CompactBlock::build_from_block(&block, &HashSet::new());
            let message = packed::RelayMessage::new_builder().set(content).build();
            self.network_controller.quick_broadcast_with_handle(
                SupportProtocols::RelayV3.protocol_id(),
                message.as_bytes(),
                self.shared.async_handle(),
            );
        }
        if ret.is_ok() {
            Ok(Some(block.hash().into()))
        } else {
            error!("process_block_without_verify error: {:?}", ret);
            Ok(None)
        }
    }
```

**File:** rpc/src/module/test.rs (L629-659)
```rust
    fn truncate(&self, target_tip_hash: H256) -> Result<()> {
        let header = {
            let snapshot = self.shared.snapshot();
            let header = snapshot
                .get_block_header(&target_tip_hash.into())
                .ok_or_else(|| {
                    RPCError::custom(RPCError::Invalid, "block not found".to_string())
                })?;
            if !snapshot.is_main_chain(&header.hash()) {
                return Err(RPCError::custom(
                    RPCError::Invalid,
                    "block not on main chain".to_string(),
                ));
            }
            header
        };

        // Truncate the chain and database
        self.chain
            .truncate(header.hash())
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        // Clear the tx_pool
        let new_snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(new_snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/service_builder.rs (L151-173)
```rust
        if self.config.integration_test_enable() {
            // IntegrationTest only on Dummy PoW chain
            assert_eq!(
                shared.consensus().pow,
                Pow::Dummy,
                "Only run integration test on Dummy PoW chain"
            );
        }
        let methods = IntegrationTestRpcImpl {
            shared,
            network_controller,
            chain,
            well_known_lock_scripts,
            well_known_type_scripts,
        };
        set_rpc_module_methods!(
            self,
            "IntegrationTest",
            integration_test_enable,
            add_integration_test_rpc_methods,
            methods
        )
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
