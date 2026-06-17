### Title
Wrong Decimal Scale Reported by `PythAggregatorV3.decimals()` for Non-Standard Exponents — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.decimals()` computes the decimal count via `uint8(-1 * int8(price.expo))`. Because `price.expo` is `int32` and is narrowed to `int8` before negation, the function silently returns a wildly wrong decimal count for any Pyth feed whose exponent is positive or outside `[-128, 127]`. Downstream Chainlink-compatible consumers that rely on `decimals()` to scale `latestAnswer()` will compute a heavily inflated or under-reported price.

---

### Finding Description

`PythAggregatorV3` is the official Pyth SDK adapter that wraps a Pyth price feed behind Chainlink's `AggregatorV3Interface`. It is the recommended migration path for Chainlink users and is actively documented and distributed via `@pythnetwork/pyth-sdk-solidity`.

The `decimals()` function at line 42 is:

```solidity
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return uint8(-1 * int8(price.expo));   // ← bug
}
```

`price.expo` is `int32`. The expression `int8(price.expo)` is an explicit narrowing cast that silently truncates the upper 24 bits. The subsequent negation and cast to `uint8` then produce wrong values:

| `price.expo` (int32) | `int8(price.expo)` | `-1 * int8(expo)` | `uint8(result)` | Expected |
|---|---|---|---|---|
| `-8` | `-8` | `8` | `8` | `8` ✓ |
| `1` | `1` | `-1` | `255` | `1` ✗ |
| `2` | `2` | `-2` | `254` | `2` ✗ |
| `-128` | `-128` | `128` | **reverts** (int8 overflow in 0.8+) | `128` ✗ |
| `-200` | `56` (truncated) | `-56` | `200` | `200` ✗ |

For a feed with `expo = 1`, `decimals()` returns `255`. Any consumer that calls `decimals()` and then scales `latestAnswer()` by `10^255` will compute a price that is `10^(255-1) = 10^254` times too large — effectively infinite.

Meanwhile, `latestAnswer()` always returns the raw mantissa `int256(price.price)` without normalization:

```solidity
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);   // raw mantissa, no scaling
}
```

The contract's design relies entirely on `decimals()` to communicate the scale. When `decimals()` is wrong, the price is wrong.

---

### Impact Explanation

Any DeFi protocol that integrates `PythAggregatorV3` as a drop-in Chainlink replacement and reads `decimals()` to normalize the price will receive a corrupted decimal count for any Pyth feed with a positive exponent. Concrete consequences:

- **Collateral over/under-valuation** in lending protocols (e.g., Aave forks using this adapter): a price reported as `10^254` times its real value allows unlimited borrowing against worthless collateral, draining the protocol.
- **Wrong liquidation thresholds**: positions that should be liquidated are not, or healthy positions are liquidated.
- **Incorrect fee/settlement calculations** in any protocol that uses the adapter for pricing.

The `latestAnswer()` / `getRoundData()` / `latestRoundData()` functions all return the raw mantissa, so the only way a consumer can correctly interpret the value is via `decimals()`. A wrong `decimals()` value corrupts every downstream price computation.

---

### Likelihood Explanation

The Pyth protocol's `PythStructs.Price.expo` is `int32` and the protocol specification does not restrict it to negative values. Positive exponents are valid and can appear for feeds representing large-integer quantities (e.g., token amounts with no sub-unit precision). The `PythAggregatorV3` contract is deployed permissionlessly by any user for any `priceId`. A user who deploys the adapter for a feed that currently has `expo = -8` is not protected if the feed's exponent ever changes, or if they later point the adapter at a different feed. The bug is latent in every deployed instance of `PythAggregatorV3` and activates the moment a feed with a non-standard exponent is used.

---

### Recommendation

Replace the narrowing `int8` cast with a proper `int32` computation and add a bounds check:

```solidity
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    require(price.expo <= 0 && price.expo >= -255, "Unsupported exponent");
    return uint8(uint32(-price.expo));
}
```

Alternatively, normalize `latestAnswer()` to a fixed decimal scale (e.g., 18) using `PythUtils.convertToUint`, and return a constant from `decimals()`, eliminating the dynamic computation entirely.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing at a Pyth feed whose `expo` is `1` (a valid `int32` value).
2. Call `decimals()`.
3. Observe it returns `255` instead of `1`.
4. A consumer that computes `price = latestAnswer() * 10^(18 - decimals())` will compute `price = mantissa * 10^(18-255) = mantissa * 10^(-237)`, effectively zero — or if the consumer multiplies by `10^decimals()` it gets `mantissa * 10^255`, which overflows `uint256`.

Concrete arithmetic for `expo = 1`:
- `int8(1)` → `1`
- `-1 * int8(1)` → `-1` (int8)
- `uint8(int8(-1))` → `255` (silent wrap-around in Solidity 0.8+ explicit cast)
- `decimals()` returns `255` ✗ [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L40-43)
```text
    function decimals() public view virtual returns (uint8) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return uint8(-1 * int8(price.expo));
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L53-56)
```text
    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return int256(price.price);
    }
```
