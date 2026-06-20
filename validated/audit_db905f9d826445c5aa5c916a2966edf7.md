### Title
Unauthenticated RPC Caller Can Inject Arbitrary Blocks With All Consensus Verification Disabled — (`rpc/src/module/test.rs`)

### Summary

The `process_block_without_verify` RPC method in the `IntegrationTestRpc` module accepts a full block from any caller and processes it with `Switch::DISABLE_ALL`, bypassing every consensus check (PoW, script verification, capacity, epoch, two-phase commit, DAO header, reward). There is no caller authentication or authorization check inside the function. The only gate is a module-level config flag (`IntegrationTest` in `modules`), which is a supported, documented configuration for testnet/devnet nodes. Any unprivileged RPC caller who can reach the JSON-RPC port on such a node can inject arbitrary invalid blocks and corrupt the chain state.

### Finding Description

`process_block_without_verify` is declared in `rpc/src/module/test.rs` as part of the `IntegrationTestRpc` trait and implemented in `IntegrationTestRpcImpl`:

```rust
fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
    let block: packed::Block = data.into();
    let block: Arc<BlockView> = Arc::new(block.into_view());
    let ret = self
        .chain
        .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
    ...
}
``` [1](#0-0) 

`Switch::DISABLE_ALL` is defined to disable every verifier:

```rust
const DISABLE_ALL = Self::DISABLE_EPOCH.bits() | Self::DISABLE_UNCLES.bits() |
    Self::DISABLE_TWO_PHASE_COMMIT.bits() | Self::DISABLE_DAOHEADER.bits() |
    Self::DISABLE_REWARD.bits() |
    Self::DISABLE_NON_CONTEXTUAL.bits() | Self::DISABLE_SCRIPT.bits() |
    Self::DISABLE_EXTENSION.bits();
``` [2](#0-1) 

The function contains **zero** caller identity checks. The only protection is the module-level config gate in `service_builder.rs`:

```rust
if self.config.integration_test_enable() {
    assert_eq!(shared.consensus().pow, Pow::Dummy, ...);
}
``` [3](#0-2) 

This assert only prevents enabling the module on a real-PoW chain at startup. On any Dummy-PoW testnet/devnet node with `IntegrationTest` in the `modules` list (a documented, supported configuration), the method is fully live and callable by any HTTP client with no authentication.

The `truncate` method in the same implementation has the identical problem — no access control, any caller can roll back the chain to any prior block: [4](#0-3) 

The `ChainController::blocking_process_block_with_switch` path that is invoked: [5](#0-4) 

### Impact Explanation

On any CKB testnet/devnet node with the `IntegrationTest` RPC module enabled and the RPC port reachable:

- An unprivileged attacker can inject blocks containing transactions that spend cells they do not own (no script/lock verification), create outputs with more capacity than inputs (no capacity check), violate epoch rules, or include invalid PoW — all accepted into the canonical chain.
- With `broadcast: true`, the injected invalid block is relayed to all connected peers via `RelayV3`, potentially propagating chain corruption across the test network.
- `truncate` allows rolling back the chain to any prior block, erasing committed transactions and resetting chain state.

The combined effect is complete, unauthenticated control over chain state on any node running this configuration.

### Likelihood Explanation

The `IntegrationTest` module is explicitly documented in the official RPC README and is the standard configuration for CKB integration test environments. The default RPC config binds to `127.0.0.1`, but testnet/devnet nodes are routinely exposed on broader interfaces for CI/CD pipelines, shared test infrastructure, and developer tooling. Any process or user that can reach the RPC port — including other processes on the same host, containers in the same network, or remote clients on exposed nodes — can exploit this with a single HTTP POST and no credentials. [6](#0-5) 

### Recommendation

Add an explicit caller authentication mechanism to `process_block_without_verify` and `truncate`. Options in increasing strength:

1. **Token-based auth**: Require a secret token (configured at node startup) to be passed as a parameter or HTTP header; reject calls that omit or mismatch it.
2. **IP allowlist**: Restrict the `IntegrationTest` module to a configurable set of trusted source IPs at the RPC server layer, separate from the general `listen_address` binding.
3. **Separate privileged port**: Bind the `IntegrationTest` module to a distinct, non-default port (e.g., localhost-only) that is not exposed alongside the standard RPC modules.

The root cause is the same as the referenced report: a function designed to be called only by a trusted internal caller (the test framework) has no mechanism to enforce that restriction at runtime.

### Proof of Concept

**Precondition**: A CKB node running with `Pow::Dummy` and `modules = [..., "IntegrationTest"]` in its config, with the RPC port reachable.

**Steps**:

1. Craft a block with an invalid transaction (e.g., spending a cell the attacker does not own, or creating outputs exceeding input capacity).
2. Send the following JSON-RPC call:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "process_block_without_verify",
  "params": [
    { "<crafted_invalid_block_json>": "..." },
    true
  ]
}
```

3. The node accepts the block, advances its tip to the invalid block, and (with `broadcast: true`) relays it to peers.
4. `get_tip_block_number` confirms the chain tip advanced to the injected block.
5. Transactions inside the block are now committed on-chain despite failing every consensus rule.

No credentials, keys, or privileged access are required beyond network reachability of the RPC port.

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

**File:** verification/traits/src/lib.rs (L47-51)
```rust
        const DISABLE_ALL               = Self::DISABLE_EPOCH.bits() | Self::DISABLE_UNCLES.bits() |
                                    Self::DISABLE_TWO_PHASE_COMMIT.bits() | Self::DISABLE_DAOHEADER.bits() |
                                    Self::DISABLE_REWARD.bits() |
                                    Self::DISABLE_NON_CONTEXTUAL.bits() | Self::DISABLE_SCRIPT.bits() |
                                    Self::DISABLE_EXTENSION.bits();
```

**File:** rpc/src/service_builder.rs (L151-158)
```rust
        if self.config.integration_test_enable() {
            // IntegrationTest only on Dummy PoW chain
            assert_eq!(
                shared.consensus().pow,
                Pow::Dummy,
                "Only run integration test on Dummy PoW chain"
            );
        }
```

**File:** chain/src/chain_controller.rs (L71-77)
```rust
    pub fn blocking_process_block_with_switch(
        &self,
        block: Arc<BlockView>,
        switch: Switch,
    ) -> VerifyResult {
        self.blocking_process_block_internal(block, Some(switch))
    }
```

**File:** rpc/README.md (L80-89)
```markdown
    * [Module Integration_test](#module-integration_test) [👉 OpenRPC spec](http://playground.open-rpc.org/?uiSchema[appBar][ui:title]=CKB-Integration_test&uiSchema[appBar][ui:splitView]=false&uiSchema[appBar][ui:examplesDropdown]=false&uiSchema[appBar][ui:logoUrl]=https://raw.githubusercontent.com/nervosnetwork/ckb-rpc-resources/develop/ckb-logo.jpg&schemaUrl=https://raw.githubusercontent.com/nervosnetwork/ckb-rpc-resources/develop/json/integration_test_rpc_doc.json)

        * [Method `process_block_without_verify`](#integration_test-process_block_without_verify)
        * [Method `truncate`](#integration_test-truncate)
        * [Method `generate_block`](#integration_test-generate_block)
        * [Method `generate_epochs`](#integration_test-generate_epochs)
        * [Method `notify_transaction`](#integration_test-notify_transaction)
        * [Method `generate_block_with_template`](#integration_test-generate_block_with_template)
        * [Method `calculate_dao_field`](#integration_test-calculate_dao_field)
        * [Method `send_test_transaction`](#integration_test-send_test_transaction)
```
