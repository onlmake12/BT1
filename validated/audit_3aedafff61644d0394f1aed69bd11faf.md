### Title
Deviation Check in `_validateShouldUpdatePrices()` Ignores Price Exponent, Causing Incorrect Deviation Calculation - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_validateShouldUpdatePrices()` function in `Scheduler.sol` computes price deviation by comparing raw `int64` price mantissas without normalizing for the Pyth `expo` field. Because Pyth prices are fixed-point values of the form `price × 10^expo`, comparing mantissas across two updates that carry different exponents produces a wildly incorrect deviation percentage — directly analogous to the M-05 pattern of multiplying/comparing two scaled values without dividing out the shared scale factor.

---

### Finding Description

Inside `_validateShouldUpdatePrices`, the deviation check reads:

```solidity
int64 currentPrice  = priceFeeds[i].price.price;
int64 previousPrice = previousFeed.price.price;

uint256 numerator   = SignedMath.abs(currentPrice - previousPrice);
uint256 denominator = SignedMath.abs(previousPrice);
uint256 deviationBps = Math.mulDiv(numerator, 10_000, denominator);
``` [1](#0-0) 

The `PythStructs.Price` struct stores both a `price` (mantissa) and an `expo` (exponent), where the true price is `price × 10^expo`: [2](#0-1) 

The deviation calculation uses only the raw `price` field and never reads `expo`. If the exponent differs between the stored previous price and the incoming current price — a legitimate occurrence in Pyth price feeds — the mantissas are not on the same scale and their arithmetic difference is meaningless.

**Concrete example:**

| | `price` (mantissa) | `expo` | True value |
|---|---|---|---|
| Previous | `150000` | `-2` | `1500.00` |
| Current | `15000000` | `-4` | `1500.0000` |

Both represent the same real price (1500), but the code computes:
- `numerator = |15000000 − 150000| = 14850000`
- `denominator = 150000`
- `deviationBps = 14850000 × 10000 / 150000 = 990000 bps (9900%)`

This triggers a spurious deviation update even though the actual price is unchanged. The inverse scenario (exponent increases) would suppress a real deviation update.

---

### Impact Explanation

1. **Spurious deviation updates / subscription balance drain**: A keeper calling `updatePriceFeeds()` with a valid Pyth update whose exponent differs from the stored one will pass the deviation check regardless of whether the real price moved. Each spurious update deducts `gasCost + keeperSpecificFee` from the subscription's `balanceInWei`, draining subscriber funds. [3](#0-2) 

2. **Missed legitimate updates**: If the exponent shifts in the opposite direction, a real price deviation can be masked, causing the Scheduler to serve stale prices to whitelisted consumers.

---

### Likelihood Explanation

Pyth price feed exponents are stable for long periods but **are not guaranteed to be constant**. The Pyth aggregation program can change the exponent when the price moves into a different order-of-magnitude range. Any keeper (an unprivileged transaction sender) can submit a valid signed Pyth update that carries the new exponent, triggering the miscalculation. No privileged access is required — `updatePriceFeeds()` is a public function callable by anyone. [4](#0-3) 

---

### Recommendation

Normalize both prices to the same exponent before computing the deviation. Use the larger (less negative) exponent as the common scale, or use `PythUtils.convertToUint` to convert both prices to a fixed decimal representation before differencing:

```solidity
// Normalize to a common exponent before comparing
int32 currentExpo  = priceFeeds[i].price.expo;
int32 previousExpo = previousFeed.price.expo;

// Convert both to the same target decimals (e.g., 18)
uint256 currentNorm  = PythUtils.convertToUint(currentPrice,  currentExpo,  18);
uint256 previousNorm = PythUtils.convertToUint(previousPrice, previousExpo, 18);

uint256 numerator    = currentNorm > previousNorm
    ? currentNorm - previousNorm
    : previousNorm - currentNorm;
uint256 deviationBps = Math.mulDiv(numerator, 10_000, previousNorm);
``` [5](#0-4) 

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and register a subscription with `updateOnDeviation = true` and `deviationThresholdBps = 100` (1%).
2. Submit an initial price update: `price = 150000`, `expo = -2` (true price = 1500.00). This is stored as `previousFeed`.
3. Obtain a valid signed Pyth update for the same feed with `price = 15000000`, `expo = -4` (true price = 1500.0000 — identical real price, only exponent changed).
4. Call `updatePriceFeeds()` with this update.
5. Observe: `deviationBps = 990000` (9900%) ≥ `deviationThresholdBps = 100`, so the update is accepted and keeper fees are charged to the subscription balance — despite zero real price movement. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L844-863)
```text
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
```

**File:** target_chains/ethereum/sdk/solidity/PythStructs.sol (L13-22)
```text
    struct Price {
        // Price
        int64 price;
        // Confidence interval around the price
        uint64 conf;
        // Price exponent
        int32 expo;
        // Unix timestamp describing when the price was published
        uint publishTime;
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythUtils.sol (L21-69)
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
```
