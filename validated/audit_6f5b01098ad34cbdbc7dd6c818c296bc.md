### Title
Fees Credited to Unregistered `providerToCredit` Are Permanently Locked — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

In `Echo.sol`, `executeCallback` credits provider fees to an arbitrary caller-supplied `providerToCredit` address with no check that the address is a registered provider. Because the only withdrawal path for provider-accrued fees in Echo requires a `feeManager` (set via `setFeeManager`, which itself requires `isRegistered == true`), fees credited to any unregistered address are permanently locked in the contract with no recovery path.

### Finding Description

`executeCallback` accepts a caller-controlled `providerToCredit` parameter and unconditionally increments that address's `accruedFeesInWei`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [1](#0-0) 

There is no check that `providerToCredit` is a registered provider. The only validation is an exclusivity-period check that restricts `providerToCredit` to `req.provider` during the exclusivity window — but after that window expires, any address may be passed. [2](#0-1) 

The only withdrawal path for provider fees in Echo is `withdrawAsFeeManager`, which requires `msg.sender == _state.providers[provider].feeManager`: [3](#0-2) 

For an unregistered address, `feeManager` is `address(0)` (the default). The only way to set a `feeManager` is via `setFeeManager`, which requires `isRegistered == true`: [4](#0-3) 

Unlike Entropy, Echo has no direct `withdraw()` function for providers themselves — only `withdrawAsFeeManager` and the admin-only `withdrawFees` (which drains `_state.accruedFeesInWei`, the Pyth protocol fee pool, not provider fees): [5](#0-4) 

The `ProviderInfo` struct confirms `feeManager` defaults to `address(0)` for any address that has never called `registerProvider`: [6](#0-5) 

### Impact Explanation

Any ETH paid by users as provider fees (the `req.fee` portion of `msg.value`) that gets credited to an unregistered `providerToCredit` address is permanently locked in the Echo contract. There is no admin escape hatch, no recovery function, and no way for the unregistered address to set a fee manager. The locked ETH is real user funds paid at request time.

### Likelihood Explanation

After the `exclusivityPeriodSeconds` window expires, `executeCallback` is open to any caller with any `providerToCredit`. A malicious or careless caller can pass an arbitrary address (e.g., `address(0xdead)`, a contract address, or any EOA that has not called `registerProvider`). This is reachable by any unprivileged transaction sender with no special access required.

### Recommendation

Add a registration check in `executeCallback` before crediting fees:

```solidity
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit not registered"
);
```

Alternatively, add a direct `withdraw()` function for providers (analogous to Entropy's `withdraw`) so that any address with a non-zero `accruedFeesInWei` can self-withdraw without needing `isRegistered`.

### Proof of Concept

```solidity
// 1. User requests a price update with a registered provider
uint64 seq = echo.requestPriceUpdatesWithCallback{value: totalFee}(
    registeredProvider, publishTime, priceIds, callbackGasLimit
);

// 2. Wait for exclusivity period to expire (warp time)
vm.warp(block.timestamp + exclusivityPeriod + 1);

// 3. Anyone calls executeCallback with an unregistered address as providerToCredit
address unregistered = address(0xdead);
echo.executeCallback(unregistered, seq, updateData, priceIds);

// 4. Fees are now credited to `unregistered`
EchoState.ProviderInfo memory info = echo.getProviderInfo(unregistered);
assertGt(info.accruedFeesInWei, 0); // fees are stuck

// 5. `unregistered` cannot set a fee manager (not registered)
vm.prank(unregistered);
vm.expectRevert("Provider not registered");
echo.setFeeManager(unregistered);

// 6. withdrawAsFeeManager fails because feeManager == address(0)
vm.prank(address(0)); // impossible in practice
// No path to recover the fees — permanently locked
```

### Citations

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
