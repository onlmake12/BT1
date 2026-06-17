### Title
Integer Overflow in Intermediate Exponent Arithmetic Bypasses Proper Error Handling — (`File: target_chains/ethereum/sdk/solidity/PythUtils.sol`)

---

### Summary

`PythUtils.deriveCrossRate` validates that each individual exponent is `>= -255` but places **no upper-bound** on `expo2` or `targetExponent`. The intermediate expression `expo2 + targetExponent` is evaluated in `int32` arithmetic before the bounds check on `deltaExponent` is reached. When the sum overflows `int32`, Solidity 0.8.x raises an uncatchable arithmetic panic instead of the expected `PythErrors.ExponentOverflow` revert, breaking any integrator contract that wraps the call in a `try/catch` expecting a specific selector.

---

### Finding Description

In `deriveCrossRate`, after the per-input guard:

```solidity
if (expo1 < -255 || expo2 < -255 || targetExponent < -255) {
    revert PythErrors.InvalidInputExpo();
}
```

the delta is computed as:

```solidity
int64 deltaExponent = int64(expo1 - (expo2 + targetExponent));
```

`expo2` and `targetExponent` are both `int32`. Their sum is also evaluated as `int32`. `int32` max is `2,147,483,647`. If a caller passes `expo2 = 1,500,000,000` and `targetExponent = 1,500,000,000`, the addition `3,000,000,000` overflows `int32`. Under Solidity 0.8.x checked arithmetic this triggers a **panic (error code 0x11)**, not `PythErrors.ExponentOverflow`. The subsequent `deltaExponent > 58 || deltaExponent < -58` guard is never reached.

The same overflow can occur in the subtraction `expo1 - (expo2 + targetExponent)` if the intermediate sum is near `int32` boundaries. [1](#0-0) 

---

### Impact Explanation

Any integrator contract that calls `deriveCrossRate` inside a `try/catch` block filtering for `PythErrors.ExponentOverflow` will **not** catch the panic; the outer transaction reverts with an unexpected error code. This can silently break downstream logic (e.g., a fallback price path that was supposed to activate on `ExponentOverflow`). If the integrator's contract is a lending protocol or DEX that relies on the cross-rate for liquidation or trade execution, the unexpected revert can cause a temporary DoS on those operations.

---

### Likelihood Explanation

Real Pyth price exponents are small negative integers (typically `-8` to `-5`), so the overflow is not triggered by authentic Pyth data. However, `deriveCrossRate` is a `public` library function callable by any transaction sender with arbitrary `int32` arguments. An attacker who controls the inputs (e.g., a contract that accepts user-supplied exponent parameters and forwards them to `deriveCrossRate`) can reliably trigger the panic. The function is documented and promoted as a general-purpose SDK utility, increasing the surface area. [2](#0-1) 

---

### Recommendation

Add an upper-bound check on `expo2` and `targetExponent` (and `expo1`) before the intermediate arithmetic, mirroring the existing lower-bound check:

```solidity
if (expo1 < -255 || expo1 > 255 ||
    expo2 < -255 || expo2 > 255 ||
    targetExponent < -255 || targetExponent > 255) {
    revert PythErrors.InvalidInputExpo();
}
```

Alternatively, widen the intermediate computation to `int64` before the addition:

```solidity
int64 deltaExponent = int64(expo1) - (int64(expo2) + int64(targetExponent));
```

This eliminates the `int32` overflow entirely and lets the existing `> 58 || < -58` guard handle out-of-range values with the correct error selector. [3](#0-2) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "./PythUtils.sol";

contract PoC {
    function trigger() external pure returns (uint256) {
        // expo2 = 1_500_000_000, targetExponent = 1_500_000_000
        // expo2 + targetExponent = 3_000_000_000 > int32.max (2_147_483_647)
        // Solidity 0.8.x raises panic(0x11) — NOT PythErrors.ExponentOverflow
        return PythUtils.deriveCrossRate(
            1,          // price1
            0,          // expo1
            1,          // price2
            1_500_000_000,  // expo2  — passes the >= -255 guard
            1_500_000_000   // targetExponent — passes the >= -255 guard
        );
    }
}
```

Calling `trigger()` reverts with `Panic(0x11)` (arithmetic overflow), not with `PythErrors.ExponentOverflow`. A `try/catch (bytes memory)` block in an integrator contract that checks `selector == PythErrors.ExponentOverflow.selector` will not match, causing the catch branch to be skipped and the outer call to revert unexpectedly. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythUtils.sol (L84-108)
```text
    function deriveCrossRate(
        int64 price1,
        int32 expo1,
        int64 price2,
        int32 expo2,
        int32 targetExponent
    ) public pure returns (uint256 crossRate) {
        // Check if the input prices are negative
        if (price1 < 0 || price2 < 0) {
            revert PythErrors.NegativeInputPrice();
        }
        // Check if the input exponents are valid and not less than -255
        if (expo1 < -255 || expo2 < -255 || targetExponent < -255) {
            revert PythErrors.InvalidInputExpo();
        }

        // note: This value can be negative.
        int64 deltaExponent = int64(expo1 - (expo2 + targetExponent));

        // Bounds check: prevent overflow/underflow with base 10 exponentiation
        // Calculation: 10 ** n <= (2 ** 256 - 63) - 1
        //              n <= log10((2 ** 193) - 1)
        //              n <= 58.2
        if (deltaExponent > 58 || deltaExponent < -58)
            revert PythErrors.ExponentOverflow();
```

**File:** target_chains/ethereum/sdk/solidity/Math.sol (L191-206)
```text
    function tryMul(
        uint256 a,
        uint256 b
    ) internal pure returns (bool success, uint256 result) {
        unchecked {
            uint256 c = a * b;
            /// @solidity memory-safe-assembly
            assembly {
                // Only true when the multiplication doesn't overflow
                // (c / a == b) || (a == 0)
                success := or(eq(div(c, a), b), iszero(a))
            }
            // equivalent to: success ? c : 0
            result = c * toUint(success);
        }
    }
```
