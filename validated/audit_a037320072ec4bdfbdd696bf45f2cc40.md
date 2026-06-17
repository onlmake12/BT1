### Title
No Recovery Mechanism for User Fees Locked in Unfulfilled Echo Requests — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
The `Echo` contract stores user-paid fees in active request structs (`req.fee`). If a request is never fulfilled via `executeCallback()`, the ETH corresponding to `req.fee` remains permanently locked in the contract's balance with no admin or user recovery path. The only withdrawable accounting variables are `_state.accruedFeesInWei` (Pyth protocol fees) and `_state.providers[x].accruedFeesInWei` (provider fees), neither of which covers ETH locked in unfulfilled requests.

### Finding Description
When a user calls `requestPriceUpdatesWithCallback()`, the contract splits `msg.value` into two parts:

- `_state.accruedFeesInWei += _state.pythFeeInWei` — credited immediately to the Pyth protocol fee pool.
- `req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei)` — stored in the request struct, to be credited to the provider only upon successful `executeCallback()`. [1](#0-0) 

The `req.fee` amount is only moved out of the request struct when `executeCallback()` is called and succeeds: [2](#0-1) 

There is no `cancelRequest()`, no timeout-based refund, and no admin function to drain ETH locked in active requests. The admin's `withdrawFees()` only covers `_state.accruedFeesInWei`: [3](#0-2) 

The `withdrawAsFeeManager()` function similarly only covers `_state.providers[provider].accruedFeesInWei`: [4](#0-3) 

The developers themselves acknowledged this risk in a TODO comment inside `executeCallback()`: [5](#0-4) 

### Impact Explanation
Any ETH stored in `req.fee` for requests that are never fulfilled is permanently locked in the contract until an upgrade. The sum of all active `req.fee` values is held in the contract's ETH balance but is not reachable by any existing withdrawal function. Users lose their provider fee portion with no recourse.

### Likelihood Explanation
A realistic trigger exists: `publishTime` must be at most 60 seconds in the future at request time. If the assigned provider is offline or the price data for that exact `publishTime` becomes unavailable before anyone calls `executeCallback()`, the request can never be fulfilled. After the exclusivity period, any party may attempt fulfillment, but if the Pyth contract rejects the update data (e.g., timestamp out of range), `executeCallback()` reverts and the request remains active with locked funds indefinitely. No malicious actor is required — ordinary provider downtime or network congestion is sufficient. [6](#0-5) 

### Recommendation
Add a cancellation/refund path. For example, allow the original requester (or admin) to cancel a request after a timeout and recover `req.fee`:

```solidity
function cancelRequest(uint64 sequenceNumber) external {
    Request storage req = findActiveRequest(sequenceNumber);
    require(
        msg.sender == req.requester || msg.sender == _state.admin,
        "Unauthorized"
    );
    require(
        block.timestamp > req.publishTime + CANCELLATION_TIMEOUT,
        "Too early to cancel"
    );
    uint96 refundAmount = req.fee;
    clearRequest(sequenceNumber);
    (bool sent, ) = req.requester.call{value: refundAmount}("");
    require(sent, "Refund failed");
}
```

Alternatively, add an admin-only emergency drain for the difference between `address(this).balance` and the sum of all tracked accounting variables.

### Proof of Concept
1. User calls `requestPriceUpdatesWithCallback{value: 1 ether}(provider, publishTime, priceIds, gasLimit)`.
2. Contract sets `req.fee = 1 ether - pythFeeInWei` and `_state.accruedFeesInWei += pythFeeInWei`.
3. Provider goes offline; no one calls `executeCallback()` for this sequence number.
4. Admin calls `withdrawFees(accruedFeesInWei)` — succeeds, but only recovers `pythFeeInWei`.
5. The remaining `req.fee` (≈ 1 ether) is permanently locked in the contract's balance with no callable function able to retrieve it. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-158)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
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
