### Title
Unprotected `setup()` Initializer Allows Frontrun to Install Malicious Guardians and Implementation — (File: `target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverSetup.sol`)

---

### Summary

`ReceiverSetup.setup()` has no access control and no initializer guard. Any unprivileged caller can frontrun the deployer's setup transaction, install malicious Wormhole guardians, and redirect the proxy to an attacker-controlled implementation — giving full control over VAA verification that Pyth depends on for price feed updates.

---

### Finding Description

`ReceiverSetup` is the initial implementation of the Wormhole receiver proxy used by Pyth. The proxy is deployed pointing to `ReceiverSetup`, and then `setup()` is called to store guardian sets, chain IDs, governance contract, and upgrade the proxy to `ReceiverImplementation` via `_upgradeTo(implementation)`.

The `setup()` function is declared `public` with zero access control:

```solidity
function setup(
    address implementation,
    address[] memory initialGuardians,
    uint16 chainId,
    uint16 governanceChainId,
    bytes32 governanceContract
) public {
``` [1](#0-0) 

There is no `onlyOwner`, no OpenZeppelin `initializer` modifier, and no `isInitialized` check (unlike `ReceiverImplementation`, which does guard its own initializer). The function directly calls `_upgradeTo(implementation)`, permanently replacing the proxy's implementation with whatever address the caller supplies. [2](#0-1) 

By contrast, every other upgradeable contract in the codebase protects its initializer:

- `PythLazer`: `constructor() { _disableInitializers(); }` [3](#0-2) 
- `SchedulerUpgradeable`: `constructor() { _disableInitializers(); }` [4](#0-3) 
- `EntropyUpgradable`, `ExecutorUpgradable`, `EchoUpgradeable`, `PythUpgradable`: all use `constructor() initializer {}` [5](#0-4) 

`ReceiverSetup` has none of these protections. [6](#0-5) 

---

### Impact Explanation

An attacker who frontruns the deployer's `setup()` call can:

1. Supply an `initialGuardians` array containing only attacker-controlled keys — making the Wormhole receiver accept VAAs signed by the attacker.
2. Supply a malicious `implementation` address — `_upgradeTo(implementation)` permanently redirects the proxy to attacker code.
3. Set `governanceContract` to an attacker-controlled address — blocking any legitimate governance recovery.

After this, the Pyth price oracle contract (`PythUpgradable`) that relies on this Wormhole receiver to verify VAAs will accept attacker-crafted price update messages. The attacker can publish arbitrary prices for any price feed, enabling theft from any protocol that consumes Pyth prices (liquidations, minting, swaps). This is a critical, irreversible compromise of the entire price feed system on the affected chain. [7](#0-6) 

---

### Likelihood Explanation

The attack requires only:
- Observing the proxy deployment transaction in the public mempool (trivially done by any MEV bot or attacker monitoring deployments).
- Submitting a `setup()` call with a higher gas price before the deployer's transaction is mined.

No privileged access, leaked keys, or social engineering is required. The window exists whenever the proxy deployment and the `setup()` call are in separate transactions, which is the standard deployment flow for this proxy pattern. [8](#0-7) 

---

### Recommendation

Add an `isInitialized` guard identical to the one used in `ReceiverImplementation`, or use OpenZeppelin's `_disableInitializers()` pattern used by the rest of the Pyth codebase. The simplest fix is to check that `setup()` has not already been called (e.g., by verifying the guardian set is empty) and to deploy the proxy and call `setup()` atomically in a single transaction so no frontrun window exists. [9](#0-8) 

---

### Proof of Concept

1. Deployer broadcasts transaction A: deploy the Wormhole receiver proxy with `ReceiverSetup` as implementation.
2. Attacker sees transaction A in the mempool. Before the deployer's `setup()` call (transaction B) is mined, attacker broadcasts transaction C with higher gas:
   ```solidity
   ReceiverSetup(proxyAddress).setup(
       attackerImpl,          // malicious implementation
       [attackerKey],         // attacker-controlled guardian
       chainId,
       governanceChainId,
       attackerGovernance
   );
   ```
3. Transaction C mines first. The proxy now points to `attackerImpl` with `attackerKey` as the sole guardian.
4. Deployer's transaction B reverts or is irrelevant — the proxy is already compromised.
5. Attacker signs a VAA with `attackerKey` containing fabricated price data (e.g., ETH/USD = $1).
6. Attacker calls `PythUpgradable.updatePriceFeeds()` with the fake VAA. The Wormhole receiver accepts it (guardian check passes). Pyth stores the manipulated price.
7. Any on-chain protocol reading `IPyth.getPrice()` receives the attacker's price, enabling liquidations, undercollateralized minting, or arbitrage theft. [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverSetup.sol (L11-33)
```text
contract ReceiverSetup is ReceiverSetters, ERC1967Upgrade {
    function setup(
        address implementation,
        address[] memory initialGuardians,
        uint16 chainId,
        uint16 governanceChainId,
        bytes32 governanceContract
    ) public {
        require(initialGuardians.length > 0, "no guardians specified");

        ReceiverStructs.GuardianSet memory initialGuardianSet = ReceiverStructs
            .GuardianSet({keys: initialGuardians, expirationTime: 0});

        storeGuardianSet(initialGuardianSet, 0);
        // initial guardian set index is 0, which is the default value of the storage slot anyways

        setChainId(chainId);

        setGovernanceChainId(governanceChainId);
        setGovernanceContract(governanceContract);

        _upgradeTo(implementation);
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L13-15)
```text
    constructor() {
        _disableInitializers();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L49-51)
```text
    constructor() {
        _disableInitializers();
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L57-57)
```text
    constructor() initializer {}
```

**File:** target_chains/ethereum/contracts/contracts/wormhole-receiver/ReceiverImplementation.sol (L12-20)
```text
    modifier initializer() {
        address implementation = ERC1967Upgrade._getImplementation();

        require(!isInitialized(implementation), "already initialized");

        setInitialized(implementation);

        _;
    }
```
