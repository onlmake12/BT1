### Title
Unauthenticated `process_block_without_verify` RPC Bypasses All Consensus Verification — (`rpc/src/module/test.rs`)

### Summary

The `IntegrationTestRpc` module exposes `process_block_without_verify`, `truncate`, `generate_block`, and related methods with no caller-identity check inside the handlers. These methods are documented as being for integration testing only, yet they are registered unconditionally in the node launcher and carry no authorization guard within the function body. Any RPC client that can reach the JSON-RPC port can call them, bypassing all block verification and directly mutating chain state.

### Finding Description

`process_block_without_verify` in `rpc/src/module/test.rs` calls `blocking_process_block_with_switch` with `Switch::DISABLE_ALL`, which disables every consensus check — PoW, transaction scripts, capacity rules, and more.

```rust
// rpc/src/module/test.rs line 606-627
fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
    let block: packed::Block = data.into();
    let block: Arc<BlockView> = Arc::new(block.into_view());
    let ret = self
        .chain
        .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
    if broadcast {
        ...
        self.network_controller.quick_broadcast_with_handle(...);
    }
    ...
}
```

There is no check of any kind on who the caller is — no token, no IP allowlist, no flag comparison. The function body is identical whether called by a legitimate test harness or by an arbitrary remote RPC client.

The same module also exposes `truncate` (rolls back the canonical chain to any block hash) and `generate_block` / `generate_epochs` (produces and commits blocks without miner PoW), all without any authorization guard.

In `util/launcher/src/lib.rs`, `enable_integration_test` is called unconditionally during node startup:

```rust
// util/launcher/src/lib.rs line 540-554
.enable_integration_test(
    shared.clone(),
    network_controller.clone(),
    chain_controller,
    ...
)
```

This means the handlers are always registered. Whether they are reachable depends solely on whether the RPC port is network-accessible — a configuration concern, not an enforcement in code.

### Impact Explanation

An attacker who can reach the RPC port (e.g., a misconfigured node, a cloud VM with an open port, or a node operator who followed the common pattern of binding to `0.0.0.0`) can:

1. **Insert arbitrary invalid blocks** — blocks with no valid PoW, double-spend transactions, or fabricated coinbase rewards are accepted and committed to the chain because `Switch::DISABLE_ALL` skips every verification step.
2. **Truncate the chain** — `truncate` rolls back the canonical tip to any stored block hash, erasing committed history and resetting the node's view of the ledger.
3. **Broadcast corrupted state** — with `broadcast: true`, the inserted block is relayed to peers via `RelayV3`, potentially propagating the invalid state.

The result is permanent, unrecoverable corruption of the node's chain state without any cryptographic key or privileged credential.

### Likelihood Explanation

The RPC README explicitly warns that binding the RPC port to arbitrary machines is "dangerous and strongly discouraged," which implicitly acknowledges that operators do expose it. Any operator who binds `rpc.listen_address` to a non-loopback address — a common misconfiguration — exposes these methods to the public internet. No leaked key, no Sybil attack, and no privileged access is required; a single HTTP POST suffices.

### Recommendation

Add an explicit authorization guard inside each sensitive `IntegrationTestRpc` handler. The minimal fix is to gate the entire module behind a compile-time feature flag (e.g., `#[cfg(feature = "integration-test")]`) so it is physically absent from production binaries, and additionally add a runtime config check at the top of each handler that returns an error unless a dedicated `integration_test_mode` flag is set in the node config. The `process_block_without_verify` handler in particular should never be reachable in a production build.

### Proof of Concept

1. Start a CKB node with `rpc.listen_address = "0.0.0.0:8114"` (or any non-loopback address).
2. Craft a block with an invalid PoW nonce and arbitrary transactions (e.g., a double-spend).
3. Send the following HTTP request:

```json
{
  "jsonrpc": "2.0",
  "method": "process_block_without_verify",
  "params": [{ "<crafted_block_fields>" }, true],
  "id": 1
}
```

4. The node commits the block to its chain without any verification and, with `broadcast: true`, relays it to connected peers.
5. Call `truncate` with any earlier block hash to roll back the chain:

```json
{
  "jsonrpc": "2.0",
  "method": "truncate",
  "params": ["0x<any_stored_block_hash>"],
  "id": 2
}
```

The node's canonical tip is now at the specified block, erasing all subsequent history.

---

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** util/launcher/src/lib.rs (L540-554)
```rust
            .enable_integration_test(
                shared.clone(),
                network_controller.clone(),
                chain_controller,
                rpc_config
                    .extra_well_known_lock_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
                rpc_config
                    .extra_well_known_type_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
            )
```
