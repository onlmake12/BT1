### Title
Signed `int64` Subtraction Overflow in Deviation Check Causes DoS on `updatePriceFeeds` — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler.sol`, the deviation-based update check computes `currentPrice - previousPrice` as a native `int64` subtraction before passing the result to `SignedMath.abs`. Under Solidity 0.8+ checked arithmetic, this subtraction reverts with an arithmetic panic whenever the true difference exceeds `type(int64).max` (≈ 9.2 × 10¹⁸). Because the stored previous price persists across failed calls, a subscription whose stored price is near one extreme of `int64` can be permanently bricked for deviation-triggered updates.

### Finding Description

Inside `_validateShouldUpdatePrices`, the deviation numerator is computed as:

```solidity
int64 currentPrice  = priceFeeds[i].price.price;
int64 previousPrice = previousFeed.price.price;
...
uint256 numerator = SignedMath.abs(
    currentPrice - previousPrice   // ← int64 subtraction, checked arithmetic
);
``` [1](#0-0) 

`SignedMath.abs` accepts `int256`, but the subtraction is evaluated first as `int64`. In Solidity ≥ 0.8 the compiler inserts an overflow check on every arithmetic operation unless the code is inside an `unchecked` block. This block is not `unchecked`.

Overflow examples:

| `previousPrice` | `currentPrice` | `currentPrice - previousPrice` (int64) |
|---|---|---|
| `type(int64).min` (-9223372036854775808) | `1` | 9223372036854775809 → **overflow** |
| `-1` | `type(int64).max` (9223372036854775807) | 9223372036854775808 → **overflow** |
| `type(int64).max` | `type(int64).min` | -18446744073709551615 → **overflow** |

The `updatePriceFeeds` entry point is `external` with no caller restriction:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
``` [2](#0-1) 

Any unprivileged pusher can call it. The Pyth fee is deducted from the subscription balance before the overflow check is reached (line 305), so the subscription also loses funds on every reverted attempt. [3](#0-2) 

### Impact Explanation

- **Permanent DoS on deviation-triggered updates**: once a stored `previousPrice` is near one `int64` extreme, every subsequent update whose `currentPrice` is on the opposite side of zero will revert. The stored price is only written on success (`_storePriceUpdates` at line 343), so the bad stored value is never overwritten by a failing call.
- **Fee drain**: the Pyth fee (`pythFee`) is deducted from `status.balanceInWei` before the overflow check, so repeated failed calls drain the subscription's balance.
- Subscriptions that have **only** `updateOnDeviation = true` (no heartbeat fallback) are completely unable to receive price updates.

### Likelihood Explanation

Pyth price fields are `int64` and can legitimately be negative (e.g., funding rates, basis spreads, or any synthetic). A price feed that oscillates between a large negative value and a large positive value — a difference exceeding `type(int64).max` — triggers the overflow. Because `updatePriceFeeds` is permissionless, any pusher who submits valid Wormhole-signed update data containing such a price pair will trigger the revert. No privileged access or key compromise is required.

### Recommendation

Cast both operands to `int256` before subtracting, so the arithmetic is performed in the wider type:

```solidity
uint256 numerator = SignedMath.abs(
    int256(currentPrice) - int256(previousPrice)
);
```

`int256` can represent any difference of two `int64` values without overflow (max difference is `2 × 2^63 = 2^64`, well within `int256`).

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with `updateOnDeviation = true`, `updateOnHeartbeat = false`.
2. Submit a first valid Pyth update where `price = type(int64).min` (-9223372036854775808). This succeeds and stores `previousPrice = type(int64).min`.
3. Submit a second valid Pyth update where `price = 1`. The EVM executes `1 - (-9223372036854775808)` as `int64`, which equals `9223372036854775809` — larger than `type(int64).max` — and the Solidity 0.8 checked arithmetic panics with code `0x11`.
4. The call reverts. The stored price remains `type(int64).min`. All future updates with any non-negative `currentPrice` will also revert. The subscription is permanently bricked for deviation updates, and its balance is drained by the Pyth fee on each attempt. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-306)
```text
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L412-453)
```text
        // If updateOnDeviation is enabled, check if any price has deviated enough
        if (params.updateCriteria.updateOnDeviation) {
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
        }

        revert SchedulerErrors.UpdateConditionsNotMet();
```
