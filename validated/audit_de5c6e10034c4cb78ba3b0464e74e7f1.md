### Title
Missing Zero-Value Validation for `ownerEmitterAddress` in `ExecutorUpgradable.initialize()` - (File: `target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol`)

---

### Summary

The `initialize` function of `ExecutorUpgradable` accepts a `bytes32 ownerEmitterAddress` parameter — the Wormhole emitter address of the governance authority — without validating that it is non-zero. If initialized with `bytes32(0)`, the governance execution path is permanently broken: no legitimate governance VAA will ever match, making the contract unupgradeable and ungovernable.

---

### Finding Description

In `ExecutorUpgradable.initialize()`, only the `wormhole` address is validated as non-zero. The `ownerEmitterAddress` (a `bytes32` representing the Wormhole emitter address of the governance authority) and `ownerEmitterChainId` (a `uint16`) are stored without any zero-value checks:

```solidity
// ExecutorUpgradable.sol L22-L44
function initialize(
    address wormhole,
    uint64 lastExecutedSequence,
    uint16 chainId,
    uint16 ownerEmitterChainId,
    bytes32 ownerEmitterAddress
) public initializer {
    require(wormhole != address(0), "wormhole is zero address");
    // ← No check: ownerEmitterAddress != bytes32(0)
    // ← No check: ownerEmitterChainId != 0
    ...
    Executor._initialize(wormhole, lastExecutedSequence, chainId,
        ownerEmitterChainId, ownerEmitterAddress);
    _transferOwnership(address(this));
}
```

Inside `Executor._initialize()`, the values are stored directly:

```solidity
// Executor.sol L47-L61
function _initialize(...) internal {
    require(_wormhole != address(0), "_wormhole is zero address");
    wormhole = IWormhole(_wormhole);
    lastExecutedSequence = _lastExecutedSequence;
    chainId = _chainId;
    ownerEmitterChainId = _ownerEmitterChainId;
    ownerEmitterAddress = _ownerEmitterAddress;  // ← stored without zero check
}
```

The stored `ownerEmitterAddress` is then used as the sole authorization gate in `verifyGovernanceVM()`:

```solidity
// Executor.sol L126-L129
if (
    vm.emitterChainId != ownerEmitterChainId ||
    vm.emitterAddress != ownerEmitterAddress
) revert ExecutorErrors.UnauthorizedEmitter();
```

If `ownerEmitterAddress` is `bytes32(0)`, this check will reject every real governance VAA (since legitimate Pyth governance emitters are non-zero), permanently disabling governance execution.

---

### Impact Explanation

`ExecutorUpgradable` is the on-chain governance executor for Pyth EVM deployments. It is the contract through which all governance actions — including contract upgrades, fee changes, and configuration updates — are executed. If `ownerEmitterAddress` is set to `bytes32(0)` at initialization:

1. `verifyGovernanceVM()` will revert with `UnauthorizedEmitter` for every real governance VAA, since no legitimate emitter has address `bytes32(0)`.
2. The `execute()` function becomes permanently non-functional.
3. Because `ExecutorUpgradable` owns itself (`_transferOwnership(address(this))`), and upgrades can only be triggered via `execute()`, the contract becomes permanently unupgradeable.
4. Any ETH held by the executor (used to fund governance calls) is permanently locked.

This is a permanent, irreversible loss of governance control over all Pyth contracts governed by this executor instance.

---

### Likelihood Explanation

This is a deployment-time misconfiguration risk. The `initialize` function is called once, by the deployer, during proxy setup. A scripting error, copy-paste mistake, or misconfigured deployment pipeline could pass `bytes32(0)` as `ownerEmitterAddress`. The absence of a guard means the error is silently accepted and only discovered when the first governance action is attempted — at which point the contract is already permanently broken with no recovery path. Deployment scripts in the repository (e.g., `zkSyncDeployEntropy.ts`) pass `governanceEmitter` directly from environment/config variables, making a misconfigured value plausible.

---

### Recommendation

Add explicit non-zero validation for `ownerEmitterAddress` and `ownerEmitterChainId` in `Executor._initialize()` (or in `ExecutorUpgradable.initialize()`):

```solidity
require(_ownerEmitterAddress != bytes32(0), "ownerEmitterAddress is zero");
require(_ownerEmitterChainId != 0, "ownerEmitterChainId is zero");
```

This mirrors the pattern already applied to `_wormhole` in the same function.

---

### Proof of Concept

1. Deploy `ExecutorUpgradable` proxy and call `initialize` with a valid `wormhole` address but `ownerEmitterAddress = bytes32(0)` and `ownerEmitterChainId = 0`. The call succeeds with no revert.
2. Attempt to call `execute()` with any valid Wormhole VAA signed by the real Pyth governance emitter (e.g., `0x5635979a...`).
3. `verifyGovernanceVM()` evaluates `vm.emitterAddress != bytes32(0)` → `true` → reverts with `UnauthorizedEmitter`.
4. No governance action can ever be executed. The contract is permanently ungovernable.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol (L22-44)
```text
    function initialize(
        address wormhole,
        uint64 lastExecutedSequence,
        uint16 chainId,
        uint16 ownerEmitterChainId,
        bytes32 ownerEmitterAddress
    ) public initializer {
        require(wormhole != address(0), "wormhole is zero address");

        __Ownable_init();
        __UUPSUpgradeable_init();

        Executor._initialize(
            wormhole,
            lastExecutedSequence,
            chainId,
            ownerEmitterChainId,
            ownerEmitterAddress
        );

        // Transfer ownership to the contract itself.
        _transferOwnership(address(this));
    }
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L47-61)
```text
    function _initialize(
        address _wormhole,
        uint64 _lastExecutedSequence,
        uint16 _chainId,
        uint16 _ownerEmitterChainId,
        bytes32 _ownerEmitterAddress
    ) internal {
        require(_wormhole != address(0), "_wormhole is zero address");

        wormhole = IWormhole(_wormhole);
        lastExecutedSequence = _lastExecutedSequence;
        chainId = _chainId;
        ownerEmitterChainId = _ownerEmitterChainId;
        ownerEmitterAddress = _ownerEmitterAddress;
    }
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L115-137)
```text
    // Check that the encoded VM is a valid wormhole VAA from the correct emitter
    // and with a sufficiently recent sequence number.
    function verifyGovernanceVM(
        bytes memory encodedVM
    ) internal returns (IWormhole.VM memory parsedVM) {
        (IWormhole.VM memory vm, bool valid, ) = wormhole.parseAndVerifyVM(
            encodedVM
        );

        if (!valid) revert ExecutorErrors.InvalidWormholeVaa();

        if (
            vm.emitterChainId != ownerEmitterChainId ||
            vm.emitterAddress != ownerEmitterAddress
        ) revert ExecutorErrors.UnauthorizedEmitter();

        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;

        return vm;
    }
```
