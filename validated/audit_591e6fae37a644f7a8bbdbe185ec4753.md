### Title
Unsafe `int8` Cast in `PythAggregatorV3.decimals()` Causes Revert or Wrong Value for Feeds with Exponent = -128 or Positive Exponents — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.decimals()` casts `price.expo` (an `int32`) to `int8` before negating it. This is the direct analog of the Connext bug: an implicit assumption about the range of a numeric field causes arithmetic overflow/wrong output when the field falls outside the assumed range. Specifically, when `price.expo == -128` the function reverts due to `int8` overflow on negation; when `price.expo > 0` it silently returns a garbage `uint8` value.

---

### Finding Description

`PythAggregatorV3.decimals()` is implemented as:

```solidity
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return uint8(-1 * int8(price.expo));
}
```

`price.expo` is declared as `int32` in `PythStructs`. The function narrows it to `int8` (range −128 to 127) before negating. In Solidity ≥ 0.8, explicit casts silently truncate, but arithmetic operations revert on overflow.

| `price.expo` value | `int8(price.expo)` | `-1 * int8(...)` | `uint8(...)` | Result |
|---|---|---|---|---|
| −8 (typical) | −8 | 8 | 8 | ✓ correct |
| **−128** | −128 | **128 → int8 overflow → REVERT** | — | **DoS** |
| **+5** (positive) | 5 | −5 | **251** | **wrong value** |
| **+200** | −56 (truncated) | 56 | 56 | **wrong value** |

The root cause is identical to the Connext bug: the code assumes `expo` is always in a narrow negative range and performs arithmetic without a bounds check.

---

### Impact Explanation

Any Chainlink-compatible protocol (Aave, Compound, etc.) that integrates `PythAggregatorV3` and calls `decimals()` is affected:

- **expo = −128**: `decimals()` reverts unconditionally, permanently DoS-ing every downstream protocol function that calls `decimals()` on this adapter. The adapter becomes permanently broken for that feed.
- **expo > 0**: `decimals()` returns a garbage value (e.g., 251 for expo = 5). Downstream protocols use this to scale prices, so they compute prices that are off by a factor of `10^(251 − correct_decimals)`, enabling massive mispricing, bad debt, or liquidation failures.

---

### Likelihood Explanation

Pyth's own `PythUtils.sol` validates `expo >= -255`, confirming the protocol supports exponents well outside `int8` range. While most current production feeds use expo = −8, the Pyth network is not constrained to this. Any feed with expo = −128 (a valid `int32` value) triggers the revert. Any feed with a positive exponent (e.g., for a very low-precision or large-denomination asset) triggers the wrong-value path. A transaction sender calling `updatePriceFeeds()` with a legitimately signed VAA containing such an exponent is the entry path.

---

### Recommendation

Replace the unsafe narrowing cast with a proper bounds check and direct arithmetic on `int32`:

```solidity
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    require(price.expo <= 0 && price.expo >= -255, "Invalid expo");
    return uint8(uint32(-price.expo));
}
```

---

### Proof of Concept

```solidity
// price.expo = -128 stored on-chain via a valid signed VAA
// Calling decimals() executes:
//   int8(-128) = -128
//   -1 * int8(-128) = 128  ← int8 overflow → REVERT in Solidity 0.8+
//
// price.expo = 5 stored on-chain:
//   int8(5) = 5
//   -1 * 5 = -5
//   uint8(-5) = 251  ← silent wrap, wrong decimals returned
``` [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L40-43)
```text
    function decimals() public view virtual returns (uint8) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return uint8(-1 * int8(price.expo));
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythUtils.sol (L29-31)
```text
        if (expo < -255) {
            revert PythErrors.InvalidInputExpo();
        }
```
