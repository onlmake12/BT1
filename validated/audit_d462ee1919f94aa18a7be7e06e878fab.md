### Title
Missing Zero-Address Validation for `wormhole` in `Pyth._initialize()` - (File: `target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

`Pyth._initialize()` accepts a `wormhole` address parameter and stores it without any zero-address guard. If the deployer passes `address(0)`, the contract is permanently bricked because `PythUpgradable.initialize()` calls `renounceOwnership()` immediately after, making any upgrade or correction impossible.

---

### Finding Description

In `Pyth._initialize()`, the `wormhole` address is stored unconditionally: [1](#0-0) 

No check of the form `require(wormhole != address(0), ...)` exists anywhere in the call chain — neither in `Pyth._initialize()` nor in `PythUpgradable.initialize()`: [2](#0-1) 

This is inconsistent with every other upgradeable contract in the same codebase. `ExecutorUpgradable.initialize()` and `Executor._initialize()` both guard the wormhole address: [3](#0-2) 

`EntropyUpgradable.initialize()` guards `owner`, `admin`, and `defaultProvider`: [4](#0-3) 

`Entropy._initialize()` also guards `admin` and `defaultProvider`: [5](#0-4) 

The same pattern is present in the Stylus `PythReceiver.initialize()`, which also sets `self.wormhole.set(wormhole)` without a zero-address check: [6](#0-5) 

---

### Impact Explanation

The `wormhole` address is the sole trust anchor for VAA verification. Every price update call routes through `wormhole().parseAndVerifyVM(...)`. If `wormhole` is `address(0)`, all calls to `updatePriceFeeds`, `parsePriceFeedUpdates`, and related functions will revert (call to zero address), rendering the entire oracle permanently non-functional.

Critically, `PythUpgradable.initialize()` ends with `renounceOwnership()`: [7](#0-6) 

This means there is no owner who can call `upgradeTo` to fix the misconfiguration. The proxy is permanently locked to a broken implementation. The only recourse is redeployment of the entire proxy and re-migration of all state.

---

### Likelihood Explanation

The deployer must pass `address(0)` as the `wormhole` argument. This is a realistic deployment mistake — especially in scripted or automated deployments where a variable is unset or a config value is missing. The absence of a guard means the EVM will silently accept the zero address and emit no warning. The `initializer` modifier ensures this can only be called once, so there is no second chance.

---

### Recommendation

Add a zero-address check at the top of `Pyth._initialize()`, consistent with `Executor._initialize()` and `Entropy._initialize()`:

```solidity
function _initialize(
    address wormhole,
    ...
) internal {
    require(wormhole != address(0), "wormhole is zero address");
    setWormhole(wormhole);
    ...
}
```

Apply the same fix to `PythReceiver.initialize()` in the Stylus contract:

```rust
if wormhole == Address::ZERO {
    return Err(PythReceiverError::InvalidWormholeAddress.into());
}
self.wormhole.set(wormhole);
```

---

### Proof of Concept

1. Deploy `PythUpgradable` proxy and call `initialize(address(0), ...)`.
2. Observe that `renounceOwnership()` is called — no owner remains.
3. Call `updatePriceFeeds(...)` with any valid accumulator update data.
4. The call reverts because `wormhole()` returns `address(0)` and the low-level call to `parseAndVerifyVM` on the zero address returns no data / reverts.
5. No upgrade path exists; the contract is permanently bricked.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L29-30)
```text
    ) internal {
        setWormhole(wormhole);
```

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

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol (L27-53)
```text
    function initialize(
        address owner,
        address admin,
        uint128 pythFeeInWei,
        address defaultProvider,
        bool prefillRequestStorage
    ) public initializer {
        require(owner != address(0), "owner is zero address");
        require(admin != address(0), "admin is zero address");
        require(
            defaultProvider != address(0),
            "defaultProvider is zero address"
        );

        __Ownable_init();
        __UUPSUpgradeable_init();

        Entropy._initialize(
            admin,
            pythFeeInWei,
            defaultProvider,
            prefillRequestStorage
        );

        // We need to transfer the ownership from deployer to the new owner
        _transferOwnership(owner);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L76-104)
```text
    function _initialize(
        address admin,
        uint128 pythFeeInWei,
        address defaultProvider,
        bool prefillRequestStorage
    ) internal {
        require(admin != address(0), "admin is zero address");
        require(
            defaultProvider != address(0),
            "defaultProvider is zero address"
        );

        _state.admin = admin;
        _state.accruedPythFeesInWei = 0;
        _state.pythFeeInWei = pythFeeInWei;
        _state.defaultProvider = defaultProvider;

        if (prefillRequestStorage) {
            // Write some data to every storage slot in the requests array such that new requests
            // use a more consistent amount of gas.
            // Note that these requests are not live because their sequenceNumber is 0.
            for (uint8 i = 0; i < NUM_REQUESTS; i++) {
                EntropyStructsV2.Request storage req = _state.requests[i];
                req.provider = address(1);
                req.blockNumber = 1234;
                req.commitment = hex"0123";
            }
        }
    }
```

**File:** target_chains/stylus/contracts/pyth-receiver/src/lib.rs (L98-103)
```rust
    ) -> Result<(), PythReceiverError> {
        if self.initialized.get() {
            return Err(PythReceiverError::AlreadyInitialized.into());
        }
        self.wormhole.set(wormhole);
        self.single_update_fee_in_wei.set(single_update_fee_in_wei);
```
