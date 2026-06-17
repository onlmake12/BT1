### Title
Deviation Check in `_validateShouldUpdatePrices` Ignores Price Exponent, Causing Incorrect Deviation Calculation - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler._validateShouldUpdatePrices()` computes price deviation by comparing raw `int64` mantissa values (`price.price`) directly, without normalizing for the Pyth price exponent (`price.expo`). If the exponent of a feed changes between two stored updates — a legitimate Pyth protocol behavior — the deviation calculation produces a wildly incorrect result, either suppressing necessary updates or triggering spurious ones that drain subscription balances.

---

### Finding Description

In `Scheduler.sol`, the deviation-based update trigger reads the raw mantissa of the current and previous price feeds and divides them to compute a basis-point deviation:

```solidity
int64 currentPrice  = priceFeeds[i].price.price;
int64 previousPrice = previousFeed.price.price;

uint256 numerator   = SignedMath.abs(currentPrice - previousPrice);
uint256 denominator = SignedMath.abs(previousPrice);
uint256 deviationBps = Math.mulDiv(numerator, 10_000, denominator);
```

A Pyth `PythStructs.Price` is a fixed-point number: `actual_price = price × 10^expo`. The `expo` field is **not consulted** anywhere in this calculation. The code assumes both `currentPrice` and `previousPrice` share the same exponent, but this is never validated or enforced.

Pyth feeds can and do change exponents over time (e.g., from `-8` to `-6`). When that happens:

| Scenario | Previous | Current | Actual Δ | Computed deviationBps |
|---|---|---|---|---|
| Exponent: -8 → -6 (same real price $1.00) | mantissa=100_000_000 | mantissa=1_000_000 | 0% | ~9900% → spurious update |
| Exponent: -6 → -8 (100× real price increase) | mantissa=1_000_000 | mantissa=100_000_000 | 10000% | 9900% → triggers, but for wrong reason |
| Exponent: -8 → -6 (real price doubled to $2.00) | mantissa=100_000_000 | mantissa=2_000_000 | 100% | ~98% → may suppress update |

The stored `previousFeed` is whatever was last written by `_storePriceUpdates`, which stores the full `PriceFeed` struct including its `expo`. The new `priceFeeds[i].price.expo` may differ, but the deviation logic never checks.

---

### Impact Explanation

**Spurious updates (exponent shrinks):** An attacker or legitimate Pyth exponent change causes the raw mantissa to appear to have changed by orders of magnitude. `deviationBps` far exceeds `deviationThresholdBps`, so `_validateShouldUpdatePrices` returns a valid timestamp and the update proceeds. The subscription's `balanceInWei` is debited for the keeper fee on every such call. A subscription relying solely on deviation-based updates can be drained to zero by repeatedly submitting valid Pyth updates that carry the new exponent.

**Suppressed updates (exponent grows):** The mantissa shrinks proportionally, making the computed deviation appear near zero even when the real price has moved significantly. Downstream protocols reading prices via `getPricesUnsafe` / `getPriceNoOlderThan` receive stale data, potentially enabling mispricing in lending, derivatives, or other DeFi integrations.

---

### Likelihood Explanation

Pyth exponent changes are infrequent but documented protocol behavior. Any unprivileged address can call `updatePriceFeeds` with a valid signed Pyth update that carries the new exponent. No privileged role, leaked key, or governance action is required. The attacker only needs to observe an exponent change on Pythnet and submit the corresponding update data to the Scheduler contract.

---

### Recommendation

Before computing deviation, normalize both prices to the same exponent. Use the existing `PythUtils.convertToUint` helper or inline the normalization:

```solidity
// Normalize both prices to a common fixed-point representation
// before computing deviation, e.g. using PythUtils.convertToUint with targetDecimals = 18
uint256 currentNorm  = PythUtils.convertToUint(
    priceFeeds[i].price.price, priceFeeds[i].price.expo, 18);
uint256 previousNorm = PythUtils.convertToUint(
    previousFeed.price.price, previousFeed.price.expo, 18);

uint256 numerator    = currentNorm > previousNorm
    ? currentNorm - previousNorm
    : previousNorm - currentNorm;
uint256 deviationBps = Math.mulDiv(numerator, 10_000, previousNorm);
```

Alternatively, validate that `priceFeeds[i].price.expo == previousFeed.price.expo` and revert or skip the deviation check if they differ.

---

### Proof of Concept

1. A subscription is created with `updateOnDeviation = true`, `deviationThresholdBps = 100` (1%).
2. First update stores a feed with `price = 100_000_000`, `expo = -8` (real price = $1.00).
3. Pyth legitimately changes the exponent for that feed to `-6`.
4. Attacker calls `updatePriceFeeds` with a valid Pyth update: `price = 1_000_000`, `expo = -6` (real price still = $1.00).
5. Inside `_validateShouldUpdatePrices`:
   - `currentPrice = 1_000_000`
   - `previousPrice = 100_000_000`
   - `numerator = |1_000_000 - 100_000_000| = 99_000_000`
   - `denominator = 100_000_000`
   - `deviationBps = 99_000_000 * 10_000 / 100_000_000 = 9_900` (99%)
6. `9_900 >= 100` → update is accepted, keeper fee is paid from subscription balance.
7. Repeat step 4 with the next valid Pyth update (same exponent, same real price) — now `previousFeed.expo = -6` and `currentPrice = 1_000_000`, so deviation = 0 and the attack resets. The attacker can re-trigger by submitting the exponent-change update again if the feed oscillates, or by finding any feed whose exponent changes.

The root cause is at: [1](#0-0) 

The `PythStructs.Price` struct carrying both `price` and `expo` fields is defined at: [2](#0-1) 

The `PythUtils.convertToUint` normalization utility that should be used is at: [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L424-442)
```text
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
```

**File:** target_chains/fuel/contracts/pyth-interface/src/data_structures/price.sw (L20-31)
```text
pub struct Price {
    // Confidence interval around the price
    pub confidence: u64,
    // Price exponent
    // This value represents the absolute value of an i32 in the range -255 to 0. Values other than 0, should be considered negative:
    // exponent of 5 means the Pyth Price exponent was -5
    pub exponent: u32,
    // Price
    pub price: u64,
    // The TAI64 timestamp describing when the price was published
    pub publish_time: u64,
}
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
