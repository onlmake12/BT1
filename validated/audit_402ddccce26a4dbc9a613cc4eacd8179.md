### Title
Missing Lower-Bound Validation on `publishTime` Permanently Locks User Fees — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback` enforces only an upper-bound check on the `publishTime` parameter (`publishTime <= block.timestamp + 60`). There is no lower-bound check. A user who passes `publishTime = 0` (or any timestamp for which no Pyth price update will ever exist) creates a request that can never be fulfilled: every subsequent call to `executeCallback` will revert inside `parsePriceFeedUpdates`, and the user's fee stored in `req.fee` is permanently locked in the contract with no refund path.

---

### Finding Description

`requestPriceUpdatesWithCallback` stores the caller-supplied `publishTime` directly into the request struct and charges the caller a fee:

```solidity
// Echo.sol lines 69, 79-84
require(publishTime <= block.timestamp + 60, "Too far in future");
...
req.publishTime = publishTime;
...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

When `executeCallback` is later called, it passes `req.publishTime` as both `minPublishTime` and `maxPublishTime` to `IPyth.parsePriceFeedUpdates`:

```solidity
// Echo.sol lines 146-153
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)   // exact-match window
);
``` [2](#0-1) 

`parsePriceFeedUpdates` reverts if no price update in `updateData` has a `publishTime` that falls within `[minPublishTime, maxPublishTime]`. For `publishTime = 0` (or any stale timestamp for which no update data is available), this condition can never be satisfied, so `executeCallback` always reverts.

The provider fee (`req.fee`) is only credited to the provider inside `executeCallback` after the Pyth call succeeds:

```solidity
// Echo.sol lines 161-162
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
clearRequest(sequenceNumber);
``` [3](#0-2) 

Because `clearRequest` is never reached, the request remains active indefinitely and the funds in `req.fee` are permanently locked. There is no cancel or refund function anywhere in `Echo.sol`. [4](#0-3) 

Additionally, `_state.firstUnfulfilledSeq` is only advanced inside `executeCallback` after a successful clear:

```solidity
// Echo.sol lines 169-174
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [5](#0-4) 

Permanently stuck requests prevent `firstUnfulfilledSeq` from advancing, degrading the `getFirstActiveRequests` view function for keepers over time.

---

### Impact Explanation

- **User fee loss**: Any ETH paid as `req.fee` is permanently locked in the contract with no recovery path.
- **Keeper degradation**: `getFirstActiveRequests` must iterate past all stuck requests to find fulfillable ones, increasing gas cost for keepers proportionally to the number of stuck requests.
- **Severity**: Medium — direct, irreversible fund loss for the affected user; no privileged access required.

---

### Likelihood Explanation

- Any unprivileged user calling `requestPriceUpdatesWithCallback` with `publishTime = 0` or a timestamp older than what Hermes retains triggers this.
- Misuse is plausible: integrators may pass `0` as a default/uninitialized value, or pass a stale cached timestamp.
- The only existing guard (`publishTime <= block.timestamp + 60`) explicitly documents the upper-bound concern but leaves the lower bound entirely unguarded. [6](#0-5) 

---

### Recommendation

Add a lower-bound check in `requestPriceUpdatesWithCallback`:

```diff
require(publishTime <= block.timestamp + 60, "Too far in future");
+require(publishTime > 0, "publishTime must be non-zero");
+require(publishTime >= block.timestamp - MAX_PAST_WINDOW, "publishTime too far in past");
```

Additionally, implement a user-callable `cancelRequest` / refund function so that requests that cannot be fulfilled (e.g., due to Hermes data expiry) can be recovered.

---

### Proof of Concept

```solidity
// Attacker/user calls:
uint64 fee = echo.getFee(provider, callbackGasLimit, priceIds);
echo.requestPriceUpdatesWithCallback{value: fee}(
    provider,
    0,          // publishTime = 0 — passes the only guard
    priceIds,
    callbackGasLimit
);

// Any subsequent call to executeCallback will revert inside parsePriceFeedUpdates
// because no price update will ever have publishTime == 0.
// req.fee is permanently locked; no refund path exists.
```

The `pythFeeInWei` portion is immediately credited to `_state.accruedFeesInWei` at request time, so only `req.fee` (the provider portion) is locked — but that is the dominant share of the total fee paid by the user. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L57-102)
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-154)
```text
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

```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L169-174)
```text
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L288-332)
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
```
