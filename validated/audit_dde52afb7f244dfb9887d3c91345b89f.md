### Title
Unprotected `initialize()` Allows Front-Running to Permanently Brick or Hijack `PythReceiver` - (File: `target_chains/stylus/contracts/pyth-receiver/src/lib.rs`)

---

### Summary

The Stylus `PythReceiver` contract exposes a public `initialize()` function with no access control. Any address can call it before the legitimate deployer, permanently locking in attacker-controlled parameters (wormhole address, data sources, governance emitter). The legitimate deployer's subsequent `initialize()` call will revert with `AlreadyInitialized`, bricking the contract or placing it under attacker control.

---

### Finding Description

`PythReceiver::initialize()` is declared `pub` inside a `#[public]` impl block, making it an externally callable function on the deployed Stylus contract. Its only guard is a single boolean flag:

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
    ...
    self.initialized.set(true);
    Ok(())
}
```

There is no `onlyOwner`, no deployer check, no role guard, and no `msg.sender` validation of any kind. The `initialized` flag is set to `true` at the end, making the call irreversible. [1](#0-0) 

The attacker-controlled parameters that get permanently committed are:

| Parameter | Impact if Attacker-Controlled |
|---|---|
| `wormhole` | All VAA verification routes through attacker's contract |
| `data_source_emitter_addresses` | Attacker's emitter is accepted as valid price source |
| `governance_emitter_address` | Attacker controls governance execution |
| `governance_initial_sequence` | Replay protection can be bypassed | [2](#0-1) 

The `wormhole` address is used directly in `parse_price_feed_updates_internal` to verify every incoming price update VAA:

```rust
let wormhole: IWormholeContract = IWormholeContract::new(self.wormhole.get());
let config = Call::new();
wormhole
    .parse_and_verify_vm(config, Vec::from(vaa.clone()))
    .map_err(|_| PythReceiverError::InvalidWormholeMessage)?;
``` [3](#0-2) 

If the attacker sets `wormhole` to a contract they control that always returns `Ok`, and sets their own address as a valid data source, they can inject arbitrary price data into the `PythReceiver`.

---

### Impact Explanation

**Scenario A – DoS / Brick:** Attacker calls `initialize()` with any valid-looking parameters before the deployer. The deployer's `initialize()` reverts with `AlreadyInitialized`. The contract is permanently stuck with attacker-chosen parameters and cannot be re-initialized. All downstream consumers of this `PythReceiver` instance receive corrupted or stale prices.

**Scenario B – Full Takeover:** Attacker supplies their own `wormhole` contract (one that accepts any VAA) and their own emitter address as a valid data source. After initialization, the attacker can call `update_price_feeds()` with self-crafted price data, injecting arbitrary prices for any feed. Any protocol consuming this `PythReceiver` for liquidations, collateral valuation, or settlement is fully compromised.

---

### Likelihood Explanation

Stylus contracts on Arbitrum are deployed in a two-step process: bytecode deployment followed by a separate `initialize()` call. This window is observable on-chain. A mempool-watching bot can trivially detect the deployment transaction and front-run the initialization with a higher gas price. The attack requires no special privileges, no leaked keys, and no off-chain coordination — only the ability to submit a transaction.

---

### Recommendation

Add an access control check to `initialize()`. The simplest approach for a Stylus contract is to store the deployer address in a storage slot that is set atomically at contract creation (using a factory/deployer contract that deploys and initializes in a single transaction), or to restrict `initialize()` to a hardcoded deployer address:

```rust
pub fn initialize(&mut self, ...) -> Result<(), PythReceiverError> {
    if self.initialized.get() {
        return Err(PythReceiverError::AlreadyInitialized);
    }
    // Ensure only the designated deployer can initialize
    if self.vm().msg_sender() != EXPECTED_DEPLOYER_ADDRESS {
        return Err(PythReceiverError::Unauthorized);
    }
    ...
}
```

Alternatively, deploy and initialize atomically via a factory contract so no window exists between deployment and initialization.

---

### Proof of Concept

1. Deployer broadcasts a transaction to deploy `PythReceiver` bytecode on Arbitrum.
2. Attacker monitors the mempool, detects the deployment.
3. Before the deployer's `initialize()` call lands, attacker submits:
   ```
   PythReceiver.initialize(
       wormhole = <attacker_wormhole_contract>,
       single_update_fee_in_wei = 0,
       valid_time_period_seconds = 999999999,
       data_source_emitter_chain_ids = [1],
       data_source_emitter_addresses = [<attacker_emitter>],
       governance_emitter_chain_id = 1,
       governance_emitter_address = <attacker_governance>,
       governance_initial_sequence = 0
   )
   ```
4. Attacker's tx confirms first; `self.initialized` is set to `true`.
5. Deployer's `initialize()` call reverts with `AlreadyInitialized`.
6. Attacker's `wormhole` contract accepts any VAA. Attacker calls `update_price_feeds()` with a crafted accumulator update signed by their own emitter, injecting arbitrary prices for any price feed ID. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L86-132)
```rust
#[public]
impl PythReceiver {
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

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L217-234)
```rust
    #[payable]
    pub fn update_price_feeds(
        &mut self,
        update_data: Vec<Vec<u8>>,
    ) -> Result<(), PythReceiverError> {
        let total_fee = self.get_update_fee(update_data.clone())?;

        let value = self.vm().msg_value();

        if value < total_fee {
            return Err(PythReceiverError::InsufficientFee);
        }

        for data in &update_data {
            self.update_price_feeds_internal(data.clone(), 0, 0, false)?;
        }
        Ok(())
    }
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L419-424)
```rust
                let wormhole: IWormholeContract = IWormholeContract::new(self.wormhole.get());
                let config = Call::new();
                wormhole
                    .parse_and_verify_vm(config, Vec::from(vaa.clone()))
                    .map_err(|_| PythReceiverError::InvalidWormholeMessage)?;

```
