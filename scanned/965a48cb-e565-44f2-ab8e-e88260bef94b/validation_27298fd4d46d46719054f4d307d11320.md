### Title
Missing Per-Request Authorization Guard on Privileged `IntegrationTestRpc` Methods Allows Any RPC Caller to Bypass All Consensus Verification — (`File: rpc/src/module/test.rs`)

---

### Summary

The `IntegrationTestRpc` module exposes `process_block_without_verify` and `truncate` as unauthenticated JSON-RPC endpoints. These methods perform destructive, privileged operations — injecting blocks with all verification disabled (`Switch::DISABLE_ALL`) and rolling back the canonical chain — with no per-request caller authorization guard. The only protection is a startup-time `assert_eq!(pow, Pow::Dummy)` that prevents the module from loading on mainnet, but once the module is enabled, any RPC caller (no credentials required) can invoke these methods unconditionally.

---

### Finding Description

**Root cause:** `process_block_without_verify` and `truncate` in `IntegrationTestRpcImpl` contain zero per-request authentication or authorization logic. The methods are registered as plain JSON-RPC handlers with no middleware guard, token check, or caller identity verification.

**Code path:**

`process_block_without_verify` calls `blocking_process_block_with_switch(block, Switch::DISABLE_ALL)`:

```rust
// rpc/src/module/test.rs:606-627
fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
    let block: packed::Block = data.into();
    let block: Arc<BlockView> = Arc::new(block.into_view());
    let ret = self
        .chain
        .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
    // ...
}
```

`Switch::DISABLE_ALL` is defined as disabling every verifier — epoch, uncles, two-phase commit, DAO header, reward, non-contextual, script, and extension:

```rust
// verification/traits/src/lib.rs:47-51
const DISABLE_ALL = Self::DISABLE_EPOCH.bits() | Self::DISABLE_UNCLES.bits() |
    Self::DISABLE_TWO_PHASE_COMMIT.bits() | Self::DISABLE_DAOHEADER.bits() |
    Self::DISABLE_REWARD.bits() |
    Self::DISABLE_NON_CONTEXTUAL.bits() | Self::DISABLE_SCRIPT.bits() |
    Self::DISABLE_EXTENSION.bits();
```

`truncate` rolls back the chain and clears the tx pool with no caller check:

```rust
// rpc/src/module/test.rs:629-659
fn truncate(&self, target_tip_hash: H256) -> Result<()> {
    // ... only checks block exists on main chain, no caller auth ...
    self.chain.truncate(header.hash())...;
    tx_pool.clear_pool(new_snapshot)...;
    Ok(())
}
```

The only guard is a startup-time `assert_eq!` in `service_builder.rs`:

```rust
// rpc/src/service_builder.rs:151-157
if self.config.integration_test_enable() {
    assert_eq!(
        shared.consensus().pow,
        Pow::Dummy,
        "Only run integration test on Dummy PoW chain"
    );
}
```

This assertion fires once at node startup. It does not restrict which callers can invoke the methods at runtime. The module enable check (`integration_test_enable()`) is also purely config-driven with no runtime identity check:

```rust
// util/app-config/src/configs/rpc.rs:99-102
pub fn integration_test_enable(&self) -> bool {
    self.modules.contains(&Module::IntegrationTest)
}
```

The integration test template config explicitly enables this module:

```toml
# test/template/ckb.toml:69
modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest"]
```

---

### Impact Explanation

Any process that can reach the RPC port on a node with `IntegrationTest` enabled can:

1. **Inject arbitrary blocks bypassing all consensus rules** via `process_block_without_verify`. With `Switch::DISABLE_ALL`, the injected block undergoes zero verification — no PoW check, no script execution, no capacity accounting, no transaction authorization. An attacker can craft a block containing transactions that spend cells they do not own (no lock script verification), create outputs with invalid capacity, or double-spend inputs.

2. **Roll back the canonical chain to any prior block** via `truncate`, erasing committed transactions and clearing the mempool. This destroys finality for all transactions committed after the target block.

Both operations directly corrupt chain state and consensus integrity. On a devnet or testnet running Dummy PoW with the `IntegrationTest` module enabled and the RPC port reachable (even only from localhost), any co-located process — a compromised dependency, a malicious script, or a lateral-movement attacker — can trigger these effects without any credential.

---

### Likelihood Explanation

The `IntegrationTest` module is explicitly documented and shipped for use in integration testing environments. The template config at `test/template/ckb.toml` enables it by default. Devnet and testnet deployments routinely use this configuration. The RPC server defaults to `127.0.0.1:8114`, so the attack surface is local by default, but:

- Any process running on the same host (e.g., a compromised CI runner, a malicious npm/cargo dependency in a test suite, a web app running locally) can reach `127.0.0.1:8114`.
- Operators who expose the RPC port beyond localhost (explicitly warned against but commonly done in dev environments) expose these methods to the network.
- The attacker needs no credentials, no keys, and no privileged access — only HTTP connectivity to the RPC port.

---

### Recommendation

Add a per-request authorization guard to `process_block_without_verify`, `truncate`, and all other `IntegrationTestRpc` methods. The guard should verify a shared secret token (configured at node startup) present in the request, analogous to the `onlyGelatoRelay` modifier in the original report. Alternatively, bind the `IntegrationTest` RPC methods to a separate listener address that is not shared with the general RPC server, so operators can restrict access at the network level without relying solely on the module-enable flag.

---

### Proof of Concept

**Preconditions:** Node running with Dummy PoW, `IntegrationTest` in `modules`, RPC on `127.0.0.1:8114`.

**Step 1 — Inject an invalid block with no verification:**
```bash
curl -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "process_block_without_verify",
    "params": [<crafted_block_with_unauthorized_spend>, false],
    "id": 1
  }'
```
The block is committed to the chain via `blocking_process_block_with_switch(..., Switch::DISABLE_ALL)` with no script, capacity, or PoW verification. The response returns the block hash, confirming acceptance.

**Step 2 — Roll back the chain:**
```bash
curl -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "truncate",
    "params": ["<any_prior_block_hash>"],
    "id": 2
  }'
```
The chain is truncated to the specified block and the tx pool is cleared. No authentication is required at any step. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rpc/src/service_builder.rs (L151-172)
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
```

**File:** verification/traits/src/lib.rs (L47-51)
```rust
        const DISABLE_ALL               = Self::DISABLE_EPOCH.bits() | Self::DISABLE_UNCLES.bits() |
                                    Self::DISABLE_TWO_PHASE_COMMIT.bits() | Self::DISABLE_DAOHEADER.bits() |
                                    Self::DISABLE_REWARD.bits() |
                                    Self::DISABLE_NON_CONTEXTUAL.bits() | Self::DISABLE_SCRIPT.bits() |
                                    Self::DISABLE_EXTENSION.bits();
```

**File:** chain/src/chain_controller.rs (L70-77)
```rust
    /// `IntegrationTestRpcImpl::process_block_without_verify` need this
    pub fn blocking_process_block_with_switch(
        &self,
        block: Arc<BlockView>,
        switch: Switch,
    ) -> VerifyResult {
        self.blocking_process_block_internal(block, Some(switch))
    }
```

**File:** util/app-config/src/configs/rpc.rs (L99-102)
```rust
    /// Checks whether the IntegrationTest module is enabled.
    pub fn integration_test_enable(&self) -> bool {
        self.modules.contains(&Module::IntegrationTest)
    }
```

**File:** test/template/ckb.toml (L68-69)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug"]
modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest"]
```
