### Title
Unvalidated `providerToCredit` in `executeCallback` Permanently Locks Provider Fees — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
In `Echo.sol`, the `executeCallback` function accepts a caller-supplied `providerToCredit` address that is only validated against `req.provider` during the exclusivity period. After the exclusivity period, any address — including an unregistered one — can be passed. Fees are unconditionally credited to `_state.providers[providerToCredit].accruedFeesInWei`. If `providerToCredit` is an unregistered address, no withdrawal path exists, permanently locking the provider's earned fees in the contract.

### Finding Description
When a user calls `requestPriceUpdatesWithCallback`, the provider's fee is stored in `req.fee` and the request is associated with `req.provider`. [1](#0-0) 

In `executeCallback`, the only guard on `providerToCredit` is: [2](#0-1) 

After the exclusivity period elapses, this guard is skipped entirely. Fees are then credited to the caller-supplied address with no registration check: [3](#0-2) 

The only withdrawal paths for provider fees are `withdrawAsFeeManager`, which requires `msg.sender == _state.providers[provider].feeManager`: [4](#0-3) 

For an unregistered address, `feeManager` is `address(0)`, so `msg.sender == address(0)` is never satisfiable. The fees are permanently locked. Additionally, `clearRequest` is called before the fee credit, so the legitimate provider cannot re-fulfill the request: [5](#0-4) 

### Impact Explanation
Provider fees earned from fulfilled requests are permanently locked in the contract when `executeCallback` is called with an unregistered `providerToCredit` address. The legitimate provider (`req.provider`) loses all fees for that request with no recovery path. This is a direct analog to the original report: just as `harvestYield()` used the wrong token address causing rewards to be unclaimable, `executeCallback` credits fees to the wrong address causing them to be unwithdrawable.

### Likelihood Explanation
Any unprivileged transaction sender can call `executeCallback` after the exclusivity period elapses. Price update data is publicly available from Hermes. The attacker only needs to wait for the exclusivity window to pass, fetch the update data, and call `executeCallback(address(0xdead), sequenceNumber, updateData, priceIds)`. No special privileges, keys, or governance access are required.

### Recommendation
Add a validation check in `executeCallback` that `providerToCredit` is a registered provider:

```solidity
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit must be a registered provider"
);
```

This ensures fees can only be credited to addresses that have a valid withdrawal path.

### Proof of Concept

```solidity
// 1. User requests price update from legitimateProvider, paying fee
echo.requestPriceUpdatesWithCallback{value: fee}(
    legitimateProvider, publishTime, priceIds, gasLimit
); // sequenceNumber = N

// 2. Wait for exclusivity period to pass
vm.warp(block.timestamp + exclusivityPeriod + 1);

// 3. Attacker calls executeCallback with a garbage address
// (no registration required for providerToCredit)
echo.executeCallback(
    address(0xdead),   // unregistered address — no feeManager set
    N,
    updateData,
    priceIds
);

// 4. Fees are now in _state.providers[address(0xdead)].accruedFeesInWei
// No one can call withdrawAsFeeManager(address(0xdead), ...) because
// _state.providers[address(0xdead)].feeManager == address(0)
// legitimateProvider's fees are permanently locked.
assertEq(echo.getProviderInfo(address(0xdead)).accruedFeesInWei, expectedFee);
// legitimateProvider received nothing
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L83-84)
```text
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L164-164)
```text
        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-378)
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
```
