### Title
Unprotected `initialize()` with No Access Control Allows Front-Running Deployment — (File: `target_chains/stylus/contracts/pyth-receiver/src/lib.rs`)

---

### Summary

The `PythReceiver::initialize()` function in the Stylus (Arbitrum) implementation has no access control. Any external caller can invoke it before the legitimate deployer does. A successful front-run permanently misconfigures the contract with attacker-controlled parameters, including a malicious Wormhole address and governance emitter, because the `initialized` flag then prevents any corrective re-initialization.

---

### Finding Description

`PythReceiver::initialize()` is a `#[public]` function with no caller restriction. Its only guard is a boolean `initialized` storage flag: [1](#0-0) 

```rust
pub fn initialize(
    &mut self,
    wormhole: Address,
    ...
) -> Result<(), PythReceiverError> {
    if self.initialized.get() {
        return Err(PythReceiverError::AlreadyInitialized.into());
    }
    ...
    self.initialized.set(true);
    Ok(())
}
```

There is no `onlyOwner`, no deployer check, and no factory-gating. The `initialized` flag only prevents a second call — it does not prevent a *first* call by an arbitrary address.

The parameters accepted by `initialize()` include:

- `wormhole: Address` — the Wormhole contract used to verify all VAAs
- `data_source_emitter_chain_ids` / `data_source_emitter_addresses` — the set of trusted Pyth data sources
- `governance_emitter_chain_id` / `governance_emitter_address` — the governance authority
- `governance_initial_sequence` — the replay-protection sequence counter [2](#0-1) 

All of these are written unconditionally if `initialized` is false, and the flag is set to `true` at the end, permanently locking the configuration. [3](#0-2) 

---

### Impact Explanation

An attacker who front-runs `initialize()` can:

1. Supply a malicious `wormhole` address that accepts any VAA as valid, bypassing guardian-set verification entirely.
2. Register attacker-controlled `data_source_emitter_*` values, making the contract accept price updates from arbitrary emitters.
3. Set themselves as the `governance_emitter`, granting full governance control (fee changes, data-source changes, contract upgrades via governance messages).

After the attacker's call succeeds, `self.initialized` is `true`. The legitimate deployer's subsequent `initialize()` call reverts with `AlreadyInitialized`. The contract is permanently misconfigured with no recovery path short of redeployment. [4](#0-3) 

---

### Likelihood Explanation

The attack requires front-running the deployment initialization transaction. On Arbitrum (the target chain for Stylus contracts), the centralized sequencer makes classic mempool front-running harder, but the window is still real if:

- Deployment and initialization are submitted as separate transactions (common in scripted deployments).
- There is any observable delay between contract creation and the `initialize()` call.
- The sequencer itself is compromised or the attacker has sequencer-level access.

The attack is a single transaction with no special privileges required — any EOA can execute it.

---

### Recommendation

Add an access control check so that only the deployer (or a designated factory) can call `initialize()`. The simplest fix is to record `msg::sender()` at construction time and assert it in `initialize()`:

```rust
// In storage struct
pub deployer: StorageAddress,

// In a constructor or first-call guard
self.deployer.set(contract_address()); // set to deployer at deploy time

// At the top of initialize()
if self.deployer.get() != msg::sender() {
    return Err(PythReceiverError::Unauthorized);
}
```

Alternatively, mirror the pattern used by all EVM upgradeable Pyth contracts, which call `_disableInitializers()` in the constructor and gate `initialize()` with OpenZeppelin's `initializer` modifier. [5](#0-4) 

---

### Proof of Concept

1. Attacker monitors the Arbitrum mempool (or watches for the contract deployment event).
2. Attacker calls `PythReceiver::initialize(malicious_wormhole, ...)` before the legitimate deployer.
3. `self.initialized.get()` returns `false` → the call succeeds, writing attacker-controlled values.
4. `self.initialized` is set to `true`.
5. Legitimate deployer's `initialize()` call reverts with `AlreadyInitialized`.
6. Attacker's malicious Wormhole contract accepts any VAA → attacker submits fabricated price updates that pass `parse_and_verify_vm` → `update_price_feeds` stores attacker-chosen prices for any feed ID. [6](#0-5)

### Citations

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L83-84)
```rust
    pub initialized: StorageBool,
}
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L88-132)
```rust
    pub fn initialize(
        &mut self,
        wormhole: Address,
        single_update_fee_in_wei: U256,
        valid_time_period_seconds: U256,
        data_source_emitter_chain_ids: Vec<u16>,
        data_source_emitter_addresses: Vec<[u8; 32]>,
        governance_emitter_chain_id: u16,
        governance_emitter_address: [u8; 32],
        governance_initial_sequence: u64,
    ) -> Result<(), PythReceiverError> {
        if self.initialized.get() {
            return Err(PythReceiverError::AlreadyInitialized.into());
        }
        self.wormhole.set(wormhole);
        self.single_update_fee_in_wei.set(single_update_fee_in_wei);
        self.valid_time_period_seconds
            .set(valid_time_period_seconds);

        self.governance_data_source_chain_id
            .set(U16::from(governance_emitter_chain_id));
        self.governance_data_source_emitter_address
            .set(FixedBytes::<32>::from(governance_emitter_address));

        self.last_executed_governance_sequence
            .set(U64::from(governance_initial_sequence));
        self.governance_data_source_index.set(U32::ZERO);

        for (i, chain_id) in data_source_emitter_chain_ids.iter().enumerate() {
            let emitter_address = FixedBytes::<32>::from(data_source_emitter_addresses[i]);

            let mut data_source = self.valid_data_sources.grow();
            data_source.chain_id.set(U16::from(*chain_id));
            data_source.emitter_address.set(emitter_address);

            let data_source_key = DataSource {
                chain_id: U16::from(*chain_id),
                emitter_address: emitter_address,
            };

            self.is_valid_data_source.setter(data_source_key).set(true);
        }
        self.initialized.set(true);
        Ok(())
    }
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L417-423)
```rust
        match accumulator_update.proof {
            Proof::WormholeMerkle { vaa, updates } => {
                let wormhole: IWormholeContract = IWormholeContract::new(self.wormhole.get());
                let config = Call::new();
                wormhole
                    .parse_and_verify_vm(config, Vec::from(vaa.clone()))
                    .map_err(|_| PythReceiverError::InvalidWormholeMessage)?;
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L13-15)
```text
    constructor() {
        _disableInitializers();
    }
```
