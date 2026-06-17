### Title
Excess Native Token Sent to `Echo::requestPriceUpdatesWithCallback` Is Not Refunded to the Caller — (File: target_chains/ethereum/contracts/contracts/echo/Echo.sol)

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback` accepts `msg.value`, validates it against a computed `requiredFee`, but then stores the **entire** `msg.value - pythFeeInWei` as `req.fee` — the provider's claimable balance. Any ETH sent above `requiredFee` silently accrues to the provider rather than being returned to the caller. Unlike `PythLazer.sol`'s `verifyUpdate`, which explicitly refunds excess, `Echo.sol` has no such mechanism.

### Finding Description

In `requestPriceUpdatesWithCallback`:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);   // <-- all excess captured here
...
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [1](#0-0) 

`getFee` returns `pythFeeInWei + providerBaseFee + providerFeedFee + callbackGasLimit * feePerGasInWei`. [2](#0-1) 

When `msg.value > requiredFee` by any amount `Δ`:
- Pyth accrues exactly `pythFeeInWei` (correct).
- `req.fee` becomes `(providerBaseFee + providerFeedFee + gasFee) + Δ`.
- On `executeCallback`, the provider is credited `req.fee + msg.value - pythFee`, so the provider receives the full overpayment `Δ`. [3](#0-2) 

The caller receives nothing back. There is no `if (msg.value > requiredFee) payable(msg.sender).transfer(...)` guard, in contrast to `PythLazer.sol` which does perform this refund: [4](#0-3) 

### Impact Explanation

Any user who overpays — whether due to a fee change between `getFee` and the actual call, a front-end rounding error, or a deliberate extra buffer — permanently loses the excess ETH to the provider. The provider can then withdraw it via `withdrawAsFeeManager`. [5](#0-4) 

The loss is bounded by the overpayment amount per transaction, not the total contract balance, placing this in the Medium severity range (analogous to the referenced report's downgrade from High to Medium for "losses limited to gas refunds").

### Likelihood Explanation

This is reachable by any unprivileged user calling `requestPriceUpdatesWithCallback`. Overpayment is common in practice: on-chain fee parameters (`feePerGasInWei`, `baseFeeInWei`) can be updated by the provider between a user's `getFee` read and their transaction landing, causing the user to send a stale (higher) estimate. The `setProviderFee` function has no timelock. [6](#0-5) 

### Recommendation

Add an excess-refund guard in `requestPriceUpdatesWithCallback`, mirroring the pattern already used in `PythLazer.sol`:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
if (msg.value > requiredFee) {
    payable(msg.sender).transfer(msg.value - requiredFee);
}
// then store only the exact provider portion:
req.fee = requiredFee - _state.pythFeeInWei;
_state.accruedFeesInWei += _state.pythFeeInWei;
```

### Proof of Concept

1. Provider registers with `baseFeeInWei = 0`, `feePerFeedInWei = 0`, `feePerGasInWei = 1`.
2. `getFee(provider, 100_000, [id])` returns `pythFeeInWei + 100_000`.
3. User calls `requestPriceUpdatesWithCallback{value: pythFeeInWei + 200_000}(...)` (sends 100_000 extra).
4. `req.fee = 200_000` (instead of the correct `100_000`).
5. Provider calls `executeCallback`; `accruedFeesInWei` for provider increases by `200_000 - pythFee`.
6. Provider withdraws the extra 100_000 wei via `withdrawAsFeeManager`.
7. User's 100_000 wei overpayment is permanently lost. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L235-254)
```text
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) public view override returns (uint96 feeAmount) {
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L361-378)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-420)
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
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L73-77)
```text
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
