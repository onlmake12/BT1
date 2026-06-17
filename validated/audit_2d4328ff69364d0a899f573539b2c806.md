### Title
Silent Callback Success on Non-Existent Requester Contract — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback` invokes `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)` via a Solidity `try/catch` without first verifying that `req.requester` has deployed code. Per EVM semantics, a low-level call to an address with no code returns `success = true` with empty return data. Because `_echoCallback` returns `void`, Solidity's `try` block completes without error, causing the contract to emit `PriceUpdateExecuted` and credit the provider — even though no callback was ever delivered.

### Finding Description

`requestPriceUpdatesWithCallback` stores `msg.sender` as `req.requester` with no check that the caller is a contract: [1](#0-0) 

Later, `executeCallback` credits the provider fee and clears the request **before** attempting the callback: [2](#0-1) 

It then calls the requester without any `extcodesize` guard: [3](#0-2) 

If `req.requester` is an EOA or a self-destructed contract, the EVM returns `(success=true, data="")`. Because `_echoCallback` is `void`, Solidity's `try` block succeeds, `emitPriceUpdate` fires, and `PriceUpdateExecuted` is emitted — falsely signalling a successful delivery.

By contrast, `Entropy.revealWithCallback` (the older code path) explicitly guards with `extcodesize` before invoking the consumer: [4](#0-3) 

Echo omits this guard entirely.

### Impact Explanation

1. A user (EOA or a contract that later self-destructs) calls `requestPriceUpdatesWithCallback`, paying the full fee.
2. A provider calls `executeCallback`; the callback to the code-less address silently "succeeds."
3. The provider's `accruedFeesInWei` is incremented, the request is cleared, and `PriceUpdateExecuted` is emitted.
4. The requester never receives the price data. The fee is permanently lost with no error surfaced on-chain.
5. Off-chain monitoring systems that rely on `PriceUpdateExecuted` to confirm delivery are misled.

### Likelihood Explanation

- Any EOA can call `requestPriceUpdatesWithCallback` directly; there is no code-existence check at request time.
- Contracts that use `selfdestruct` (or are destroyed via a proxy upgrade) after requesting a callback will silently lose their callback and fee.
- The pattern is reachable by any unprivileged transaction sender with no special privileges required.

### Recommendation

Add an `extcodesize` check in `executeCallback` (mirroring the pattern already used in `Entropy.revealWithCallback`) before invoking the callback, and/or add a check in `requestPriceUpdatesWithCallback` that `msg.sender` has deployed code:

```solidity
// In executeCallback, before the try block:
uint256 codeSize;
address requester = req.requester;
assembly { codeSize := extcodesize(requester) }
if (codeSize == 0) {
    emit PriceUpdateCallbackFailed(sequenceNumber, providerToCredit, priceIds, requester, "no code at requester");
    return;
}
```

### Proof of Concept

1. Deploy no contract — use a plain EOA address as `msg.sender`.
2. Call `Echo.requestPriceUpdatesWithCallback(provider, publishTime, priceIds, gasLimit)` from the EOA, paying the required fee.
3. A provider calls `Echo.executeCallback(providerToCredit, sequenceNumber, updateData, priceIds)`.
4. Observe: `PriceUpdateExecuted` is emitted (not `PriceUpdateCallbackFailed`), provider's `accruedFeesInWei` increases, request is cleared — yet the EOA received no callback and the fee is unrecoverable. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-202)
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L669-681)
```text
            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```
