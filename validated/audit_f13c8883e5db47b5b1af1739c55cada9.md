### Title
Fee Paid Upfront Is Not Refunded When `_echoCallback` Fails — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, a user pays a fee upfront via `requestPriceUpdatesWithCallback`. When a provider later calls `executeCallback`, the provider's fee balance is credited and the request is **cleared before** the consumer's `_echoCallback` is invoked. If `_echoCallback` reverts, the provider retains the full fee, the request is permanently deleted, and the user has no refund path — an exact structural analog to the Axelar H-01 finding.

---

### Finding Description

`requestPriceUpdatesWithCallback` collects `msg.value` from the caller and stores the net fee in `req.fee`: [1](#0-0) 

Inside `executeCallback`, the provider is credited and the request is cleared **before** the callback is attempted: [2](#0-1) 

Only after those irreversible state changes does the contract attempt the consumer callback inside a `try/catch`: [3](#0-2) 

When the `catch` branch is taken, the contract emits `PriceUpdateCallbackFailed` but performs no refund and no request restoration. The request slot is already cleared, so it cannot be retried. The provider keeps the fee. The user's funds are permanently lost.

The codebase itself acknowledges the danger with an inline TODO: [4](#0-3) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback` and whose `_echoCallback` reverts (due to a bug in their consumer contract, out-of-gas, or any other reason) permanently loses the fee they paid. There is no `cancelRequest`, no refund function, and no way to reclaim the fee once the request is cleared. This is a direct loss of user funds with no recovery path inside the protocol.

---

### Likelihood Explanation

`_echoCallback` failure is a realistic, common occurrence:
- Consumer contracts may have bugs that cause reverts.
- The `callbackGasLimit` set at request time may be insufficient for the actual callback logic.
- Any revert inside the consumer (e.g., a failed assertion, a downstream call failure) triggers the `catch` branch.

Any unprivileged user who calls `requestPriceUpdatesWithCallback` is exposed. No special privileges or attack setup are required.

---

### Recommendation

Move the provider fee credit and request clearance to **after** a successful callback, or implement a refund mechanism:

1. **Option A (preferred):** Only credit the provider and clear the request inside the `try` success branch. On callback failure, restore the request as retryable and do not credit the provider.
2. **Option B:** On callback failure, refund `req.fee` to `req.requester` instead of crediting the provider.
3. **Option C:** Add a `cancelRequest` function that allows the requester to reclaim their fee if the request has been in a failed state for a configurable timeout.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit)`.
   - `req.fee = msg.value - pythFeeInWei` is stored; `_state.accruedFeesInWei += pythFeeInWei`.
2. Provider calls `executeCallback(provider, sequenceNumber, updateData, priceIds)`.
   - Line 161–162: `_state.providers[provider].accruedFeesInWei += (req.fee + msg.value) - pythFee` — provider is paid.
   - Line 164: `clearRequest(sequenceNumber)` — request is deleted.
   - Lines 176–200: `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)` reverts.
   - `catch` branch emits `PriceUpdateCallbackFailed`. No refund. No request restoration.
3. User's fee is gone. Provider keeps it. The sequence number is cleared and cannot be replayed. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-201)
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
```
