### Title
`uint8` Loop Counter and Cast Truncation in `_getPricesInternal` and Deviation Check Causes Revert for Subscriptions with >255 Price IDs — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol` uses `uint8` as the loop index variable in three separate `for` loops that iterate over `priceIds` arrays, and also silently casts `priceIds.length` (a `uint256`) to `uint8` when computing the minimum balance. If `MAX_PRICE_IDS_PER_SUBSCRIPTION` is set above 255, any subscription with more than 255 price IDs will cause these loops to revert on overflow (Solidity 0.8+ checked arithmetic), and the minimum-balance calculation will silently undercount feeds, allowing underfunded subscriptions to be created.

---

### Finding Description

In `Scheduler.sol`, three `for` loops use `uint8 i` as the counter:

**Deviation check loop** (line 414):
```solidity
for (uint8 i = 0; i < priceFeeds.length; i++) {
```

**`_getPricesInternal` — full-list branch** (line 479):
```solidity
for (uint8 i = 0; i < params.priceIds.length; i++) {
```

**`_getPricesInternal` — filtered branch** (line 500):
```solidity
for (uint8 i = 0; i < priceIds.length; i++) {
```

Additionally, `priceIds.length` is cast to `uint8` in three places when computing the minimum balance:

- `createSubscription` line 39: `uint8(subscriptionParams.priceIds.length)`
- `updateSubscription` line 108: `uint8(newParams.priceIds.length)`
- `updateSubscription` line 119: `uint8(newParams.priceIds.length)`

In Solidity ≥0.8, incrementing a `uint8` past 255 reverts with an arithmetic overflow. If `MAX_PRICE_IDS_PER_SUBSCRIPTION` (enforced in `_validateSubscriptionParams`) is set to a value greater than 255, any subscription holding 256+ price IDs will cause every call to `_getPricesInternal` and the deviation-check path of `updatePriceFeeds` to revert. The silent `uint8` cast in the minimum-balance path additionally means that a subscription with, say, 256 feeds is treated as having 0 feeds, so the balance check passes with far less ETH than required.

---

### Impact Explanation

- **`_getPricesInternal` revert**: `getPricesUnsafe`, `getPricesNoOlderThan`, and `getEmaPricesUnsafe` all call `_getPricesInternal`. Any subscription with >255 price IDs becomes permanently unreadable — all price-read calls revert.
- **`updatePriceFeeds` revert**: The deviation-check loop at line 414 also uses `uint8 i`, so `updatePriceFeeds` reverts for such subscriptions, permanently blocking keeper updates.
- **Minimum-balance undercount**: The `uint8` cast of `priceIds.length` silently wraps (e.g., 256 → 0), so `getMinimumBalance` returns 0, allowing a subscription to be created or reactivated with no ETH, draining keeper incentives.

The combined effect is a complete DoS of the Scheduler for any subscription that legitimately uses more than 255 price IDs, and a fee-bypass for subscriptions at the 256-feed boundary.

---

### Likelihood Explanation

The likelihood is conditional on `MAX_PRICE_IDS_PER_SUBSCRIPTION > 255`. The constant is defined in `SchedulerConstants.sol` (not read in this analysis), so the exact value is unconfirmed. However:

- The `uint8` type is clearly mismatched with `priceIds.length`, which is `uint256`.
- The pattern is identical to the JOJO M-7 root cause: a narrower integer type is used where a wider one is required.
- Any future governance action raising `MAX_PRICE_IDS_PER_SUBSCRIPTION` above 255 would immediately activate the vulnerability without any code change.
- A subscription manager (unprivileged user) triggers the vulnerable path simply by creating a subscription with >255 feeds.

---

### Recommendation

Replace all `uint8 i` loop counters that iterate over `priceIds`-length arrays with `uint256 i`:

```diff
- for (uint8 i = 0; i < priceFeeds.length; i++) {
+ for (uint256 i = 0; i < priceFeeds.length; i++) {

- for (uint8 i = 0; i < params.priceIds.length; i++) {
+ for (uint256 i = 0; i < params.priceIds.length; i++) {

- for (uint8 i = 0; i < priceIds.length; i++) {
+ for (uint256 i = 0; i < priceIds.length; i++) {
```

Replace the silent `uint8` casts with the actual `uint256` length (or use `SafeCast.toUint8` with a revert if the value exceeds 255, and update `getMinimumBalance` to accept `uint256`):

```diff
- uint256 minimumBalance = this.getMinimumBalance(uint8(subscriptionParams.priceIds.length));
+ uint256 minimumBalance = this.getMinimumBalance(subscriptionParams.priceIds.length);
```

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` with `MAX_PRICE_IDS_PER_SUBSCRIPTION = 300`.
2. Call `createSubscription` with 256 distinct `priceIds` and sufficient ETH (the `uint8` cast wraps 256 → 0, so `getMinimumBalance(0)` returns 0 — any `msg.value` passes).
3. Call `getPricesUnsafe(subscriptionId, [])` → reverts with arithmetic overflow at line 479 because `uint8 i` increments past 255.
4. Call `updatePriceFeeds(subscriptionId, updateData)` → reverts at line 414 for the same reason.
5. The subscription is permanently bricked: no reads, no updates. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L38-40)
```text
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L107-109)
```text
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(newParams.priceIds.length)
            );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L165-173)
```text
        // Price ID limits and uniqueness
        if (
            params.priceIds.length >
            SchedulerConstants.MAX_PRICE_IDS_PER_SUBSCRIPTION
        ) {
            revert SchedulerErrors.TooManyPriceIds(
                params.priceIds.length,
                SchedulerConstants.MAX_PRICE_IDS_PER_SUBSCRIPTION
            );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L414-414)
```text
            for (uint8 i = 0; i < priceFeeds.length; i++) {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L479-479)
```text
            for (uint8 i = 0; i < params.priceIds.length; i++) {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L500-500)
```text
        for (uint8 i = 0; i < priceIds.length; i++) {
```
