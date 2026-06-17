### Title
User Funds Permanently Locked in Echo.sol When `parsePriceFeedUpdates` Reverts With No Refund Path — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol` implements a two-phase price-update-with-callback protocol. Users pay a fee upfront in `requestPriceUpdatesWithCallback`. A provider later calls `executeCallback`, which internally calls `parsePriceFeedUpdates` with an exact `publishTime` window. If `parsePriceFeedUpdates` reverts (e.g., `PriceFeedNotFoundWithinRange`), the entire `executeCallback` reverts, the request remains active, and the user's pre-paid fee is permanently locked in the contract with no refund or cancellation mechanism.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` collects a fee from the caller and stores it in `req.fee`:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

Later, `executeCallback` calls `parsePriceFeedUpdates` with `minPublishTime == maxPublishTime == req.publishTime` — a zero-width time window:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)   // exact match required
);
``` [2](#0-1) 

If the supplied `updateData` does not contain a price for that exact timestamp, `parsePriceFeedUpdates` reverts with `PriceFeedNotFoundWithinRange`. This causes the entire `executeCallback` to revert. The fee credit and `clearRequest` that follow are never reached:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += ...;  // never reached
clearRequest(sequenceNumber);                                  // never reached
``` [3](#0-2) 

The request stays active indefinitely. There is no `cancelRequest`, no timeout, and no refund function anywhere in `Echo.sol`. The user's fee is locked in the contract with no recovery path.

The developers themselves acknowledge this in a TODO comment immediately above the fee-credit line:

> "TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert. If executeCallback can revert, then funds can be permanently locked in the contract." [4](#0-3) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback` and pays a fee loses those funds permanently if:

1. The assigned provider submits `updateData` that does not contain a price for the exact `req.publishTime` (causing `parsePriceFeedUpdates` to revert), **and**
2. The price data for that exact timestamp is no longer available from Hermes (Hermes has a finite data retention window).

After the exclusivity period expires, any party may call `executeCallback`, but if the historical price data is gone from Hermes, no one can supply valid `updateData` and the funds are irrecoverable. The user paid for a service (price data delivered to their contract) and received nothing, with no recourse.

---

### Likelihood Explanation

- `executeCallback` has no access control beyond the exclusivity period check; any unprivileged caller can trigger it.
- The zero-width `[publishTime, publishTime]` window means any `updateData` blob that does not contain a price for that exact second will cause a revert.
- Hermes retains historical price data for a limited period. If the provider delays fulfillment past that window, the data becomes permanently unavailable.
- A negligent or malicious provider can intentionally delay fulfillment past the Hermes retention window, locking all pending user fees.
- The code's own TODO comment confirms the developers are aware this path leads to locked funds but have not yet fixed it.

---

### Recommendation

1. **Add a `cancelRequest` function** that allows the original requester to reclaim their fee after a configurable timeout (e.g., if the request has not been fulfilled within N seconds of `publishTime`).
2. **Wrap `parsePriceFeedUpdates` in a try/catch** inside `executeCallback` so that a failed price lookup does not revert the entire function; instead, emit a failure event and allow the request to be retried or cancelled.
3. **Alternatively**, relax the time window (e.g., `[publishTime, publishTime + tolerance]`) so that providers have a practical window to supply valid data.

---

### Proof of Concept

1. Alice calls `requestPriceUpdatesWithCallback(provider, T, priceIds, gasLimit)` and sends `fee` ETH. `req.fee` is stored; `req.publishTime = T`.
2. The provider attempts `executeCallback(provider, seq, updateData, priceIds)` but supplies `updateData` whose embedded price timestamp is `T+1` (off by one second).
3. `parsePriceFeedUpdates(updateData, priceIds, T, T)` reverts with `PriceFeedNotFoundWithinRange`.
4. `executeCallback` reverts. `req.fee` remains in the contract; `clearRequest` is never called.
5. After Hermes's retention window passes, no valid `updateData` for timestamp `T` exists anywhere. No one can ever call `executeCallback` successfully for this request.
6. Alice's fee is permanently locked. There is no function in `Echo.sol` to cancel the request or recover the funds. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L143-164)
```text
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
