### Title
Echo Provider Fees Permanently Locked When No Fee Manager Is Set - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol` accumulates provider fees in `_state.providers[provider].accruedFeesInWei` but provides **no direct withdrawal path for the provider itself**. The only withdrawal function for provider fees is `withdrawAsFeeManager()`, which requires a fee manager to be explicitly set. If a provider registers without calling `setFeeManager()`, their `feeManager` defaults to `address(0)` and their accrued fees are permanently locked with no recovery mechanism.

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, the provider's fee portion is credited: [1](#0-0) 

The only way to withdraw these provider-specific fees is via `withdrawAsFeeManager`: [2](#0-1) 

This function enforces `msg.sender == _state.providers[provider].feeManager`. The `feeManager` field is set only by an explicit call to `setFeeManager()`: [3](#0-2) 

If a provider never calls `setFeeManager()`, `feeManager` remains `address(0)`. Since `address(0)` can never be `msg.sender` in a real transaction, `withdrawAsFeeManager` is permanently uncallable for that provider.

Critically, the `IEcho` interface exposes **no** `withdraw(amount)` function for providers to call directly: [4](#0-3) 

This contrasts with `Entropy.sol`, which provides a direct `withdraw(amount)` callable by `msg.sender` (the provider itself): [5](#0-4) 

The `withdrawFees()` function in Echo only covers the Pyth protocol fee pool (`_state.accruedFeesInWei`), not provider fees: [6](#0-5) 

### Impact Explanation

Any Echo provider who registers and begins fulfilling callbacks without first calling `setFeeManager()` will have all their accrued ETH fees permanently locked in the contract. There is no admin override, no governance escape hatch, and no upgrade path described in the contract. The ETH is irrecoverable unless the contract is upgraded. This is a direct loss of funds for providers.

### Likelihood Explanation

The `registerProvider()` function does not require or prompt a fee manager to be set at registration time: [7](#0-6) 

A provider can register, start receiving fees, and never realize they need a separate `setFeeManager()` call to unlock withdrawals. The default provider set at initialization is particularly at risk since it is configured by the admin, not necessarily by the provider themselves. This is a realistic omission for any provider integrating with Echo.

### Recommendation

Add a direct `withdraw(uint128 amount)` function callable by the provider itself (analogous to `Entropy.sol`'s `withdraw`), so that providers who have not set a fee manager can still recover their accrued fees:

```solidity
function withdraw(uint128 amount) external {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(provider.isRegistered, "Provider not registered");
    require(provider.accruedFeesInWei >= amount, "Insufficient balance");
    provider.accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "withdrawal failed");
    emit FeesWithdrawn(msg.sender, amount);
}
```

### Proof of Concept

1. Provider calls `registerProvider(baseFee, feedFee, gasFee)` — does **not** call `setFeeManager()`.
2. Users call `requestPriceUpdatesWithCallback{value: fee}(provider, ...)`.
3. Provider calls `executeCallback(...)` — `_state.providers[provider].accruedFeesInWei` grows.
4. Provider attempts to withdraw: there is no `withdraw()` function to call.
5. Provider attempts `withdrawAsFeeManager(provider, amount)` — reverts with `"Only fee manager"` because `feeManager == address(0)` and `msg.sender != address(0)`.
6. Fees are permanently locked. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L108-116)
```text
    function setFeeManager(address manager) external;

    /**
     * @notice Allows the admin to withdraw accumulated Pyth protocol fees
     * @param amount The amount of fees to withdraw in wei
     */
    function withdrawFees(uint128 amount) external;

    function withdrawAsFeeManager(address provider, uint128 amount) external;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L151-173)
```text
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```
