### Title
No Cancellation/Refund Mechanism for Unfulfilled Echo Requests Permanently Locks User Funds - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `Echo` contract collects fees from users when they call `requestPriceUpdatesWithCallback`, but provides no mechanism to cancel a request or refund the fee if the request is never fulfilled. If `executeCallback` cannot be completed (e.g., because no valid price data exists at the exact requested `publishTime`, or because the provider never calls it), the user's ETH is permanently locked in the contract with no recovery path.

### Finding Description

When a user calls `requestPriceUpdatesWithCallback`, they pay a fee that is split between the Pyth protocol fee (immediately credited to `_state.accruedFeesInWei`) and the provider fee (stored in `req.fee`): [1](#0-0) 

The only way to release these funds is through `executeCallback`. Inside `executeCallback`, the provider's fee is credited and the request is cleared **before** the callback is attempted: [2](#0-1) 

The developers themselves acknowledged this risk in a TODO comment at line 155–156. The `executeCallback` function calls `parsePriceFeedUpdates` with `req.publishTime` as **both** `minPublishTime` and `maxPublishTime`, requiring an exact timestamp match: [3](#0-2) 

Another developer TODO at line 143 acknowledges this may be too restrictive: `"// TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?"`. If no Pyth price update exists at the exact `publishTime` second, `parsePriceFeedUpdates` will revert, causing `executeCallback` to revert, leaving the request permanently active and the user's funds permanently locked.

There is no `cancelRequest`, `refundRequest`, or any other recovery function in the Echo contract. The `withdrawFees` function is admin-only and only covers the Pyth protocol fee portion. The `withdrawAsFeeManager` function only covers provider-accrued fees. Neither can return funds to the original requester. [4](#0-3) 

### Impact Explanation

User ETH paid as fees to `requestPriceUpdatesWithCallback` becomes permanently locked in the Echo contract if:
1. The assigned provider never calls `executeCallback` (no liveness guarantee or penalty mechanism — also acknowledged in the TODO at line 157–159), or
2. `parsePriceFeedUpdates` always reverts because no Pyth price update exists at the exact `publishTime` second.

The Pyth fee portion (`_state.accruedFeesInWei`) is immediately credited and can be withdrawn by the admin, but the provider fee portion (`req.fee`) has no recovery path for the user. Funds are effectively lost.

### Likelihood Explanation

Any user of the Echo contract who submits a request that goes unfulfilled is affected. The exact-timestamp match in `parsePriceFeedUpdates` (same value for both min and max) is a realistic failure mode — if the Pyth network's published timestamp for a given slot does not exactly match the user's `publishTime` (e.g., due to rounding, network delay, or market closure), `executeCallback` will always revert. Additionally, there is no on-chain incentive or penalty mechanism to ensure providers fulfill requests, making provider non-fulfillment a realistic scenario.

### Recommendation

1. Add a `cancelRequest(uint64 sequenceNumber)` function that allows the original requester to reclaim their fee after a timeout period (e.g., after `publishTime + some_grace_period` has passed with no fulfillment).
2. Change the `parsePriceFeedUpdates` call to use a range (e.g., `[req.publishTime, req.publishTime + 1]`) rather than an exact timestamp match, as the TODO comment at line 143 already suggests.
3. Consider adding a penalty mechanism for providers who fail to fulfill requests within the exclusivity period, as noted in the TODO at line 157–159.

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback` with `publishTime = block.timestamp`, paying `totalFee` ETH. The fee is stored: `req.fee = msg.value - pythFeeInWei`.
2. The assigned provider attempts `executeCallback` but `pyth.parsePriceFeedUpdates` reverts because no Pyth price update exists at the exact `publishTime` second (e.g., the closest update was published 1 second earlier).
3. `executeCallback` reverts; the request remains active.
4. No other function exists to cancel the request or refund the user.
5. The user's ETH is permanently locked in the Echo contract. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-379)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }

    function findActiveRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        req = findRequest(sequenceNumber);

        if (!isActive(req) || req.sequenceNumber != sequenceNumber)
            revert NoSuchRequest();
    }

    function findRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }

    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }

    function allocRequest(
        uint64 sequenceNumber
    ) internal returns (Request storage req) {
        (, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            (bytes32 reqKey, ) = requestKey(req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }

    function isActive(Request memory req) internal pure returns (bool) {
        return req.sequenceNumber != 0;
    }

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
