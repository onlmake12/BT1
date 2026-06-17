### Title
User Funds Permanently Locked When Provider Never Executes Callback — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, when a user calls `requestPriceUpdatesWithCallback`, their fee is stored in `req.fee`. The only way to release those funds is for a provider to call `executeCallback`. There is no cancellation path, no timeout, and no self-service refund mechanism. If the assigned provider goes offline or never fulfills the request, the user's ETH is permanently locked in the contract.

---

### Finding Description

When a user submits a price update request, the contract immediately credits Pyth's protocol fee and stores the remainder as `req.fee`: [1](#0-0) 

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
...
_state.accruedFeesInWei += _state.pythFeeInWei;
```

The stored `req.fee` is only ever released inside `executeCallback`, which credits it to the provider: [2](#0-1) 

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

During the exclusivity window, only the assigned provider may call `executeCallback`: [3](#0-2) 

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After the exclusivity period, anyone may call `executeCallback`, but this only executes the callback — it does not provide a refund path to the original requester. There is no `cancelRequest`, no `claimRefund`, and no expiry-based self-service withdrawal anywhere in the contract.

The codebase itself acknowledges this gap with a TODO comment: [4](#0-3) 

```solidity
// TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
// If executeCallback can revert, then funds can be permanently locked in the contract.
// TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
```

---

### Impact Explanation

If the assigned provider's keeper service goes offline, crashes, or is otherwise unavailable, the user's ETH stored in `req.fee` is permanently locked. There is no on-chain mechanism for the user to recover their funds. This is a direct loss-of-funds scenario for users of the Echo protocol.

---

### Likelihood Explanation

Keeper services are off-chain infrastructure. They can fail due to misconfiguration, network issues, or deliberate shutdown. The exclusivity period further delays any third-party intervention. Given that the protocol is designed for production use with real ETH, the probability of at least one provider going offline is non-trivial, and the impact per occurrence is a complete loss of the user's request fee.

---

### Recommendation

Add a user-callable refund function that can be invoked after a configurable timeout (e.g., after `publishTime + exclusivityPeriodSeconds + gracePeriod`). If the request is still active (i.e., `executeCallback` was never called), the user should be able to reclaim `req.fee`. This mirrors the pattern recommended in the external report: once a refund is implicitly authorized (by the provider's failure to act), the user should be able to claim it unilaterally.

```solidity
function cancelRequest(uint64 sequenceNumber) external {
    Request storage req = findActiveRequest(sequenceNumber);
    require(msg.sender == req.requester, "Not the requester");
    require(
        block.timestamp > req.publishTime + _state.exclusivityPeriodSeconds + REFUND_GRACE_PERIOD,
        "Too early to cancel"
    );
    uint96 refundAmount = req.fee;
    clearRequest(sequenceNumber);
    (bool sent, ) = msg.sender.call{value: refundAmount}("");
    require(sent, "Refund failed");
}
```

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit)`.
2. Contract stores `req.fee = fee - pythFeeInWei` and credits `pythFeeInWei` to Pyth immediately.
3. Provider's keeper service goes offline; `executeCallback` is never called.
4. User attempts to recover funds — no function exists to do so.
5. `req.fee` remains locked in the contract indefinitely with no recovery path. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-165)
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

```
