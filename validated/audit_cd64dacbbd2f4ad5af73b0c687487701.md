### Title
Missing Zero-Address Validation in `setFeeManager` Allows Provider to Permanently Brick Fee Manager Role - (File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol)

### Summary
`Entropy.sol`'s `setFeeManager(address manager)` and `Echo.sol`'s `setFeeManager(address manager)` accept `address(0)` without any validation. Any registered provider can call `setFeeManager(address(0))`, permanently removing the fee manager's ability to withdraw accrued fees or update provider fees on the provider's behalf.

### Finding Description
`Entropy.sol` at line 876 implements `setFeeManager`:

```solidity
function setFeeManager(address manager) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) {
        revert EntropyErrors.NoSuchProvider();
    }
    address oldFeeManager = provider.feeManager;
    provider.feeManager = manager;   // ← no require(manager != address(0))
    emit ProviderFeeManagerUpdated(msg.sender, oldFeeManager, manager);
    ...
}
``` [1](#0-0) 

There is no `require(manager != address(0))` guard. Once `provider.feeManager` is set to `address(0)`, the two functions that gate on `feeManager` become permanently inaccessible:

**`withdrawAsFeeManager`** checks:
```solidity
if (providerInfo.feeManager != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
``` [2](#0-1) 

**`setProviderFeeAsFeeManager`** checks:
```solidity
if (providerInfo.feeManager != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
``` [3](#0-2) 

Since `msg.sender` can never equal `address(0)` in a normal EVM call, both functions become permanently unreachable for the fee manager once `feeManager` is set to `address(0)`.

The identical pattern exists in `Echo.sol`:

```solidity
function setFeeManager(address manager) external override {
    require(_state.providers[msg.sender].isRegistered, "Provider not registered");
    address oldFeeManager = _state.providers[msg.sender].feeManager;
    _state.providers[msg.sender].feeManager = manager;  // ← no zero-address check
    emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
}
``` [4](#0-3) 

By contrast, the governance-level setters in `EntropyGovernance.sol` correctly validate against `address(0)`: [5](#0-4) 

### Impact Explanation
A registered provider who has delegated fee management to a third-party fee manager (e.g., a multisig, a revenue-sharing contract, or a business partner) can call `setFeeManager(address(0))` — either accidentally or maliciously — and permanently revoke the fee manager's ability to:
1. Withdraw accrued provider fees via `withdrawAsFeeManager`.
2. Update the provider's fee schedule via `setProviderFeeAsFeeManager`.

The fee manager's accrued withdrawal rights are lost. While the provider can still call `withdraw()` directly, the fee manager has no recourse to recover their delegated access. Any business arrangement relying on the fee manager role is irreversibly broken.

### Likelihood Explanation
Provider registration is permissionless — any address can call `register()` and become a provider. The `setFeeManager` function is callable by any registered provider with no additional access control. The zero-address case can be triggered accidentally (e.g., passing an uninitialized variable) or deliberately (griefing a fee manager). Likelihood is medium: the scenario requires a provider to have previously set a fee manager, but once that relationship exists, a single mistaken or malicious call permanently breaks it.

### Recommendation
Add a zero-address guard in both `Entropy.sol` and `Echo.sol`:

```solidity
function setFeeManager(address manager) external override {
    require(manager != address(0), "feeManager cannot be zero address");
    ...
}
```

If intentional removal of a fee manager is a desired feature, introduce a dedicated `removeFeeManager()` function with explicit semantics, rather than allowing `address(0)` to silently disable the role.

### Proof of Concept
1. Provider `P` registers via `register(...)` in `Entropy.sol`.
2. `P` calls `setFeeManager(feeManagerAddr)` to delegate fee management to `FM`.
3. `FM` accumulates the right to call `withdrawAsFeeManager(P, amount)`.
4. `P` calls `setFeeManager(address(0))`.
5. `FM` calls `withdrawAsFeeManager(P, amount)` → reverts with `Unauthorized` because `providerInfo.feeManager` (`address(0)`) `!= msg.sender` (`FM`).
6. `FM` can never recover their delegated access. `P` retains full access via `withdraw()`.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L187-189)
```text
        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L841-843)
```text
        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L876-893)
```text
    function setFeeManager(address manager) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];
        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        address oldFeeManager = provider.feeManager;
        provider.feeManager = manager;
        emit ProviderFeeManagerUpdated(msg.sender, oldFeeManager, manager);
        emit EntropyEventsV2.ProviderFeeManagerUpdated(
            msg.sender,
            oldFeeManager,
            manager,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L350-358)
```text
    function setFeeManager(address manager) external override {
        require(
            _state.providers[msg.sender].isRegistered,
            "Provider not registered"
        );
        address oldFeeManager = _state.providers[msg.sender].feeManager;
        _state.providers[msg.sender].feeManager = manager;
        emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L83-94)
```text
    function setDefaultProvider(address newDefaultProvider) external {
        require(
            newDefaultProvider != address(0),
            "newDefaultProvider is zero address"
        );
        _authoriseAdminAction();

        address oldDefaultProvider = _state.defaultProvider;
        _state.defaultProvider = newDefaultProvider;

        emit DefaultProviderSet(oldDefaultProvider, newDefaultProvider);
    }
```
