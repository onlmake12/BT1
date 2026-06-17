### Title
`PythLazerDeploy.s.sol::run()` Does Not Transfer Ownership of `PythLazer` Proxy to Governance Executor After Deployment - (File: lazer/contracts/evm/script/PythLazerDeploy.s.sol)

---

### Summary

`PythLazerDeploy.s.sol::deployProxy()` initializes the `PythLazer` proxy with the hardcoded `deployer` EOA as owner ("for the time being"), but `run()` never atomically transfers ownership to the intended governance executor contract. Ownership transfer is left as a completely separate, manually-run script (`PythLazerChangeOwnership.s.sol`). If this step is skipped or delayed, the deployer EOA permanently retains `onlyOwner` control over trusted signer management and contract upgrades.

---

### Finding Description

In `PythLazerDeploy.s.sol`, `deployProxy()` explicitly sets `topAuthority = deployer` and passes it as the `_topAuthority` argument to `PythLazer.initialize()`:

```solidity
// Set the top authority to the deployer for the time being
address topAuthority = deployer;

vm.startBroadcast();
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
``` [1](#0-0) 

`PythLazer.initialize()` calls `__Ownable_init(_topAuthority)`, making the deployer EOA the permanent owner until explicitly changed:

```solidity
function initialize(address _topAuthority) public initializer {
    __Ownable_init(_topAuthority);
    __UUPSUpgradeable_init();
    verification_fee = 1 wei;
}
``` [2](#0-1) 

The `run()` function only deploys the implementation and proxy — it performs no ownership transfer:

```solidity
function run() public {
    address impl = deployImplementation("lazer:impl");
    address proxy = deployProxy("lazer:proxy", impl);
    _writeOutput(impl, proxy, "./deployment-output.json");
}
``` [3](#0-2) 

The ownership transfer to the governance executor is a completely separate, manually-invoked script (`PythLazerChangeOwnership.s.sol`), which must be run as a distinct post-deployment step: [4](#0-3) 

Both critical `PythLazer` functions are `onlyOwner`:

```solidity
function updateTrustedSigner(address trustedSigner, uint256 expiresAt) external onlyOwner { ... }
function _authorizeUpgrade(address) internal override onlyOwner {}
``` [5](#0-4) 

The intended post-deployment owner is the governance executor contract, as confirmed by `EvmLazerContract.generateUpdateTrustedSignerPayload()`:

```solidity
// Executor contract is the owner of the PythLazer contract
const executorAddress = await this.getOwner();
``` [6](#0-5) 

---

### Impact Explanation

If the `PythLazerChangeOwnership.s.sol` step is omitted or delayed, the deployer EOA retains full `onlyOwner` authority over `PythLazer`. An attacker who compromises the deployer key (or a negligent deployer) can:

1. **Call `updateTrustedSigner`** to register an attacker-controlled address as a trusted Lazer signer with an arbitrary expiration. All subsequent calls to `verifyUpdate()` by any consumer contract will accept fraudulent price payloads signed by the attacker, enabling price manipulation across any DeFi protocol using Pyth Lazer.
2. **Call `upgradeTo`** to replace the proxy implementation with a malicious contract, permanently compromising the Lazer verification infrastructure.

---

### Likelihood Explanation

**Medium-High.** The comment "for the time being" in the deploy script explicitly acknowledges the deployer is a temporary owner, confirming the ownership transfer is a known required follow-up step. Multi-step deployment procedures are a well-documented source of operational failures. The existence of a separate `PythLazerChangeOwnership.s.sol` script — rather than an atomic transfer within `run()` — means the transfer can be forgotten, delayed, or executed against the wrong executor address. The deployer key is also a higher-value target during the deployment window.

---

### Recommendation

Atomically transfer ownership to the governance executor contract within `deployProxy()` or `run()`, immediately after the proxy is deployed:

```solidity
function run() public {
    address impl = deployImplementation("lazer:impl");
    address proxy = deployProxy("lazer:proxy", impl);

    // Atomically transfer ownership to the governance executor
    address governanceExecutor = vm.envAddress("GOVERNANCE_EXECUTOR");
    vm.startBroadcast();
    PythLazer(proxy).transferOwnership(governanceExecutor);
    vm.stopBroadcast();

    _writeOutput(impl, proxy, "./deployment-output.json");
}
```

This eliminates the deployment window and removes the dependency on a separate manual script.

---

### Proof of Concept

1. Operator runs `forge script script/PythLazerDeploy.s.sol --broadcast` — `PythLazer` proxy is deployed with `deployer` (EOA `0x78357316239040e19fC823372cC179ca75e64b81`) as owner.
2. Operator forgets (or delays) running `PythLazerChangeOwnership.s.sol`.
3. Attacker compromises the deployer key.
4. Attacker calls:
   ```solidity
   PythLazer(0xACeA761c27A909d4D3895128EBe6370FDE2dF481)
       .updateTrustedSigner(attackerSigner, block.timestamp + 365 days);
   ```
5. `isValidSigner(attackerSigner)` returns `true`.
6. Any consumer calling `verifyUpdate()` with an attacker-signed payload will receive `(payload, attackerSigner)` without revert, accepting fabricated price data as legitimate Pyth Lazer output.

### Citations

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L146-160)
```text
        // Set the top authority to the deployer for the time being
        address topAuthority = deployer;

        vm.startBroadcast();
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
        vm.stopBroadcast();
```

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L178-184)
```text
    function run() public {
        address impl = deployImplementation("lazer:impl");
        address proxy = deployProxy("lazer:proxy", impl);

        // Write deployment output to JSON file for programmatic access
        _writeOutput(impl, proxy, "./deployment-output.json");
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L22-27)
```text
    function initialize(address _topAuthority) public initializer {
        __Ownable_init(_topAuthority);
        __UUPSUpgradeable_init();

        verification_fee = 1 wei;
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L29-34)
```text
    function _authorizeUpgrade(address) internal override onlyOwner {}

    function updateTrustedSigner(
        address trustedSigner,
        uint256 expiresAt
    ) external onlyOwner {
```

**File:** lazer/contracts/evm/script/PythLazerChangeOwnership.s.sol (L1-19)
```text
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

// --- Script Purpose ---
// This script transfers ownership of the deployed PythLazer contract (proxy) to a new owner contract (typically the governance executor contract).
// Usage: Run this script after deploying the new executor contract on the target chain. Ensure the executor address is correct and deployed.
// Preconditions:
//   - The LAZER_PROXY_ADDRESS must point to the deployed PythLazer proxy contract. Currently set to 0xACeA761c27A909d4D3895128EBe6370FDE2dF481, which was made using createX.
//   - The NEW_OWNER must be the deployed executor contract address on this chain.
//   - The script must be run by the current owner (OLD_OWNER) of the PythLazer contract.
//   - The DEPLOYER_PRIVATE_KEY environment variable must be set to the current owner's private key.
//
// Steps:
//   1. Log current and new owner addresses, and the proxy address.
//   2. Check the current owner matches the expected OLD_OWNER.
//   3. Transfer ownership to the NEW_OWNER (executor contract).
//   4. Log the new owner for verification.
//
// Note: This script is intended for use with Foundry (forge-std) tooling.
```

**File:** contract_manager/src/core/contracts/evm.ts (L1043-1044)
```typescript
    // Executor contract is the owner of the PythLazer contract
    const executorAddress = await this.getOwner();
```
