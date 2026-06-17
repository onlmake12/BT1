### Title
Echo Provider Fee Withdrawal Blocked When No Fee Manager Is Set — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `Echo` contract provides only one mechanism for providers to withdraw their accrued fees: `withdrawAsFeeManager`. There is no direct `withdraw(amount)` function for providers (unlike the `Entropy` contract). When a provider registers and accrues fees without first calling `setFeeManager`, their `feeManager` field defaults to `address(0)`, causing every call to `withdrawAsFeeManager` to revert. The provider's accrued fees are permanently inaccessible until they discover and execute the extra `setFeeManager` step.

### Finding Description

In `Entropy.sol`, providers have two independent withdrawal paths:

1. `withdraw(uint128 amount)` — callable directly by the provider (`msg.sender` is the provider).
2. `withdrawAsFeeManager(address provider, uint128 amount)` — callable by a designated fee manager. [1](#0-0) 

In `Echo.sol`, the direct `withdraw` path is entirely absent. The only provider-facing withdrawal function is `withdrawAsFeeManager`: [2](#0-1) 

`registerProvider` never initialises `feeManager`, so it defaults to `address(0)`: [3](#0-2) 

The `withdrawAsFeeManager` guard is:

```solidity
require(
    msg.sender == _state.providers[provider].feeManager,
    "Only fee manager"
);
```

When `feeManager == address(0)`, this condition is `msg.sender == address(0)`, which is always `false`. Every withdrawal attempt reverts with `"Only fee manager"`.

The `IEcho` interface confirms there is no `withdraw(uint128)` for providers: [4](#0-3) 

### Impact Explanation

Providers who register and begin accruing fees without explicitly calling `setFeeManager` cannot withdraw any of their accrued fees. The funds are locked in the contract until the provider discovers the requirement and calls `setFeeManager`. This is a direct loss of expected income for Echo providers, mirroring the ERC1155 royalty-claim failure in the reference report.

### Likelihood Explanation

Any provider who follows the `registerProvider` → `executeCallback` flow without reading the fee-manager requirement will be affected. The `registerProvider` function gives no indication that a separate `setFeeManager` call is required before withdrawal is possible. The default post-registration state silently blocks all withdrawals.

### Recommendation

Add a direct `withdraw(uint128 amount)` function to `Echo.sol`, identical in structure to the one in `Entropy.sol`, allowing providers to withdraw their own accrued fees without requiring a fee manager:

```solidity
function withdraw(uint128 amount) external {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(provider.isRegistered, "Provider not registered");
    require(provider.accruedFeesInWei >= amount, "Insufficient balance");
    provider.accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "withdrawal to msg.sender failed");
    emit FeesWithdrawn(msg.sender, amount);
}
```

### Proof of Concept

1. Provider calls `registerProvider(baseFee, feePerFeed, feePerGas)`. `feeManager` is `address(0)`.
2. Users call `requestPriceUpdatesWithCallback`; provider fulfils via `executeCallback`. `accruedFeesInWei` grows.
3. Provider calls `withdrawAsFeeManager(providerAddress, amount)`.
4. Check `msg.sender == _state.providers[provider].feeManager` evaluates to `msg.sender == address(0)` → **reverts with "Only fee manager"**.
5. Provider has no alternative withdrawal path. Fees are inaccessible. [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-164)
```text
    function withdraw(uint128 amount) public override {
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L108-120)
```text
    function setFeeManager(address manager) external;

    /**
     * @notice Allows the admin to withdraw accumulated Pyth protocol fees
     * @param amount The amount of fees to withdraw in wei
     */
    function withdrawFees(uint128 amount) external;

    function withdrawAsFeeManager(address provider, uint128 amount) external;

    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
```
