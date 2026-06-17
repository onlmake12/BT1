### Title
Deviation Check in `_validateShouldUpdatePrices` Compares Raw Price Mantissas Without Normalizing for Exponent Changes - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s deviation-based update trigger compares raw `int64` price mantissa values directly, without accounting for the Pyth price exponent (`expo`). If the exponent for a price feed changes between the stored previous update and the incoming update, the deviation calculation operates on values at different scales, producing a wildly incorrect result — either suppressing a legitimate update or triggering a spurious one.

---

### Finding Description

In `_validateShouldUpdatePrices`, the deviation check reads:

```solidity
int64 currentPrice  = priceFeeds[i].price.price;
int64 previousPrice = previousFeed.price.price;
...
uint256 numerator   = SignedMath.abs(currentPrice - previousPrice);
uint256 denominator = SignedMath.abs(previousPrice);
uint256 deviationBps = Math.mulDiv(numerator, 10_000, denominator);
``` [1](#0-0) 

The `PriceFeed` struct carries both a mantissa (`.price.price`) and an exponent (`.price.expo`). The actual decimal price is `mantissa × 10^expo`. The deviation logic uses only the mantissa and silently assumes both the current and previous price share the same exponent.

Pyth price feeds have historically had their exponents changed (e.g., a feed moving from `expo = -5` to `expo = -8`). When this happens:

- **Previous stored price**: mantissa = `1_000_000`, expo = `-5` → actual price = `$10.00`
- **Incoming price**: mantissa = `1_001_000_000`, expo = `-8` → actual price = `$10.01` (0.1% change)

The raw mantissa deviation is:

```
|1_001_000_000 - 1_000_000| / 1_000_000 = 99,900 bps ≈ 999%
```

This is a 999% computed deviation for a 0.1% actual price change, causing a spurious update trigger and draining subscription funds. The inverse scenario (exponent increasing) causes the opposite: a large actual price move appears as near-zero deviation, suppressing a legitimate update.

The previous price is stored via `_storePriceUpdates` and read back as `previousFeed`: [2](#0-1) 

The exponent fields `priceFeeds[i].price.expo` and `previousFeed.price.expo` are never compared or used in the deviation arithmetic.

The correct normalized deviation requires:

```
actual_current  = currentPrice  × 10^currentExpo
actual_previous = previousPrice × 10^previousExpo
deviation = |actual_current - actual_previous| / |actual_previous|
```

---

### Impact Explanation

**False positive (spurious trigger):** When the new exponent is smaller (more negative) than the old one, the new mantissa is proportionally larger. The raw subtraction produces a huge numerator relative to the old denominator, computing a deviation orders of magnitude above any threshold. Every `updatePriceFeeds` call succeeds regardless of actual price movement, draining the subscription's ETH balance to zero through keeper fees.

**False negative (missed trigger):** When the new exponent is larger (less negative), the new mantissa is proportionally smaller. A large actual price move appears as a tiny raw mantissa difference, computing near-zero deviation. The deviation condition is never satisfied, so subscribers relying on deviation-based updates never receive them — a critical failure for DeFi protocols using the Scheduler for risk management.

---

### Likelihood Explanation

Pyth has changed exponents for live price feeds in the past. Any keeper calling `updatePriceFeeds()` with a valid, Wormhole-verified price update after an exponent change will trigger this bug — no special privilege is required. The `updatePriceFeeds` function is permissionless: [3](#0-2) 

The keeper submits cryptographically verified Pyth price data; the exponent is part of that signed payload and cannot be forged. The bug activates automatically on the first valid update after any legitimate exponent change.

---

### Recommendation

Normalize both prices to the same exponent before computing the deviation. One approach:

```solidity
int32 currentExpo  = priceFeeds[i].price.expo;
int32 previousExpo = previousFeed.price.expo;

// Normalize to the more-negative (higher-precision) exponent
int32 targetExpo = currentExpo < previousExpo ? currentExpo : previousExpo;

// Scale each mantissa: multiply by 10^(ownExpo - targetExpo)  (always >= 0)
uint256 normCurrent  = scaleToExpo(currentPrice,  currentExpo,  targetExpo);
uint256 normPrevious = scaleToExpo(previousPrice, previousExpo, targetExpo);

uint256 deviationBps = Math.mulDiv(
    SignedMath.abs(int256(normCurrent) - int256(normPrevious)),
    10_000,
    normPrevious
);
```

Alternatively, use the existing `PythUtils.convertToUint` helper (already in the repo) to convert both prices to a common fixed-decimal representation before comparing: [4](#0-3) 

---

### Proof of Concept

1. A subscription is created with `updateOnDeviation = true` and `deviationThresholdBps = 100` (1%).
2. First `updatePriceFeeds` call stores: `price.price = 1_000_000`, `price.expo = -5` (actual = $10.00).
3. Pyth's oracle network changes the exponent for this feed from `-5` to `-8`.
4. A keeper calls `updatePriceFeeds` with a valid update: `price.price = 1_001_000_000`, `price.expo = -8` (actual = $10.01, a 0.1% move).
5. The contract computes:
   - `numerator = |1_001_000_000 - 1_000_000| = 1_000_000_000`
   - `denominator = 1_000_000`
   - `deviationBps = 1_000_000_000 * 10_000 / 1_000_000 = 10_000_000` (1,000,000 bps = 10,000%)
6. `10_000_000 >= 100` → update is accepted and keeper is paid, despite only a 0.1% actual price change.
7. This repeats on every subsequent update, continuously draining the subscription balance. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
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

**File:** target_chains/ethereum/sdk/solidity/PythUtils.sol (L21-70)
```text
    function convertToUint(
        int64 price,
        int32 expo,
        uint8 targetDecimals
    ) public pure returns (uint256) {
        if (price < 0) {
            revert PythErrors.NegativeInputPrice();
        }
        if (expo < -255) {
            revert PythErrors.InvalidInputExpo();
        }

        // If targetDecimals is 6, we want to multiply the final price by 10 ** -6
        // So the delta exponent is targetDecimals + currentExpo
        int32 deltaExponent = int32(uint32(targetDecimals)) + expo;

        // Bounds check: prevent overflow/underflow with base 10 exponentiation
        // Calculation: 10 ** n <= (2 ** 256 - 63) - 1
        //              n <= log10((2 ** 193) - 1)
        //              n <= 58.2
        if (deltaExponent > 58 || deltaExponent < -58)
            revert PythErrors.ExponentOverflow();

        // We can safely cast the price to uint256 because the above condition will revert if the price is negative
        uint256 unsignedPrice = uint256(uint64(price));

        if (deltaExponent > 0) {
            (bool success, uint256 result) = Math.tryMul(
                unsignedPrice,
                10 ** uint32(deltaExponent)
            );
            // This condition is unreachable since we validated deltaExponent bounds above.
            // But keeping it here for safety.
            if (!success) {
                revert PythErrors.CombinedPriceOverflow();
            }
            return result;
        } else {
            (bool success, uint256 result) = Math.tryDiv(
                unsignedPrice,
                10 ** uint(Math.abs(deltaExponent))
            );
            // This condition is unreachable since we validated deltaExponent bounds above.
            // But keeping it here for safety.
            if (!success) {
                revert PythErrors.CombinedPriceOverflow();
            }
            return result;
        }
    }
```
