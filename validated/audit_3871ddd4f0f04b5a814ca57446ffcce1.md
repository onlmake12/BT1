### Title
Premature `clearRequest` Before Callback Delivery Causes Permanent Loss of User Fee With No Retry — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

In `Echo.sol`, the `executeCallback` function clears the user's request from storage **before** invoking the consumer's `_echoCallback`. If the callback fails (caught by the surrounding `try/catch`), the request is permanently gone with no retry mechanism. The user's fee has already been credited to the provider, and the callback is never delivered.

### Finding Description

In `executeCallback`, the sequence of operations is:

1. Provider fee is credited to `providerToCredit` (line 161–162)
2. `clearRequest(sequenceNumber)` is called (line 164) — **request is permanently deleted**
3. `firstUnfulfilledSeq` is advanced (lines 169–174)
4. `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)` is attempted inside a `try/catch` (lines 176–201)
5. If the callback reverts for any reason (gas exhaustion, logic error, etc.), the `catch` branch emits `PriceUpdateCallbackFailed` and returns — **no retry is possible** [1](#0-0) [2](#0-1) 

Because `clearRequest` runs unconditionally before the callback, a subsequent call to `executeCallback` for the same sequence number will hit `findActiveRequest` → `NoSuchRequest` revert. There is no refund path and no re-execution path for the user.

The developers themselves flagged this ordering risk in a `TODO` comment:

> `// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract.` [3](#0-2) 

However, the current implementation does **not** guarantee the callback cannot fail — it merely catches the failure and emits an event, leaving the user with no recourse.

### Impact Explanation

- User calls `requestPriceUpdatesWithCallback`, paying fee `F`.
- Provider calls `executeCallback`; fee `F` is credited to the provider and the request is cleared.
- The user's `_echoCallback` reverts (gas limit too low, logic error, etc.).
- The `PriceUpdateCallbackFailed` event is emitted, but the request slot is already gone.
- The user has permanently lost fee `F` and never received the price update callback.
- No refund mechanism and no retry mechanism exist.

**Impact: High** — user funds (fees) are permanently lost and the contracted service is never rendered.

### Likelihood Explanation

**Likelihood: Medium** — Callback failures are a normal operational scenario:
- User sets `callbackGasLimit` too low for their actual callback logic.
- User's callback contract has a bug that causes a revert.
- The provider calls `executeCallback` with insufficient outer gas, causing the 63/64 forwarding rule to starve the callback.

Any of these common conditions triggers the permanent loss.

### Recommendation

Move `clearRequest` (and the `firstUnfulfilledSeq` advancement) to **after** a successful callback, mirroring the pattern used in `Entropy.sol`'s `revealWithCallback` where `clearRequest` is only called on the success branch: [4](#0-3) 

Alternatively, introduce a `CALLBACK_FAILED` state (analogous to `EntropyStatusConstants.CALLBACK_FAILED`) that keeps the request alive and allows re-execution after the consumer fixes their callback, while still crediting the provider for the first attempt.

### Proof of Concept

1. Deploy an `EchoConsumer` whose `_echoCallback` always reverts (e.g., `revert("always fails")`).
2. Call `requestPriceUpdatesWithCallback` with a valid fee → request stored with `sequenceNumber = N`.
3. Provider calls `executeCallback(providerToCredit, N, updateData, priceIds)`.
   - Line 161–162: provider's `accruedFeesInWei` is incremented.
   - Line 164: `clearRequest(N)` — slot is zeroed.
   - Lines 176–201: `_echoCallback` reverts → `catch` branch emits `PriceUpdateCallbackFailed`.
4. Attempt to call `executeCallback` again for sequence `N` → `findActiveRequest` reverts with `NoSuchRequest`.
5. User has lost their fee; callback was never delivered; no recovery path exists. [5](#0-4)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L601-620)
```text
            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
```
