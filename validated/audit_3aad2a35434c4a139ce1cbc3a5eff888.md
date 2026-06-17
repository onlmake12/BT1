### Title
Missing Zero-Length `priceIds` Validation Allows Fee Loss and Potential Fund Lock in Echo Contract - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.requestPriceUpdatesWithCallback` validates that `priceIds.length` does not exceed `MAX_PRICE_IDS` but performs no lower-bound check. A caller can submit `priceIds = []`, pay the full fee, and receive either a callback with empty price data (fee drained to provider with zero value delivered) or, if `IPyth.parsePriceFeedUpdates` reverts on empty input, a permanently stuck request whose fee can never be recovered.

### Finding Description

`Echo.requestPriceUpdatesWithCallback` accepts a `bytes32[] calldata priceIds` parameter and enforces only an upper bound:

```solidity
if (priceIds.length > MAX_PRICE_IDS) {
    revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
}
``` [1](#0-0) 

No lower-bound check (`priceIds.length == 0`) exists. The function then proceeds to:

1. Compute a fee that includes `0 * feePerFeedInWei` (zero per-feed component) but still charges `pythFeeInWei + providerBaseFee + callbackGasLimit * feePerGasInWei`.
2. Store the request with `req.priceIdPrefixes = new bytes8[](0)`.
3. Emit `PriceUpdateRequested(req, priceIds)` with an empty `priceIds` array.
4. Consume `msg.value`. [2](#0-1) 

In `executeCallback`, the length-match check passes trivially (`0 == 0`), and `IPyth.parsePriceFeedUpdates` is called with empty `updateData` and empty `priceIds`: [3](#0-2) 

**Path A – `parsePriceFeedUpdates` reverts on empty input:** `executeCallback` reverts entirely. The lines that credit the provider and clear the request (lines 161–164) are never reached. The request remains active indefinitely with no user-facing refund path. The fee is permanently locked in the contract.

**Path B – `parsePriceFeedUpdates` returns `[]`:** The callback is invoked with an empty `priceFeeds` array. The fee is credited to the provider and the request is cleared. The user paid a non-trivial fee and received zero price data. [4](#0-3) 

By contrast, the Pulse `Scheduler` contract explicitly guards against this with `EmptyPriceIds`: [5](#0-4) 

The fee calculation confirms the per-feed component is zero when `priceIds.length == 0`, so the fee check does not act as an implicit guard: [6](#0-5) 

### Impact Explanation

- **Path A (revert):** User's ETH is permanently locked in the Echo contract. There is no `cancelRequest` or refund function. Severity: High (fund loss).
- **Path B (empty return):** User pays `pythFeeInWei + providerBaseFee + callbackGasLimit * feePerGasInWei` and receives a callback with zero price feeds. The consumer contract may mishandle an empty array (e.g., array-out-of-bounds, silent no-op). Severity: Medium (fee drain, consumer-side breakage).
- **Event integrity:** `PriceUpdateRequested` is emitted with an empty `priceIds` field, misleading off-chain subscribers and indexers that monitor this event to track active requests.

### Likelihood Explanation

Any unprivileged caller can invoke `requestPriceUpdatesWithCallback` with `priceIds = new bytes32[](0)`. No special role is required. The call succeeds as long as `msg.value >= requiredFee` (which is non-zero due to `pythFeeInWei + providerBaseFee`). A user could trigger this accidentally (e.g., passing an uninitialized array) or a malicious actor could use it to grief the contract's request storage or lock funds.

### Recommendation

Add a lower-bound check mirroring the Scheduler's guard, immediately after the upper-bound check:

```solidity
if (priceIds.length == 0) {
    revert EmptyPriceIds();
}
if (priceIds.length > MAX_PRICE_IDS) {
    revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
}
```

### Proof of Concept

```solidity
// Attacker/user calls with empty priceIds
bytes32[] memory emptyIds = new bytes32[](0);
uint96 fee = echo.getFee(provider, callbackGasLimit, emptyIds);
// fee = pythFeeInWei + providerBaseFee + callbackGasLimit * feePerGasInWei (non-zero)

uint64 seq = echo.requestPriceUpdatesWithCallback{value: fee}(
    provider,
    uint64(block.timestamp),
    emptyIds,          // <-- no validation, passes
    callbackGasLimit
);
// Request is stored, fee consumed, PriceUpdateRequested emitted with empty priceIds.
// executeCallback will either revert (fee stuck) or deliver empty priceFeeds (fee drained, zero value).
``` [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L123-153)
```text
        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-201)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }

        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
        {
            // Callback succeeded
            emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
        } catch Error(string memory reason) {
            // Explicit revert/require
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                reason
            );
        } catch {
            // Out of gas or other low-level errors
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                "low-level error (possibly out of gas)"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L244-254)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L160-163)
```text
        // No zero‐feed subscriptions
        if (params.priceIds.length == 0) {
            revert SchedulerErrors.EmptyPriceIds();
        }
```
