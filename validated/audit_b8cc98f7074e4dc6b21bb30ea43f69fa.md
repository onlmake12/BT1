### Title
`uint8` Loop Counter Overflow and Silent Truncation in Minimum Balance Check Enables Permanently Broken Subscriptions — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `Scheduler` contract uses `uint8` for loop counters iterating over `priceFeeds.length` (which equals the user-supplied `priceIds.length`), and also silently truncates `priceIds.length` to `uint8` when computing the minimum balance in `createSubscription`. A subscription created with exactly 256 price IDs bypasses the minimum balance requirement (due to `uint8(256) == 0`) and then permanently breaks `updatePriceFeeds` (all calls revert due to `uint8` overflow at the loop increment step), locking any deposited funds.

### Finding Description

In `createSubscription`, the minimum balance is computed by casting the array length to `uint8`:

```solidity
uint256 minimumBalance = this.getMinimumBalance(
    uint8(subscriptionParams.priceIds.length)
);
``` [1](#0-0) 

When `priceIds.length == 256`, `uint8(256)` silently wraps to `0`, so `getMinimumBalance(0)` returns a near-zero or zero minimum, allowing the subscription to be created with minimal funds.

Subsequently, every code path in `updatePriceFeeds` that iterates over the price feeds uses a `uint8` loop counter:

```solidity
for (uint8 i = 1; i < slots.length; i++) { ... }
``` [2](#0-1) 

```solidity
for (uint8 i = 0; i < priceFeeds.length; i++) { ... }  // timestamp loop
``` [3](#0-2) 

```solidity
for (uint8 i = 0; i < priceFeeds.length; i++) { ... }  // deviation loop
``` [4](#0-3) 

```solidity
for (uint8 i = 0; i < priceFeeds.length; i++) { ... }  // store loop
``` [5](#0-4) 

In Solidity 0.8+, arithmetic overflow reverts by default (outside `unchecked`). None of these loops are inside `unchecked` blocks. When `priceFeeds.length == 256`, the loop body executes for `i == 255` (condition `255 < 256` is true), then `i++` attempts to increment `uint8(255)` to `256`, which reverts. This makes every call to `updatePriceFeeds` permanently revert for any subscription with 256 price IDs.

There is no explicit upper-bound check on `priceIds.length` in `_validateSubscriptionParams`. [6](#0-5) 

### Impact Explanation

- A subscription with 256 price IDs is created with near-zero minimum balance (bypassing the economic safety check).
- `updatePriceFeeds` permanently reverts for that subscription — no keeper can ever update it.
- Any ETH deposited into the subscription is locked with no recovery path (no emergency withdrawal for broken subscriptions is visible in scope).
- The `activeSubscriptionIds` list is polluted with a permanently inoperable entry.

### Likelihood Explanation

Any unprivileged user can call `createSubscription` with a crafted `priceIds` array of length 256. No special role or key is required. The cost is minimal (near-zero minimum balance due to the truncation). This is directly reachable from an external transaction.

### Recommendation

1. Replace all `uint8` loop counters with `uint256` (or at minimum `uint16`) in `updatePriceFeeds`, `_validateShouldUpdatePrices`, and `_storePriceUpdates`.
2. Add an explicit upper-bound check in `_validateSubscriptionParams`:
   ```solidity
   require(params.priceIds.length > 0 && params.priceIds.length <= MAX_PRICE_IDS, "invalid priceIds length");
   ```
3. Remove the `uint8(...)` cast in `createSubscription` and use the raw `priceIds.length` (as `uint256`) when calling `getMinimumBalance`, updating its signature accordingly.

### Proof of Concept

1. Deploy `Scheduler` on a testnet.
2. Call `createSubscription` with `subscriptionParams.priceIds` containing exactly 256 distinct `bytes32` entries and `msg.value == 0` (since `getMinimumBalance(uint8(256)) == getMinimumBalance(0)`).
3. Observe the subscription is created successfully.
4. Attempt to call `updatePriceFeeds(subscriptionId, updateData)` with valid update data covering all 256 feeds.
5. Observe the transaction reverts with an arithmetic overflow error when the `uint8` loop counter reaches 255 and attempts to increment.
6. Confirm no mechanism exists to recover the deposited funds from the broken subscription.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L38-44)
```text
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L211-219)
```text
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
        if (
            params.updateCriteria.updateOnDeviation &&
            params.updateCriteria.deviationThresholdBps == 0
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L324-328)
```text
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L367-371)
```text
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L414-450)
```text
            for (uint8 i = 0; i < priceFeeds.length; i++) {
                // Get the previous price feed for this price ID using subscriptionId
                PythStructs.PriceFeed storage previousFeed = _state
                    .priceUpdates[subscriptionId][priceFeeds[i].id];

                // If there's no previous price, this is the first update
                if (previousFeed.id == bytes32(0)) {
                    return updateTimestamp;
                }

                // Calculate the deviation percentage
                int64 currentPrice = priceFeeds[i].price.price;
                int64 previousPrice = previousFeed.price.price;

                // Skip if either price is zero to avoid division by zero
                if (previousPrice == 0 || currentPrice == 0) {
                    continue;
                }

                // Calculate absolute deviation basis points (scaled by 1e4)
                uint256 numerator = SignedMath.abs(
                    currentPrice - previousPrice
                );
                uint256 denominator = SignedMath.abs(previousPrice);
                uint256 deviationBps = Math.mulDiv(
                    numerator,
                    10_000,
                    denominator
                );

                // If deviation exceeds threshold, trigger update
                if (
                    deviationBps >= params.updateCriteria.deviationThresholdBps
                ) {
                    return updateTimestamp;
                }
            }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L827-831)
```text
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            _state.priceUpdates[subscriptionId][priceFeeds[i].id] = priceFeeds[
                i
            ];
        }
```
