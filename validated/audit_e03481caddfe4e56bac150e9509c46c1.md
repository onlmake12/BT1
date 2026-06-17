### Title
Permissionless `initialize` in Stylus `WormholeContract` Can Be Front-Run to Install Attacker-Controlled Guardian Set — (`File: target_chains/stylus/contracts/wormhole/src/lib.rs`)

---

### Summary

The Stylus `WormholeContract.initialize()` function has no access control. Any unprivileged caller can invoke it before the deployer does. If an attacker front-runs the initialization with a malicious guardian set, the deployer's subsequent `initialize` call permanently reverts with `AlreadyInitialized`, and the Wormhole contract is left under attacker-controlled guardians. The dependent `PythReceiver` Stylus contract then verifies all VAAs against those forged guardians, allowing the attacker to submit arbitrary fake price updates.

---

### Finding Description

`WormholeContract.initialize()` in `target_chains/stylus/contracts/wormhole/src/lib.rs` is a public, permissionless function that sets the guardian set, chain IDs, and governance contract for the Wormhole instance used by the Pyth Stylus receiver: [1](#0-0) 

The only guard is a single boolean flag: [2](#0-1) 

There is no `onlyOwner`, no deployer check, and no `msg.sender` restriction of any kind. The deployment flow calls `initialize` in a **separate transaction** from contract creation, as confirmed by the initialization script: [3](#0-2) 

This two-step pattern (deploy → separate `initialize` tx) creates a front-running window identical in structure to the Uniswap `createPair` race described in the reference report.

The `PythReceiver` Stylus contract depends on this `WormholeContract` for VAA verification: [4](#0-3) 

---

### Impact Explanation

If an attacker front-runs `initialize` with a guardian set they control:

1. The deployer's `initialize` call reverts with `WormholeError::AlreadyInitialized`.
2. The `WormholeContract` is permanently locked with the attacker's guardian set.
3. The attacker can sign arbitrary VAAs with their own keys.
4. `PythReceiver.update_price_feeds()` will accept those forged VAAs as valid.
5. The attacker can write arbitrary price values for any price feed on the affected Stylus chain.

This is strictly worse than the reference report's DoS: the attacker does not merely block initialization — they seize control of the oracle's trust root.

---

### Likelihood Explanation

- The `initialize` function is public and callable by any EOA.
- Stylus chains (Arbitrum Stylus) have a public mempool; transactions are visible before inclusion.
- The two-step deploy/initialize pattern is confirmed in the repository's own shell script.
- No special knowledge, privileged key, or Sybil attack is required — only monitoring the mempool and submitting a transaction with a higher gas price.
- The attack window exists on every new chain deployment.

---

### Recommendation

Add a deployer/owner guard to `initialize`. The simplest fix is to record `msg::sender()` at construction time and require it matches in `initialize`:

```rust
pub fn initialize(&mut self, ...) -> Result<(), Vec<u8>> {
    if self.initialized.get() {
        return Err(WormholeError::AlreadyInitialized.into());
    }
    // Add: require!(msg::sender() == self.deployer.get(), ...);
    ...
}
```

Alternatively, encode the `initialize` call atomically inside the constructor (as Pyth already does for the Lazer proxy): [5](#0-4) 

---

### Proof of Concept

1. Attacker monitors the Stylus mempool for a `WormholeContract` deployment transaction.
2. Attacker submits `initialize([attacker_guardian], 0, chain_id, gov_chain_id, gov_contract)` with higher gas, front-running the deployer.
3. Attacker's transaction is included first; `initialized` is set to `true` with attacker's guardian key.
4. Deployer's `initialize` transaction reverts: `WormholeError::AlreadyInitialized`.
5. Attacker signs a VAA payload containing `price_id → $1,000,000` with their guardian key.
6. Attacker calls `PythReceiver.update_price_feeds(forged_vaa)`.
7. `WormholeContract.parse_and_verify_vm` accepts the VAA (attacker's guardian is the only registered guardian).
8. Price feed is updated to the attacker-chosen value on-chain. [1](#0-0) [6](#0-5)

### Citations

**File:** target_chains/stylus/contracts/wormhole/src/lib.rs (L116-138)
```rust
    pub fn initialize(
        &mut self,
        initial_guardians: Vec<Address>,
        initial_guardian_set_index: u32,
        chain_id: u16,
        governance_chain_id: u16,
        governance_contract: Address,
    ) -> Result<(), Vec<u8>> {
        if self.initialized.get() {
            return Err(WormholeError::AlreadyInitialized.into());
        }
        self.current_guardian_set_index
            .set(U256::from(initial_guardian_set_index));
        self.chain_id.set(U256::from(chain_id));
        self.governance_chain_id
            .set(U256::from(governance_chain_id));
        self.governance_contract.set(governance_contract);

        self.store_gs(initial_guardian_set_index, initial_guardians, 0)?;

        self.initialized.set(true);
        Ok(())
    }
```

**File:** target_chains/stylus/contracts/wormhole/src/lib.rs (L157-171)
```rust
    pub fn parse_and_verify_vm(&self, encoded_vaa: Vec<u8>) -> Result<Vec<u8>, Vec<u8>> {
        if !self.initialized.get() {
            return Err(WormholeError::NotInitialized.into());
        }

        if encoded_vaa.is_empty() {
            return Err(WormholeError::InvalidVAAFormat.into());
        }

        let vaa = self.parse_vm(&encoded_vaa)?;

        let _verified = self.verify_vm(&vaa)?;

        Ok(vaa.payload)
    }
```

**File:** target_chains/stylus/contracts/wormhole/contract_tests/initialize_contract.sh (L35-35)
```shellscript
if cast send "$CONTRACT_ADDRESS" "initialize(address[],uint16,uint16,address)" "$GUARDIAN_ADDRESSES" $CHAIN_ID $GOVERNANCE_CHAIN_ID $GOVERNANCE_CONTRACT --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY"; then
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/integration_tests.rs (L462-477)
```rust
    #[motsu::test]
    fn test_double_initialization_reverts(
        pyth_contract: Contract<PythReceiver>,
        wormhole_contract: Contract<WormholeContract>,
        alice: Address,
    ) {
        let _ = pyth_wormhole_init(&pyth_contract, &wormhole_contract, &alice);

        let double_init = pyth_init(&pyth_contract, &wormhole_contract, &alice);

        assert!(double_init.is_err());
        assert_eq!(
            double_init.unwrap_err(),
            PythReceiverError::AlreadyInitialized
        );
    }
```

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L150-159)
```text
        addr = createX.deployCreate3({
            salt: salt,
            initCode: abi.encodePacked(
                type(ERC1967Proxy).creationCode,
                abi.encode(
                    impl,
                    abi.encodeWithSignature("initialize(address)", topAuthority)
                )
            )
        });
```
