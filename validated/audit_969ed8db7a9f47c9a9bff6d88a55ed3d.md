### Title
Missing Zero-Address Validation for `wormhole` Parameter in `PythUpgradable.initialize` - (File: target_chains/ethereum/contracts/contracts/pyth/PythUpgradable.sol)

### Summary
`PythUpgradable.initialize` accepts a `wormhole` address parameter with no zero-address check. If initialized with `address(0)`, the contract is permanently bricked: all price update calls revert, and governance cannot recover the contract because governance VAA verification also depends on the wormhole address.

### Finding Description
`PythUpgradable.initialize` passes the `wormhole` parameter directly to `Pyth._initialize`, which calls `setWormhole(wormhole)`. Neither function validates that `wormhole != address(0)`. [1](#0-0) 

`setWormhole` in `PythSetters.sol` performs no validation either — it blindly assigns the value: [2](#0-1) 

`Pyth._initialize` also performs no zero-address check on `wormhole` before calling `setWormhole`: [3](#0-2) 

After initialization, `renounceOwnership()` is called, permanently removing any owner-based recovery path: [4](#0-3) 

The governance-based recovery path (`setWormholeAddress`) also fails because it calls `wormhole().parseAndVerifyVM(encodedVM)` — which reverts when `wormhole` is `address(0)`: [5](#0-4) 

This contrasts with every other upgradeable contract in the same codebase, which all include explicit zero-address guards. For example, `ExecutorUpgradable.initialize` checks `require(wormhole != address(0), "wormhole is zero address")`: [6](#0-5) 

`EntropyUpgradable.initialize` checks `owner`, `admin`, and `defaultProvider`: [7](#0-6) 

`SchedulerUpgradeable.initialize` checks `owner`, `admin`, and `pythAddress`: [8](#0-7) 

`PythUpgradable` is the only upgradeable contract in the suite that omits this guard for its critical dependency address. [1](#0-0) 

### Impact Explanation
If `wormhole` is set to `address(0)` at initialization time:

1. Every call to `updatePriceFeeds` reverts — the contract calls `wormhole().parseAndVerifyVM()` on `address(0)`, which is not a contract.
2. Governance cannot recover the contract: `setWormholeAddress` also calls `wormhole().parseAndVerifyVM()` to verify the governance VAA, so it too reverts.
3. `renounceOwnership()` is called unconditionally at the end of `initialize`, so there is no owner who could perform an emergency upgrade.
4. The `initializer` modifier prevents re-initialization.

The result is a permanently non-functional Pyth oracle deployment. Any protocol or user relying on price feeds from this deployment would receive no data, and any funds locked in dependent protocols could be at risk. [1](#0-0) 

### Likelihood Explanation
The likelihood is low-to-medium. The `initialize` function is called once by the deployer, typically via a deployment script. A scripting error, copy-paste mistake, or misconfigured environment variable could result in `address(0)` being passed. The risk is elevated because:

- There is no on-chain guard to catch the mistake.
- The mistake is irreversible (no owner, no re-initialization, governance also broken).
- All other contracts in the same codebase have this guard, suggesting it was an oversight rather than intentional design. [1](#0-0) 

### Recommendation
Add a zero-address check for `wormhole` in `PythUpgradable.initialize`, consistent with the pattern used in all other upgradeable contracts in the codebase:

```solidity
function initialize(
    address wormhole,
    ...
) public initializer {
    require(wormhole != address(0), "wormhole is zero address"); // ADD THIS
    __Ownable_init();
    __UUPSUpgradeable_init();
    Pyth._initialize(wormhole, ...);
    renounceOwnership();
}
```

Optionally, also add the check inside `Pyth._initialize` for defense-in-depth. [1](#0-0) 

### Proof of Concept
1. Deploy `PythUpgradable` proxy and call `initialize` with `wormhole = address(0)` and valid values for all other parameters.
2. The `initializer` modifier allows the call to succeed; `setWormhole(address(0))` stores `address(0)` in `_state.wormhole`.
3. `renounceOwnership()` executes, removing the owner.
4. Call `updatePriceFeeds(validUpdateData)` — the call reverts because `wormhole()` returns `address(0)` and calling `parseAndVerifyVM` on it fails.
5. Attempt governance recovery via `executeGovernanceInstruction(validGovernanceVAA)` — this also reverts for the same reason.
6. The contract is permanently non-functional with no recovery path. [3](#0-2) [2](#0-1) [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/pyth/PythSetters.sol (L10-12)
```text
    function setWormhole(address wh) internal {
        _state.wormhole = payable(wh);
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

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L213-226)
```text
    function setWormholeAddress(
        SetWormholeAddressPayload memory payload,
        bytes memory encodedVM
    ) internal {
        address oldWormholeAddress = address(wormhole());
        setWormhole(payload.newWormholeAddress);

        // We want to verify that the new wormhole address is valid, so we make sure that it can
        // parse and verify the same governance VAA that is used to set it.
        (IWormhole.VM memory vm, bool valid, ) = wormhole().parseAndVerifyVM(
            encodedVM
        );

        if (!valid) revert PythErrors.InvalidGovernanceMessage();
```

**File:** target_chains/ethereum/contracts/contracts/executor/ExecutorUpgradable.sol (L29-29)
```text
        require(wormhole != address(0), "wormhole is zero address");
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
