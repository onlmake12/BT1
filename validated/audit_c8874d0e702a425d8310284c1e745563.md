### Title
Echo Provider Fee Parameters Lack Upper Bound Validation, Enabling Arithmetic Overflow in `getFee` - (File: target_chains/ethereum/contracts/contracts/echo/Echo.sol)

### Summary
`Echo.registerProvider` and `Echo.setProviderFee` accept fee parameters (`baseFeeInWei`, `feePerFeedInWei`, `feePerGasInWei`) as raw `uint96` values with no upper-bound validation. The `getFee` function performs unchecked arithmetic on these values. Setting `feePerGasInWei` to a sufficiently large value causes `getFee` — and therefore `requestPriceUpdatesWithCallback` — to revert for every caller, permanently bricking that provider's service.

### Finding Description
`registerProvider` stores all three fee components without any range check:

```solidity
function registerProvider(
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    provider.baseFeeInWei    = baseFeeInWei;
    provider.feePerFeedInWei = feePerFeedInWei;
    provider.feePerGasInWei  = feePerGasInWei;
    provider.isRegistered    = true;
    ...
}
``` [1](#0-0) 

`getFee` then multiplies the caller-supplied `callbackGasLimit` (`uint32`) by the stored `feePerGasInWei` (`uint96`):

```solidity
uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei;
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [2](#0-1) 

In Solidity 0.8+, `uint32 * uint96` is evaluated in the common type `uint96`. If `callbackGasLimit * feePerGasInWei > type(uint96).max`, the multiplication reverts. Even if the compiler widens to `uint256` first, `SafeCast.toUint96(gasFee)` reverts when `gasFee > type(uint96).max`. Either path produces a revert.

The same pattern applies to `feePerFeedInWei`:

```solidity
uint96 providerFeedFee = SafeCast.toUint96(
    priceIds.length * _state.providers[provider].feePerFeedInWei
);
``` [3](#0-2) 

`setProviderFee` has the identical absence of validation: [4](#0-3) 

### Impact Explanation
Any address can call `registerProvider` and set `feePerGasInWei = type(uint96).max`. For any `callbackGasLimit >= 2`, `getFee` reverts. Because `requestPriceUpdatesWithCallback` calls `getFee` internally:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
``` [5](#0-4) 

every user request to that provider reverts. If the admin subsequently designates this provider as the default via `setDefaultProvider`, the entire Echo contract's default-provider path becomes permanently unusable until the admin intervenes. The `EchoState.ProviderInfo` struct confirms all three fee fields are plain `uint96` with no stored cap: [6](#0-5) 

### Likelihood Explanation
Any unprivileged address can register as a provider with extreme fees in a single transaction. The admin then only needs to call `setDefaultProvider` (a routine operational action) to trigger the DoS. Even without

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-76)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L245-247)
```text
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L248-254)
```text
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-426)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L31-46)
```text
    struct ProviderInfo {
        // Slot 1: 12 + 12 + 8 = 32 bytes
        uint96 baseFeeInWei;
        uint96 feePerFeedInWei;
        // 8 bytes padding

        // Slot 2: 12 + 16 + 4 = 32 bytes
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
        // 4 bytes padding

        // Slot 3: 20 + 1 + 11 = 32 bytes
        address feeManager;
        bool isRegistered;
        // 11 bytes padding
    }
```
