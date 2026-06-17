### Title
User Fee Permanently Locked in `Echo.sol` When `executeCallback()` Cannot Be Fulfilled Due to Missing Cancel/Refund Mechanism — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, a user pays a fee when calling `requestPriceUpdatesWithCallback()`. The provider's portion of this fee is stored in `req.fee` and is only credited to the provider upon a successful `executeCallback()` call. There is no cancel or refund mechanism for users. If `executeCallback()` can never succeed — for example, because the Pyth oracle has no price data for the exact `req.publishTime` — the user's fee is permanently locked in the contract with no recovery path. The developers themselves acknowledged this risk in a TODO comment inside `executeCallback()`.

---

### Finding Description

When a user calls `requestPriceUpdatesWithCallback()`, the fee is split: Pyth's flat fee is immediately credited, but the provider's portion is stored in `req.fee` inside the request struct. [1](#0-0) 

The provider's fee is only released when `executeCallback()` is called successfully: [2](#0-1) 

Inside `executeCallback()`, the contract calls `parsePriceFeedUpdates` with both `minPublishTime` and `maxPublishTime` set to `req.publishTime`: [3](#0-2) 

This means the submitted price data must have a `publishTime` equal **exactly** to `req.publishTime`. Pyth does not publish price updates every second for every feed; gaps exist. If the user requested a `publishTime` for which no Pyth update was ever published, `parsePriceFeedUpdates` will always revert, `executeCallback()` will always revert, and the user's fee stored in `req.fee` is permanently locked.

There is no `cancelRequest()`, `refund()`, or timeout-based recovery function anywhere in the contract. The developers explicitly flagged this in a TODO comment: [4](#0-3) 

The `IEcho` interface also exposes no cancel or refund path: [5](#0-4) 

---

### Impact Explanation

A user who calls `requestPriceUpdatesWithCallback()` with a `publishTime` for which no Pyth price update exists will have their fee permanently locked in the `Echo` contract. Neither the user nor the provider can recover these funds. This is a direct loss of user funds with no on-chain remedy.

---

### Likelihood Explanation

Pyth price feeds are not published every second. A user requesting a `publishTime` equal to `block.timestamp` may land on a second for which no update was published. Additionally, if the assigned provider goes offline or is unable to source data for the exact timestamp, the same outcome occurs. The 60-second future limit on `publishTime` does not mitigate this — it only bounds how far ahead the user can request, not whether data will exist. This scenario is realistic in normal operation.

---

### Recommendation

Add a user-callable `cancelRequest(uint64 sequenceNumber)` function that:
1. Verifies the caller is the original requester.
2. Enforces a minimum timeout (e.g., after the exclusivity period plus a grace window) to prevent griefing the provider.
3. Refunds `req.fee` to the requester and clears the request.

Alternatively, move the check for data availability earlier, or use a publish-time range (e.g., `[publishTime, publishTime + tolerance]`) instead of an exact match in `parsePriceFeedUpdates`, so providers have flexibility to fulfill requests even when the exact timestamp has no update.

---

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(provider, block.timestamp, priceIds, gasLimit)` paying fee `F`. `req.fee = F - pythFee` is stored in the request.
2. Pyth has no price update published at exactly `block.timestamp` for the requested `priceIds`.
3. Provider attempts `executeCallback(provider, seqNum, updateData, priceIds)`. The call to `parsePriceFeedUpdates{value: pythFee}(updateData, priceIds, req.publishTime, req.publishTime)` reverts because no update with `publishTime == req.publishTime` exists.
4. `executeCallback()` reverts. `req.fee` remains locked in the contract.
5. No cancel or refund function exists. The user's fee is permanently lost.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-99)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-153)
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
