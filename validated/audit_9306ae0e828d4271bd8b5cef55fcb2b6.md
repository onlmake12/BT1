### Title
Requester Funds Permanently Locked When Provider Fails to Execute Callback - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract collects a fee from requesters at `requestPriceUpdatesWithCallback` time, storing the provider's portion in `req.fee`. There is no cancellation or refund mechanism. If the assigned provider never calls `executeCallback` — due to going offline, the exact `publishTime` price data being unavailable, or any other failure — the provider-fee portion of the requester's payment is permanently locked in the contract with no recovery path. The contract's own TODO comments acknowledge this risk explicitly.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` splits `msg.value` into two parts:

1. `_state.accruedFeesInWei += _state.pythFeeInWei` — the Pyth protocol fee, immediately credited and withdrawable by admin.
2. `req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei)` — the provider fee, stored in the pending `Request` struct. [1](#0-0) 

The provider fee (`req.fee`) is only credited to the provider inside `executeCallback`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
clearRequest(sequenceNumber);
``` [2](#0-1) 

If `executeCallback` is never called, `req.fee` remains in the `Request` struct indefinitely. `clearRequest` only zeroes the `sequenceNumber` field — it does not refund the requester. [3](#0-2) 

There is **no `cancelRequest` function** anywhere in the contract or its interface. [4](#0-3) 

The contract itself acknowledges this in developer TODO comments:

> "TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. **If executeCallback can revert, then funds can be permanently locked in the contract.**"
> "TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback." [5](#0-4) 

Additionally, `executeCallback` calls `parsePriceFeedUpdates` with `req.publishTime` as **both** `minPublishTime` and `maxPublishTime`, meaning price data must exist for that exact second. If no Pyth price update was published at that precise timestamp, the call will always revert and the callback can never be executed. [6](#0-5) 

---

### Impact Explanation

The provider-fee portion of every `requestPriceUpdatesWithCallback` payment is permanently locked in the `Echo` contract if the callback is never executed. The requester has no mechanism to recover funds. The admin can only withdraw the Pyth-protocol fee portion (`accruedFeesInWei`); the `req.fee` amounts stored in pending requests are inaccessible to anyone until `executeCallback` is called. [7](#0-6) 

**Impact**: Direct, permanent loss of user funds (ETH) with no recovery path.

---

### Likelihood Explanation

Multiple realistic scenarios cause permanent lock:

1. **Provider goes offline** — the assigned provider stops operating; after the exclusivity period, any provider may fulfill, but if no provider does, funds are locked.
2. **Exact-timestamp price data unavailable** — `parsePriceFeedUpdates` requires price data published at exactly `req.publishTime`. If no Pyth update was published at that second, every `executeCallback` attempt reverts permanently.
3. **Callback gas limit too low** — if the consumer's `echoCallback` always reverts (e.g., out of gas), the request is cleared and fees credited to the provider, but if the provider never attempts execution, funds remain locked.

The 60-second `publishTime` window constraint makes scenario 2 realistic for any request where the exact timestamp has no corresponding Pyth price update. [8](#0-7) 

---

### Recommendation

Add a `cancelRequest` function that allows the original requester to cancel a pending request after a timeout period (e.g., after the exclusivity period has elapsed with no fulfillment) and recover their `req.fee`. Example:

```solidity
function cancelRequest(uint64 sequenceNumber) external {
    Request storage req = findActiveRequest(sequenceNumber);
    require(msg.sender == req.requester, "Only requester can cancel");
    require(
        block.timestamp > req.publishTime + _state.exclusivityPeriodSeconds + CANCEL_TIMEOUT,
        "Too early to cancel"
    );
    uint96 refundAmount = req.fee;
    clearRequest(sequenceNumber);
    (bool sent, ) = msg.sender.call{value: refundAmount}("");
    require(sent, "Refund failed");
}
```

Also consider changing `parsePriceFeedUpdates` to use a time range (e.g., `[publishTime, publishTime + tolerance]`) rather than an exact timestamp match.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, block.timestamp, priceIds, gasLimit)`.
2. `req.fee = fee - pythFeeInWei` is stored in the pending request.
3. Provider goes offline (or no Pyth price update exists for that exact `block.timestamp`).
4. No one calls `executeCallback` — the exclusivity period passes, but no provider fulfills.
5. The request remains active indefinitely. `req.fee` is locked in the contract.
6. The requester has no function to call to recover their funds. `withdrawFees` is admin-only and only covers `accruedFeesInWei` (the Pyth fee portion). `withdrawAsFeeManager` only covers `providers[x].accruedFeesInWei` (fees already credited to providers). Neither covers `req.fee` in pending requests. [9](#0-8) [10](#0-9) [11](#0-10)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L146-153)
```text
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-160)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L288-299)
```text
    // TODO: move out governance functions into a separate PulseGovernance contract
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L323-332)
```text
    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
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

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L37-75)
```text
interface IEcho is EchoEvents {
    // Core functions
    /**
     * @notice Requests price updates with a callback
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
     * @param provider The provider to fulfill the request
     * @param publishTime The minimum publish time for price updates, it should be less than or equal to block.timestamp + 60
     * @param priceIds The price feed IDs to update. Maximum 10 price feeds per request.
     *        Requests requiring more feeds should be split into multiple calls.
     * @param callbackGasLimit The amount of gas allocated for the callback execution
     * @return sequenceNumber The sequence number assigned to this request
     * @dev Security note: The 60-second future limit on publishTime prevents a DoS vector where
     *      attackers could submit many low-fee requests for far-future updates when gas prices
     *      are low, forcing executors to fulfill them later when gas prices might be much higher.
     *      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
     *      the fee estimation unreliable.
     */
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable returns (uint64 sequenceNumber);

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
