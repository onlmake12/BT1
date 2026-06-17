### Title
`PythAggregatorV3.latestRoundData()` / `latestAnswer()` Return Stale Price Without Staleness Check - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink `AggregatorV3Interface`-compatible adapter, explicitly documented as a drop-in Chainlink replacement for EVM protocols. Every price-returning function in the contract (`latestRoundData`, `latestAnswer`, `getRoundData`, `decimals`) calls `pyth.getPriceUnsafe()`, which performs **no staleness check** and can return a price from arbitrarily far in the past. This is the direct Pyth analog of the Chainlink `minAnswer` circuit-breaker issue: instead of returning a clamped minimum price, `PythAggregatorV3` silently returns a frozen last-known price when the feed stops updating, with no revert and no on-chain signal beyond a stale `updatedAt` timestamp that most Chainlink-compatible consumers do not validate.

---

### Finding Description

`PythAggregatorV3` is the official Pyth SDK adapter for protocols migrating from Chainlink. Its `latestRoundData()` and `latestAnswer()` functions are the primary price-consumption entry points for any Chainlink-compatible protocol.

All four price-returning functions call `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestAnswer — no staleness check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — no staleness check
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

`getPriceUnsafe` is explicitly documented as returning a price "from arbitrarily far in the past." The Pyth contract itself provides `getPriceNoOlderThan(id, age)` as the safe alternative, but `PythAggregatorV3` never uses it.

The `updatedAt` field in the `latestRoundData` return value is set to `price.publishTime`. While a careful consumer could check `block.timestamp - updatedAt > threshold`, the Chainlink ecosystem convention is that `latestRoundData` returns a fresh price; most Chainlink-compatible protocols either do not check `updatedAt` at all, or check it with a very loose threshold.

The Pyth SDK itself warns: `getPriceUnsafe` "may return a price from arbitrarily far in the past." The safer `getPriceNoOlderThan` exists precisely to guard against this, but `PythAggregatorV3` does not use it.

---

### Impact Explanation

Any protocol using `PythAggregatorV3` as a Chainlink replacement (its stated purpose) will receive a stale, frozen price when the Pyth feed stops updating — for example, during a network outage, extreme market volatility, or an asset crash that causes publishers to halt. The protocol will continue operating at the last-known price, allowing users to:

- Borrow against collateral at an inflated stale price (if the asset has crashed)
- Execute trades at a price that no longer reflects market reality
- Exploit the discrepancy between the stale on-chain price and the true off-chain price

This is functionally identical to the Chainlink `minAnswer` circuit-breaker issue: in both cases, the oracle returns a wrong price without reverting, and the consuming protocol has no on-chain mechanism to detect the problem.

---

### Likelihood Explanation

Medium. The `PythAggregatorV3` contract is the official Pyth-provided Chainlink migration path, actively documented and promoted. Any protocol that follows the migration guide will deploy this contract. Pyth feed staleness can occur during network outages, extreme market events, or asset crashes — exactly the scenarios where price accuracy is most critical. The `updatedAt` field is present in the return value but is routinely ignored by Chainlink-compatible consumers.

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` in all price-returning functions of `PythAggregatorV3`, using a configurable `maxAge` parameter set at construction time:

```solidity
uint256 public maxAge;

constructor(address _pyth, bytes32 _priceId, uint256 _maxAge) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    maxAge = _maxAge;
}

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    // ...
}
```

This causes the function to revert with `StalePrice()` when the feed has not been updated within `maxAge` seconds, matching the behavior that Chainlink-compatible consumers expect.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a Pyth price feed for asset X.
2. Integrate it into a Chainlink-compatible lending protocol as the price oracle for asset X.
3. Asset X crashes; Pyth publishers stop submitting updates (or a network outage prevents updates).
4. The Pyth on-chain price for X is now stale — e.g., $100, while the true price is $1.
5. Call `latestRoundData()` on `PythAggregatorV3`. It calls `getPriceUnsafe`, which returns the stale $100 price with no revert.
6. The lending protocol accepts the $100 price and allows borrowing against X collateral at the inflated valuation.
7. The attacker borrows the maximum amount against X collateral, then walks away as X is worth only $1.

The root cause is entirely within Pyth's own `PythAggregatorV3.sol` — specifically the use of `getPriceUnsafe` at lines 41, 54, 59, 89, and 110. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L99-119)
```text
    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        )
    {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        roundId = uint80(price.publishTime);
        return (
            roundId,
            int256(price.price),
            price.publishTime,
            price.publishTime,
            roundId
        );
    }
```

**File:** target_chains/ethereum/sdk/solidity/AbstractPyth.sol (L50-60)
```text
    function getPriceNoOlderThan(
        bytes32 id,
        uint age
    ) public view virtual override returns (PythStructs.Price memory price) {
        price = getPriceUnsafe(id);

        if (diff(block.timestamp, price.publishTime) > age)
            revert PythErrors.StalePrice();

        return price;
    }
```
