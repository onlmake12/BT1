### Title
`PythAggregatorV3::getRoundData` Ignores `_roundId` and Returns Current Price Instead of Historical Price — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.sol` implements the Chainlink `AggregatorV3Interface` as a drop-in compatibility adapter. The `getRoundData(uint80 _roundId)` function is supposed to return price data for a specific historical round, but it silently ignores `_roundId` and always returns the **current** latest price via `getPriceUnsafe`. Any downstream protocol that calls `getRoundData` expecting historical price data will receive the current price instead, leading to incorrect settlement, reward distribution, or collateral valuation based on wrong historical prices.

---

### Finding Description

The Chainlink `AggregatorV3Interface` defines `getRoundData(uint80 _roundId)` to return the price recorded at a specific historical round. Protocols commonly use this to retrieve prices at past timestamps for settlement, reward distribution, or historical power calculations — exactly the pattern described in the reference report.

`PythAggregatorV3.sol` implements this interface but ignores the `_roundId` argument entirely:

```solidity
// target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol
function getRoundData(
    uint80 _roundId
)
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
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);  // always current
    return (
        _roundId,
        int256(price.price),   // current price, not historical
        price.publishTime,
        price.publishTime,
        _roundId
    );
}
``` [1](#0-0) 

The function returns `_roundId` in the output fields to make it appear as though the correct historical round was fetched, but the `answer` is always the latest price from `getPriceUnsafe`. [2](#0-1) 

`getPriceUnsafe` returns only the most recently stored price with no historical lookup capability: [3](#0-2) 

This is structurally identical to the reference vulnerability: the historical identifier (`_roundId` / `epochStartTs`) is accepted as input, the historical *quantity* (stake / round ID) is correctly threaded through, but the *price* used in the computation is always the current one.

---

### Impact Explanation

Any protocol that uses `PythAggregatorV3` as a Chainlink-compatible oracle and calls `getRoundData` for historical price lookups will silently receive the current price. Concrete affected patterns include:

- **Reward distribution** based on past-epoch collateral value (exact analog to the reference report's `_getRewardsAmountPerVault`)
- **Derivatives settlement** at a specific past timestamp
- **TWAP / historical collateral valuation** using round-based lookups

The returned `roundId` and `answeredInRound` fields echo back the caller-supplied `_roundId`, making the response appear valid. There is no revert or error signal. The price discrepancy between the historical round and the current price can be arbitrarily large (e.g., during high-volatility periods), leading to material mispricing of rewards or collateral.

**Impact: Medium** — incorrect financial calculations in any protocol using `PythAggregatorV3` for historical price queries.

---

### Likelihood Explanation

`PythAggregatorV3` is explicitly marketed as a Chainlink drop-in replacement. Protocols migrating from Chainlink to Pyth that call `getRoundData` for historical lookups will hit this silently. The function signature gives no indication that historical data is unsupported. An unprivileged user can trigger the affected code path simply by interacting with any downstream protocol (e.g., claiming rewards, triggering settlement) at a time when the current price diverges from the historical price.

**Likelihood: Medium** — requires a downstream protocol to use `getRoundData` for historical lookups, which is a standard Chainlink usage pattern.

---

### Recommendation

Add an explicit revert in `getRoundData` to signal that historical round data is not supported by the Pyth pull-oracle model:

```solidity
function getRoundData(uint80 /* _roundId */) external pure override returns (...) {
    revert("getRoundData: historical round data not supported; use parsePriceFeedUpdates");
}
```

Alternatively, document prominently that `getRoundData` always returns the latest price regardless of `_roundId`, so integrators do not rely on it for historical lookups. Protocols requiring historical prices should use `parsePriceFeedUpdates` with Pyth Benchmarks data instead.

---

### Proof of Concept

1. Protocol `X` uses `PythAggregatorV3` as its Chainlink oracle for collateral pricing.
2. At epoch `N` (timestamp `T_past`), ETH/USD was $2,000. The Chainlink round ID for that time is `R_past`.
3. At epoch `N+1` (now), ETH/USD is $3,000.
4. Protocol `X` calls `getRoundData(R_past)` to compute rewards based on epoch `N` prices.
5. `PythAggregatorV3.getRoundData(R_past)` ignores `R_past`, calls `getPriceUnsafe`, and returns `$3,000`.
6. Rewards are computed using `$3,000` instead of `$2,000` — a 50% overstatement.
7. Any user can trigger this by calling the reward claim function at a time when current price > historical price, extracting excess rewards. [1](#0-0)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L76-97)
```text
    function getRoundData(
        uint80 _roundId
    )
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
        return (
            _roundId,
            int256(price.price),
            price.publishTime,
            price.publishTime,
            _roundId
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L184-194)
```text
    function getPriceUnsafe(
        bytes32 id
    ) public view override returns (PythStructs.Price memory price) {
        PythInternalStructs.PriceInfo storage info = _state.latestPriceInfo[id];
        price.publishTime = info.publishTime;
        price.expo = info.expo;
        price.price = info.price;
        price.conf = info.conf;

        if (price.publishTime == 0) revert PythErrors.PriceFeedNotFound();
    }
```
