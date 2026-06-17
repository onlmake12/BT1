### Title
Unvalidated `providerToCredit` Parameter in `executeCallback` Enables Front-Running Fee Theft - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback` accepts an arbitrary `providerToCredit` address from any caller after the exclusivity period expires. Because `providerToCredit` is never validated against `msg.sender`, an attacker can front-run a legitimate provider's `executeCallback` transaction by copying the `updateData` from the mempool and substituting their own registered address as `providerToCredit`, redirecting the entire provider fee to themselves.

### Finding Description
`executeCallback` is a public function with no caller restriction after the exclusivity window. The only guard is:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`, the check is skipped entirely. Any caller may pass any address as `providerToCredit`, and the full provider fee is credited to that address:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

`req.fee` is the fee deposited by the requester at request time (minus the Pyth protocol fee). There is no requirement that `providerToCredit == msg.sender`. [1](#0-0) [2](#0-1) 

### Impact Explanation
A registered attacker can front-run any legitimate provider's `executeCallback` transaction:

1. Attacker calls `registerProvider` to become a valid provider.
2. Attacker monitors the mempool for `executeCallback` calls from legitimate providers.
3. Attacker copies the `updateData` and `priceIds` from the pending transaction and resubmits with a higher gas price, substituting `providerToCredit = attacker_address`.
4. Attacker's transaction is included first; the request is cleared.
5. Legitimate provider's transaction reverts (`findActiveRequest` fails — request already cleared).
6. Attacker withdraws the stolen fee via the provider withdrawal path.

The requester's callback still executes correctly (the consumer receives valid price data), but the entire provider fee is diverted. This is a direct financial loss to the legitimate provider. [3](#0-2) 

### Likelihood Explanation
- The exclusivity period is a configurable `uint32` (default 15 seconds per test setup). Any request not fulfilled within that window is permanently open to this attack.
- The `updateData` payload is fully visible in the mempool; no secret knowledge is required.
- Registering as a provider requires only calling `registerProvider` — no permissioning.
- Front-running is a well-known, low-effort attack on EVM chains with a public mempool. [4](#0-3) [5](#0-4) 

### Recommendation
Validate that `providerToCredit == msg.sender` when the exclusivity period has elapsed, so only the address actually performing the work can claim the fee:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(providerToCredit == req.provider,
        "Only assigned provider during exclusivity period");
} else {
    require(providerToCredit == msg.sender,
        "providerToCredit must be msg.sender after exclusivity period");
}
```

This preserves the open-fulfillment design (anyone can execute after exclusivity) while ensuring the fee goes to the actual executor.

### Proof of Concept

```solidity
// 1. Attacker registers as a provider
vm.prank(attacker);
echo.registerProvider(baseFee, feePerFeed, feePerGas);

// 2. Legitimate provider submits executeCallback (visible in mempool)
// Attacker copies updateData and priceIds, front-runs with higher gas price

// 3. Attacker's transaction (after exclusivity period)
vm.warp(req.publishTime + exclusivityPeriod + 1);
vm.prank(attacker);
echo.executeCallback(
    attacker,          // <-- providerToCredit = attacker, not legitimate provider
    sequenceNumber,
    updateData,        // copied from mempool
    priceIds           // copied from mempool
);

// 4. Attacker's accrued fees now include req.fee
// Legitimate provider's transaction reverts: request already cleared
// Consumer callback still fires correctly — no revert visible to user
``` [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L57-84)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L61-75)
```text
    /**
     * @notice Executes the callback for a price update request
     * @dev Requires 1.5x the callback gas limit to account for cross-contract call overhead
     * For example, if callbackGasLimit is 1M, the transaction needs at least 1.5M gas + some gas for some other operations in the function before the callback
     * @param providerToCredit The provider to credit for fulfilling the request. This may not be the provider that submitted the request (if the exclusivity period has elapsed).
     * @param sequenceNumber The sequence number of the request
     * @param updateData The raw price update data from Pyth
     * @param priceIds The price feed IDs to update, must match the request
     */
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable;
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L118-122)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external;
```
