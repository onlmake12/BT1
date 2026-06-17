### Title
Unvalidated `providerToCredit` in `Echo.executeCallback` Allows Fee Theft After Exclusivity Period - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback` accepts a caller-supplied `providerToCredit` address and credits all request fees to it without validating that the address is a registered provider, once the exclusivity window has elapsed. Because `registerProvider` is permissionless, any attacker can register, wait 15 seconds, and redirect the full fee balance of any pending request to themselves.

### Finding Description

`executeCallback` enforces one constraint on `providerToCredit`: during the exclusivity window it must equal `req.provider`. After that window closes, the parameter is used directly to credit fees with no further check:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
// ... price validation ...
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is no `require(_state.providers[providerToCredit].isRegistered, ...)` guard after the exclusivity check. The `ProviderInfo` mapping silently initialises to zero for any address, so the credit succeeds for any address the caller supplies.

`registerProvider` is fully permissionless:

```solidity
function registerProvider(
    uint96 baseFeeInWei,
    uint96 feePerFeedInWei,
    uint96 feePerGasInWei
) external override {
    ProviderInfo storage provider = _state.providers[msg.sender];
    require(!provider.isRegistered, "Provider already registered");
    provider.isRegistered = true;
    ...
}
```

After registering, the attacker calls `setFeeManager(attackerAddress)` to make themselves the fee manager of their own provider entry, then calls `withdrawAsFeeManager` to drain the credited balance.

### Impact Explanation

An attacker can steal the full fee paid by any requester for any request whose exclusivity period has elapsed. The stolen ETH is the sum `req.fee + msg.value - pythFee` for each hijacked request. Legitimate providers who were supposed to fulfill requests receive nothing. Because the exclusivity period defaults to 15 seconds and `executeCallback` is open to any caller, every unfulfilled request older than 15 seconds is a target. This constitutes direct, repeatable theft of provider revenue from the Echo contract.

### Likelihood Explanation

The exclusivity period is 15 seconds by default. Any address can register as a provider at zero cost. No privileged access, leaked key, or governance action is required. The attacker only needs to monitor the chain for pending requests and submit a transaction after the exclusivity window. This is trivially automatable and economically rational whenever the fee exceeds gas cost.

### Recommendation

Add a registration check on `providerToCredit` inside `executeCallback`, mirroring the check already present in `requestPriceUpdatesWithCallback` for the `provider` parameter:

```solidity
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit not registered"
);
```

This ensures only legitimate, registered providers can receive fees, consistent with the invariant enforced at request time.

### Proof of Concept

1. Attacker calls `registerProvider(0, 0, 0)` — sets `_state.providers[attacker].isRegistered = true` at zero cost. [1](#0-0) 

2. Attacker calls `setFeeManager(attacker)` — sets `_state.providers[attacker].feeManager = attacker`. [2](#0-1) 

3. A legitimate user calls `requestPriceUpdatesWithCallback(legitimateProvider, publishTime, priceIds, gasLimit)` paying `req.fee` in ETH. The request is stored with `req.provider = legitimateProvider`. [3](#0-2) 

4. After `exclusivityPeriodSeconds` (15 s default) elapses, the exclusivity check is skipped. Attacker calls `executeCallback(attacker, sequenceNumber, updateData, priceIds)`. [4](#0-3) 

5. With no registration guard, `_state.providers[attacker].accruedFeesInWei += (req.fee + msg.value) - pythFee` executes, crediting the full fee to the attacker. [5](#0-4) 

6. Attacker calls `withdrawAsFeeManager(attacker, amount)`. The check `msg.sender == _state.providers[attacker].feeManager` passes (both are `attacker`), and the ETH is transferred out. [6](#0-5) 

The legitimate provider receives zero fees; the attacker receives the full request fee.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
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
