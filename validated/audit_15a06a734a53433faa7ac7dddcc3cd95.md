### Title
Missing Request Cancellation Mechanism Causes Permanent ETH Lock When `executeCallback()` Is Unfulfillable — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback()` accepts ETH from users and stores it in the contract. There is no cancel or refund function. If the stored `publishTime` is one for which no Pyth price data will ever exist, `executeCallback()` will always revert (because `parsePriceFeedUpdates` requires an exact timestamp match), and the user's ETH is permanently locked with no recovery path.

---

### Finding Description

`requestPriceUpdatesWithCallback()` accepts `msg.value` from any caller and stores it in a `Request` struct:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

The Pyth protocol fee portion is immediately credited to `_state.accruedFeesInWei`:

```solidity
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [2](#0-1) 

The remaining fee (`req.fee`) stays locked in the contract until `executeCallback()` is called. Inside `executeCallback()`, the Pyth contract is called with an exact timestamp window:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)   // minPublishTime == maxPublishTime
);
``` [3](#0-2) 

`parsePriceFeedUpdates` requires a price update at **exactly** `req.publishTime`. If no such update exists (e.g., the timestamp falls between two Pyth publish slots), the call reverts, `executeCallback()` reverts, and the request remains active but permanently unfulfillable.

The developers themselves flagged this in a TODO comment:

```solidity
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
``` [4](#0-3) 

There is no `cancelRequest()`, no user-facing refund function, and no timeout-based recovery. The only ETH withdrawal paths are `withdrawFees()` (admin-only, for Pyth protocol fees) and `withdrawAsFeeManager()` (fee manager only, for provider fees) — neither of which can recover a user's locked request fee. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback()` with a `publishTime` for which no Pyth price data will ever be available (e.g., a timestamp between two consecutive Pyth publish slots, or a timestamp in the past for which data has been pruned) will have their ETH permanently locked in the `Echo` contract. There is no on-chain mechanism to recover it. The impact is direct, irreversible loss of user funds.

---

### Likelihood Explanation

`publishTime` is a user-supplied `uint64` parameter accepted up to `block.timestamp + 60`. Pyth price feeds are published at discrete intervals; any `publishTime` that does not align with an actual Pyth slot will cause `parsePriceFeedUpdates` to revert. A user who misunderstands the semantics of `publishTime` (treating it as "I want a price no older than X" rather than "I want a price at exactly X") will trigger this condition. Additionally, if the assigned provider goes offline or is deregistered, no one will ever call `executeCallback()`, and the ETH is locked indefinitely.

---

### Recommendation

1. **Add a `cancelRequest()` function** that allows the original requester to reclaim their fee after a timeout (e.g., if the request has not been fulfilled within N seconds of `publishTime`).
2. **Relax the timestamp window** in `executeCallback()` — use `minPublishTime <= req.publishTime` and `maxPublishTime >= req.publishTime` rather than requiring an exact match, so that the nearest available price update can satisfy the request.
3. **Emit a warning or revert** in `requestPriceUpdatesWithCallback()` if `publishTime` is already in the past by more than the Pyth publish interval, since such requests are likely unfulfillable.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(provider, publishTime=T, priceIds, gasLimit)` with `msg.value = fee`. `T` is a timestamp between two consecutive Pyth price updates (e.g., `T = lastSlotTime + 1`).
2. Provider calls `executeCallback(provider, sequenceNumber, updateData, priceIds)` with the nearest available update data.
3. Inside `executeCallback()`, `pyth.parsePriceFeedUpdates(updateData, priceIds, T, T)` reverts because no price exists at exactly `T`.
4. `executeCallback()` reverts. The request remains active.
5. No valid `updateData` will ever satisfy `minPublishTime = maxPublishTime = T`, so `executeCallback()` will always revert for this request.
6. The user's ETH (`req.fee`) is permanently locked in the `Echo` contract with no recovery path. [7](#0-6) [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-202)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
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
    }
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
