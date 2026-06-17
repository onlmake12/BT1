### Title
Missing Zero-Address Validation for `wormhole` in `PythUpgradable.initialize()` — (File: `target_chains/ethereum/contracts/contracts/pyth/PythUpgradable.sol`)

---

### Summary

`PythUpgradable.initialize()` accepts an `address wormhole` parameter and passes it directly to `Pyth._initialize()` → `setWormhole()` without any `require(wormhole != address(0))` guard. If the proxy is initialized with `wormhole = address(0)` — whether by deployer error or by a front-running attacker on an uninitialized proxy — every subsequent price-update call that invokes `wormhole().parseAndVerifyVM()` will call into `address(0)`, permanently bricking the on-chain Pyth oracle.

---

### Finding Description

`PythUpgradable.initialize()` is declared `public initializer`: [1](#0-0) 

It accepts `address wormhole` and forwards it to `Pyth._initialize()` with no zero-address guard: [2](#0-1) 

`Pyth._initialize()` calls `setWormhole(wormhole)` unconditionally. There is no `require(wormhole != address(0))` at any layer between the public entry point and the storage write.

Contrast this with every other upgradeable contract in the same repo, which all guard their address parameters:

- `EntropyUpgradable.initialize()` — `require(owner != address(0))`, `require(admin != address(0))`, `require(defaultProvider != address(0))` [3](#0-2) 
- `SchedulerUpgradeable.initialize()` — `require(owner != address(0))`, `require(admin != address(0))`, `require(pythAddress != address(0))` [4](#0-3) 
- `ExecutorUpgradable.initialize()` — `require(wormhole != address(0))` [5](#0-4) 

`PythUpgradable` is the only upgradeable contract in the suite that omits this guard for its critical address dependency.

---

### Impact Explanation

If `wormhole` is stored as `address(0)`:

1. `updatePriceFeeds()` calls `updatePriceInfosFromAccumulatorUpdate()` → `wormhole().parseAndVerifyVM()` → call to `address(0)` → revert. No price feed can ever be updated.
2. `executeGovernanceInstruction()` calls `verifyGovernanceVM()` → `wormhole().parseAndVerifyVM()` → same revert. No governance action (including a corrective upgrade) can be executed.
3. Because `PythUpgradable` renounces ownership at the end of `initialize()` (`renounceOwnership()`), there is no owner who can call `upgradeTo` directly — upgrades must go through governance, which is also bricked.

The result is **permanent, irrecoverable freezing** of the Pyth price oracle on the affected chain. [6](#0-5) 

---

### Likelihood Explanation

Two realistic paths exist:

1. **Deployer error**: The deployer passes `address(0)` for `wormhole` by mistake (e.g., a misconfigured deployment script). Because `initialize()` is `public` and can only be called once, there is no recovery.

2. **Front-running an uninitialized proxy**: If the proxy is deployed in a transaction that does not atomically call `initialize()` (e.g., a two-step deploy script), an attacker observing the mempool can call `initialize(address(0), ...)` before the deployer, permanently seizing the initialization slot with a zero wormhole address.

Path 2 is attacker-controlled and requires no privileged access — only the ability to submit a transaction before the deployer's second transaction confirms.

---

### Recommendation

Add a zero-address guard for `wormhole` in `PythUpgradable.initialize()`, consistent with every other upgradeable contract in the codebase:

```solidity
function initialize(
    address wormhole,
    ...
) public initializer {
+   require(wormhole != address(0), "wormhole is zero address");
    __Ownable_init();
    __UUPSUpgradeable_init();
    Pyth._initialize(wormhole, ...);
    renounceOwnership();
}
```

Optionally, add the same guard inside `Pyth._initialize()` as a defense-in-depth measure.

---

### Proof of Concept

```solidity
// Attacker front-runs the deployer's initialize() call on a freshly deployed proxy

PythUpgradable proxy = PythUpgradable(proxyAddress);

proxy.initialize(
    address(0),          // wormhole = zero address
    new uint16[](0),     // no data sources
    new bytes32[](0),
    0,                   // governanceEmitterChainId
    bytes32(0),          // governanceEmitterAddress
    0,                   // governanceInitialSequence
    60,                  // validTimePeriodSeconds
    1                    // singleUpdateFeeInWei
);

// Now wormhole() == address(0).
// Any call to updatePriceFeeds() or executeGovernanceInstruction() reverts.
// renounceOwnership() was called inside initialize(), so no owner can rescue the contract.
// The proxy is permanently bricked.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/PythUpgradable.sol (L23-48)
```text
    function initialize(
        address wormhole,
        uint16[] calldata dataSourceEmitterChainIds,
        bytes32[] calldata dataSourceEmitterAddresses,
        uint16 governanceEmitterChainId,
        bytes32 governanceEmitterAddress,
        uint64 governanceInitialSequence,
        uint validTimePeriodSeconds,
        uint singleUpdateFeeInWei
    ) public initializer {
        __Ownable_init();
        __UUPSUpgradeable_init();

        Pyth._initialize(
            wormhole,
            dataSourceEmitterChainIds,
            dataSourceEmitterAddresses,
            governanceEmitterChainId,
            governanceEmitterAddress,
            governanceInitialSequence,
            validTimePeriodSeconds,
            singleUpdateFeeInWei
        );

        renounceOwnership();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L20-31)
```text
    function _initialize(
        address wormhole,
        uint16[] calldata dataSourceEmitterChainIds,
        bytes32[] calldata dataSourceEmitterAddresses,
        uint16 governanceEmitterChainId,
        bytes32 governanceEmitterAddress,
        uint64 governanceInitialSequence,
        uint validTimePeriodSeconds,
        uint singleUpdateFeeInWei
    ) internal {
        setWormhole(wormhole);

```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L34-39)
```text
        require(owner != address(0), "owner is zero address");
        require(admin != address(0), "admin is zero address");
        require(
            defaultProvider != address(0),
            "defaultProvider is zero address"
        );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L31-33)
```text
        require(owner != address(0), "owner is zero address");
        require(admin != address(0), "admin is zero address");
        require(pythAddress != address(0), "pyth is zero address");
```

**File:** target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol (L29-29)
```text
        require(wormhole != address(0), "wormhole is zero address");
```
