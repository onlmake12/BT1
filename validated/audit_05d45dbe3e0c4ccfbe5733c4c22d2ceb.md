### Title
No User Refund Mechanism for Unfulfilled Price Update Requests - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
The `Echo` contract allows users to pay fees for price update callbacks via `requestPriceUpdatesWithCallback()`, but provides no mechanism for users to cancel their request or reclaim their fee if the assigned provider never calls `executeCallback()`. The provider's portion of the fee (`req.fee`) is stored in the request struct and only credited to the provider upon fulfillment. If the provider never fulfills the request, the user's funds are permanently locked in the contract with no recovery path.

### Finding Description

When a user calls `requestPriceUpdatesWithCallback()`, the fee is split at request time:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
// ...
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [1](#0-0) 

The Pyth protocol fee (`pythFeeInWei`) is immediately credited to `_state.accruedFeesInWei`, but the provider's portion (`req.fee = msg.value - pythFeeInWei`) is stored inside the request struct and only credited to the provider when `executeCallback()` is called:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);

clearRequest(sequenceNumber);
``` [2](#0-1) 

There is no `cancelRequest()` function, no timeout-based refund, and no user-callable withdrawal path anywhere in the contract. The entire `Echo.sol` exposes only `requestPriceUpdatesWithCallback()`, `executeCallback()`, `withdrawFees()` (admin-only), and `withdrawAsFeeManager()` (fee manager-only). None of these allow the original requester to reclaim their locked `req.fee`. [3](#0-2) 

A secondary lock vector exists because `executeCallback()` calls `parsePriceFeedUpdates` with a strict `publishTime` window:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
``` [4](#0-3) 

Pyth price data has a finite availability window in Hermes. Once the `publishTime` of a request is too old for Hermes to serve, `parsePriceFeedUpdates` will revert for any caller — including third parties who attempt to fulfill after the exclusivity period expires. This makes the lock permanent even after the exclusivity period.

The developers themselves acknowledge this in a TODO comment:

```solidity
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
``` [5](#0-4) 

**The lock chain:**
```
✅ User pays fee → req.fee stored in request struct
❌ Provider goes offline or ignores the request
❌ Exclusivity period expires (block.timestamp >= req.publishTime + exclusivityPeriodSeconds)
❌ publishTime becomes too old → parsePriceFeedUpdates reverts for all callers
❌ No cancelRequest() function exists
❌ No timeout-based refund exists
❌ req.fee is permanently locked in the contract
```

### Impact Explanation
Any user who calls `requestPriceUpdatesWithCallback()` and whose assigned provider fails to fulfill the request (due to going offline, being malicious, or the price data window expiring) loses their entire fee minus the Pyth protocol cut. The funds are unrecoverable by the user, and there is no admin escape hatch for user refunds either — `withdrawFees()` only allows the admin to withdraw `accruedFeesInWei` (the Pyth protocol portion), not `req.fee` stored in individual requests. [6](#0-5) 

### Likelihood Explanation
The likelihood is medium. The default provider is a trusted Pyth-operated service, but:
1. Any registered provider can be selected by the user
2. Providers can go offline or become unresponsive
3. The `publishTime` constraint means requests must be fulfilled quickly — a provider that is slow or temporarily offline will cause the price data to expire, permanently blocking fulfillment
4. The exclusivity period (`exclusivityPeriodSeconds`) creates a window where only the assigned provider can fulfill, increasing the risk of a single point of failure [7](#0-6) 

### Recommendation
Add a user-callable `cancelRequest()` function that allows the original requester to reclaim their `req.fee` after a timeout period (e.g., after the exclusivity period plus a grace period). The function should:
1. Verify `msg.sender == req.requester`
2. Verify sufficient time has elapsed since `req.publishTime`
3. Refund `req.fee` to the requester
4. Call `clearRequest(sequenceNumber)`

Alternatively, implement a timeout mechanism in `executeCallback()` that automatically refunds the requester if the request is too old to be fulfilled.

### Proof of Concept

```solidity
// 1. User requests a price update and pays 1 ETH
uint64 seqNum = echo.requestPriceUpdatesWithCallback{value: 1 ether}(
    provider,
    block.timestamp,  // publishTime = now
    priceIds,
    500_000           // callbackGasLimit
);
// req.fee = 1 ether - pythFeeInWei is now stored in the request

// 2. Provider goes offline and never calls executeCallback()

// 3. After exclusivity period, publishTime is now too old for Hermes
// Any attempt to call executeCallback() will revert at parsePriceFeedUpdates
// because the price data for the original publishTime is no longer available

// 4. User attempts to cancel — no such function exists
// echo.cancelRequest(seqNum); // DOES NOT EXIST

// 5. req.fee is permanently locked in the contract
// User has lost their funds with no recovery path
``` [3](#0-2) [8](#0-7)

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
