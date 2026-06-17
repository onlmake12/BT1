### Title
Loop Exhaustion Returns Out-of-Bounds Index in `findIndexOfPriceId` - (File: `target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

`Pyth.sol` contains a private helper `findIndexOfPriceId` that searches a `priceIds` calldata array for a target price ID using a `for` loop with a post-loop return of the loop counter. When the target is absent, the counter equals `priceIds.length` — an out-of-bounds value — and is returned to the caller. Any caller that passes this value directly as an index into a same-length array (e.g., `priceFeeds[k]` or `slots[k]`) will trigger an EVM revert with no meaningful error.

---

### Finding Description

`findIndexOfPriceId` is defined as:

```solidity
// Pyth.sol lines 641-652
function findIndexOfPriceId(
    bytes32[] calldata priceIds,
    bytes32 targetPriceId
) private pure returns (uint index) {
    uint k = 0;
    for (; k < priceIds.length; k++) {
        if (priceIds[k] == targetPriceId) {
            break;
        }
    }
    return k;   // equals priceIds.length when targetPriceId is absent
}
```

When `targetPriceId` is not present in `priceIds`, the loop exits with `k == priceIds.length`. The function returns this value without any sentinel or error. The companion function `fillPriceFeedFromPriceInfo` (lines 654–673) accepts `k` and immediately indexes into `priceFeeds[k]` and `slots[k]`:

```solidity
// Pyth.sol lines 654-673
function fillPriceFeedFromPriceInfo(
    PythStructs.PriceFeed[] memory priceFeeds,
    uint k,
    ...
    uint64[] memory slots,
    uint64 slot
) private pure {
    priceFeeds[k].id = priceId;   // panics if k == priceFeeds.length
    ...
    slots[k] = slot;              // panics if k == slots.length
}
```

Both `priceFeeds` and `slots` are allocated with length `priceIds.length`, so an index of `priceIds.length` is always out of bounds for them. The EVM will revert with a generic panic rather than a structured `PriceFeedNotFound` error.

---

### Impact Explanation

Any call path through `parsePriceFeedUpdatesWithConfig` (or its public wrappers such as `parsePriceFeedUpdates`, `parsePriceFeedUpdatesUnique`) that supplies update data whose embedded price IDs do not cover every element of the caller-supplied `priceIds` array will revert with an opaque out-of-bounds panic. This is a functional DoS on the price-feed parsing path: legitimate callers who request a price ID that happens not to be present in the submitted update blob receive an uninformative revert instead of the expected `PriceFeedNotFoundWithinRange` error, and the transaction fails entirely.

---

### Likelihood Explanation

The entry point is fully permissionless — any address can call `parsePriceFeedUpdates` with arbitrary `updateData` and `priceIds`. A user who accidentally (or deliberately) requests a `priceId` not included in the Merkle update blob will trigger the condition. Because Hermes can return update blobs that cover only a subset of feeds, this is a realistic operational scenario, not a contrived one.

---

### Recommendation

Add an explicit bounds check immediately after calling `findIndexOfPriceId`:

```solidity
uint k = findIndexOfPriceId(priceIds, targetPriceId);
if (k >= priceIds.length) revert PythErrors.PriceFeedNotFoundWithinRange();
```

Alternatively, refactor `findIndexOfPriceId` to return an `(bool found, uint index)` tuple so callers cannot silently ignore the "not found" case.

---

### Proof of Concept

1. Deploy `PythUpgradable` on a local fork.
2. Obtain a valid Merkle update blob for price feed A only.
3. Call `parsePriceFeedUpdates(updateData, [A, B], 0, type(uint64).max)` where B is absent from the blob.
4. `findIndexOfPriceId` returns `2` (== `priceIds.length`) for feed B.
5. `fillPriceFeedFromPriceInfo` attempts `priceFeeds[2]` on a length-2 array → EVM out-of-bounds revert (panic code `0x32`) instead of `PriceFeedNotFoundWithinRange`. [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L641-652)
```text
    function findIndexOfPriceId(
        bytes32[] calldata priceIds,
        bytes32 targetPriceId
    ) private pure returns (uint index) {
        uint k = 0;
        for (; k < priceIds.length; k++) {
            if (priceIds[k] == targetPriceId) {
                break;
            }
        }
        return k;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L654-673)
```text
    function fillPriceFeedFromPriceInfo(
        PythStructs.PriceFeed[] memory priceFeeds,
        uint k,
        bytes32 priceId,
        PythInternalStructs.PriceInfo memory info,
        uint publishTime,
        uint64[] memory slots,
        uint64 slot
    ) private pure {
        priceFeeds[k].id = priceId;
        priceFeeds[k].price.price = info.price;
        priceFeeds[k].price.conf = info.conf;
        priceFeeds[k].price.expo = info.expo;
        priceFeeds[k].price.publishTime = publishTime;
        priceFeeds[k].emaPrice.price = info.emaPrice;
        priceFeeds[k].emaPrice.conf = info.emaConf;
        priceFeeds[k].emaPrice.expo = info.expo;
        priceFeeds[k].emaPrice.publishTime = publishTime;
        slots[k] = slot;
    }
```
