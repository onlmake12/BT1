### Title
First-Occurrence-Wins in `parsePriceFeedUpdates` Allows Price Manipulation via Duplicate `priceId` Ordering in `updateData` — (`target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

In `Pyth.sol`, the internal helper `_parseSingleMerkleUpdate` uses a "first write wins" guard (`context.priceFeeds[k].id == 0`) that permanently locks a price-feed slot once it is filled during a single call. When `updateData` contains multiple valid Wormhole-signed Merkle blobs for the same `priceId`, all within the caller-supplied time window, only the **first** occurrence is used; every subsequent occurrence is silently discarded. An unprivileged caller who controls the ordering of `updateData` blobs can therefore choose which price is returned, enabling price manipulation against any protocol that accepts user-supplied `updateData` and relies on `parsePriceFeedUpdates` / `parsePriceFeedUpdatesWithConfig`.

---

### Finding Description

`_parseSingleMerkleUpdate` fills a result slot for a requested `priceId` only when two conditions hold simultaneously:

```solidity
// Pyth.sol line 239
if (k < context.priceIds.length && context.priceFeeds[k].id == 0) {
``` [1](#0-0) 

Once `context.priceFeeds[k].id` is set to a non-zero value, the slot is considered "filled" and every later occurrence of the same `priceId` in the same call is skipped entirely — regardless of whether the later occurrence has a newer `publishTime` or is otherwise more appropriate.

`parsePriceFeedUpdatesWithConfig` iterates over all blobs in `updateData` and delegates to `_parseSingleMerkleUpdate` for each individual Merkle update:

```solidity
for (uint i = 0; i < updateData.length; i++) {
    totalUpdatesAcrossBlobs += _processSingleUpdateDataBlob(updateData[i], context);
}
``` [2](#0-1) 

The public `parsePriceFeedUpdates` and `parsePriceFeedUpdatesUnique` both delegate to `parsePriceFeedUpdatesWithConfig`: [3](#0-2) 

The test suite explicitly documents this asymmetry:

> *"Only the first occurrence of a valid priceFeedMessage for a particular priceFeed.id within an updateData will be parsed … This is different than how updatePriceFeed behaves which will always update using the data of the priceFeedMessage with the latest publishTime."* [4](#0-3) 

A further test confirms that swapping the order of two blobs containing the same `priceId` changes which price is returned: [5](#0-4) 

**Secondary impact — `storeUpdatesIfFresh=true`:** When `parsePriceFeedUpdatesWithConfig` is called with `storeUpdatesIfFresh=true`, only the first-occurrence price is passed to `updateLatestPriceIfNecessary`. If a newer blob for the same `priceId` appears later in `updateData`, it is never considered for on-chain storage, potentially leaving the persistent on-chain price stale. [6](#0-5) 

---

### Impact Explanation

Any DeFi protocol that:
1. Accepts user-supplied `updateData`, and
2. Uses `parsePriceFeedUpdates` (or `parsePriceFeedUpdatesWithConfig`) to obtain a price for a specific time window

is vulnerable to price manipulation. An attacker obtains two genuine, Wormhole-guardian-signed Merkle proofs for the same `priceId` from Hermes (both within the target time window), places the more favorable (e.g., lower collateral price) blob first in the `updateData` array, and calls the victim protocol. The protocol receives the attacker-chosen price rather than the latest or highest-confidence price in the window. This can be used to under-collateralize loans, manipulate liquidation thresholds, or extract value from AMMs that use Pyth for pricing.

---

### Likelihood Explanation

- Hermes serves multiple valid signed update blobs per price feed per second; obtaining two blobs for the same `priceId` within a narrow time window is trivial.
- No signature forgery is required — only reordering of genuine blobs.
- The attack is permissionless: any caller of `parsePriceFeedUpdates` can supply crafted `updateData`.
- The behavior is undocumented in the public-facing NatSpec / interface (`IPyth.sol`), so protocol developers are unlikely to guard against it. [7](#0-6) 

---

### Recommendation

Replace the "first write wins" guard with a "latest publishTime wins" strategy inside `_parseSingleMerkleUpdate`. When a slot is already filled and a new occurrence of the same `priceId` arrives within the allowed time range, overwrite the slot if the new `publishTime` is strictly greater than the stored one:

```solidity
if (k < context.priceIds.length) {
    uint publishTime = uint(priceInfo.publishTime);
    if (
        publishTime >= context.minAllowedPublishTime &&
        publishTime <= context.maxAllowedPublishTime &&
        (!context.checkUniqueness || context.minAllowedPublishTime > prevPublishTime) &&
        publishTime > context.priceFeeds[k].price.publishTime  // overwrite if newer
    ) {
        context.priceFeeds[k].id = priceId;
        // ... fill remaining fields
    }
}
```

This mirrors the behaviour of `updatePriceFeeds` / `updateLatestPriceIfNecessary`, which already selects the latest `publishTime` when duplicate `priceId` entries appear.

---

### Proof of Concept

1. Obtain two valid Hermes update blobs for `BTC/USD` (`priceId = 0xe62df6...`) at times T=100 (price=$60,000) and T=101 (price=$61,000), both within `[minPublishTime, maxPublishTime]`.
2. Construct `updateData = [blob_T100, blob_T101]`.
3. Call `parsePriceFeedUpdates(updateData, [BTC_USD_ID], 99, 200)`.
4. Observe returned price = $60,000 (first occurrence wins).
5. Reverse order: `updateData = [blob_T101, blob_T100]`.
6. Call again — returned price = $61,000.

The attacker freely chooses which price the protocol receives by controlling blob ordering, with no cryptographic forgery required. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L231-258)
```text
        uint k = 0;
        for (; k < context.priceIds.length; k++) {
            if (context.priceIds[k] == priceId) {
                break;
            }
        }

        // Check if the priceId was requested and not already filled
        if (k < context.priceIds.length && context.priceFeeds[k].id == 0) {
            uint publishTime = uint(priceInfo.publishTime);
            if (
                publishTime >= context.minAllowedPublishTime &&
                publishTime <= context.maxAllowedPublishTime &&
                (!context.checkUniqueness ||
                    context.minAllowedPublishTime > prevPublishTime)
            ) {
                context.priceFeeds[k].id = priceId;
                context.priceFeeds[k].price.price = priceInfo.price;
                context.priceFeeds[k].price.conf = priceInfo.conf;
                context.priceFeeds[k].price.expo = priceInfo.expo;
                context.priceFeeds[k].price.publishTime = publishTime;
                context.priceFeeds[k].emaPrice.price = priceInfo.emaPrice;
                context.priceFeeds[k].emaPrice.conf = priceInfo.emaConf;
                context.priceFeeds[k].emaPrice.expo = priceInfo.expo;
                context.priceFeeds[k].emaPrice.publishTime = publishTime;
                context.slots[k] = merkleData.slot;
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L354-362)
```text
        unchecked {
            // Process each update, passing the context struct
            // Parsed results will be filled in context.priceFeeds and context.slots
            for (uint i = 0; i < updateData.length; i++) {
                totalUpdatesAcrossBlobs += _processSingleUpdateDataBlob(
                    updateData[i],
                    context
                );
            }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L364-379)
```text
            for (uint j = 0; j < priceIds.length; j++) {
                PythStructs.PriceFeed memory pf = context.priceFeeds[j];
                if (storeUpdatesIfFresh && pf.id != 0) {
                    updateLatestPriceIfNecessary(
                        priceIds[j],
                        PythInternalStructs.PriceInfo({
                            publishTime: uint64(pf.price.publishTime),
                            expo: pf.price.expo,
                            price: pf.price.price,
                            conf: pf.price.conf,
                            emaPrice: pf.emaPrice.price,
                            emaConf: pf.emaPrice.conf
                        })
                    );
                }
            }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L401-421)
```text
    function parsePriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minPublishTime,
        uint64 maxPublishTime
    )
        external
        payable
        override
        returns (PythStructs.PriceFeed[] memory priceFeeds)
    {
        (priceFeeds, ) = parsePriceFeedUpdatesWithConfig(
            updateData,
            priceIds,
            minPublishTime,
            maxPublishTime,
            false,
            false,
            false
        );
    }
```

**File:** target_chains/ethereum/contracts/test/Pyth.WormholeMerkleAccumulator.t.sol (L285-292)
```text
        // Only the first occurrence of a valid priceFeedMessage for a paritcular priceFeed.id
        // within an updateData will be parsed which is why we exclude priceFeedMessages2[1]
        // since it has the same priceFeed.id as priceFeedMessages1[0] even though it has a later publishTime.
        // This is different than how updatePriceFeed behaves which will always update using the data
        // of the priceFeedMessage with the latest publishTime for a particular priceFeed.id
        expectedPriceFeedMessages[0] = priceFeedMessages1[0];
        expectedPriceFeedMessages[1] = priceFeedMessages1[1];
        expectedPriceFeedMessages[2] = priceFeedMessages2[0];
```

**File:** target_chains/ethereum/contracts/test/Pyth.WormholeMerkleAccumulator.t.sol (L377-389)
```text
        // parsePriceFeedUpdates should return the first priceFeed in the case
        // that the updateData contains multiple feeds with the same id.
        // Swap the order of updates in updateData to verify that the other priceFeed is returned
        bytes[] memory updateData1 = new bytes[](2);
        updateData1[0] = updateData[1];
        updateData1[1] = updateData[0];

        PythStructs.PriceFeed[] memory priceFeeds1 = pyth.parsePriceFeedUpdates{
            value: updateFee
        }(updateData1, priceIds, 0, MAX_UINT64);
        assertEq(priceFeeds1.length, 1);
        assertEq(priceFeeds1[0].price.publishTime, 5);
    }
```

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L121-127)
```text
    function parsePriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minPublishTime,
        uint64 maxPublishTime
    ) external payable returns (PythStructs.PriceFeed[] memory priceFeeds);

```
