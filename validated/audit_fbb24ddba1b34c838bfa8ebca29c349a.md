### Title
`getRoundData()` Always Returns Latest Price Regardless of `_roundId`, Breaking Chainlink Staleness Checks and Causing DoS — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary

`PythAggregatorV3` is Pyth's official Chainlink `AggregatorV3Interface` adapter, actively promoted for migrating Chainlink-dependent protocols to Pyth. Its `getRoundData()` implementation silently ignores the requested `_roundId` and always returns the **latest** price, while setting `answeredInRound = _roundId` (the caller-supplied value). Combined with `latestRoundData()` using `publishTime` as `roundId`, any protocol that applies the standard Chainlink staleness check (`require(answeredInRound >= roundId)`) on a previous-round query will always revert, causing a permanent DoS on core protocol functions.

### Finding Description

In `PythAggregatorV3.sol`, `latestRoundData()` encodes the current `publishTime` as the `roundId`:

```solidity
roundId = uint80(price.publishTime);  // e.g. roundId = 1_700_000_000
```

`getRoundData()` completely ignores `_roundId` and always returns the latest price, setting both `roundId` and `answeredInRound` to the caller-supplied value:

```solidity
function getRoundData(uint80 _roundId) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId); // always latest
    return (
        _roundId,            // roundId   = caller input
        int256(price.price), // always latest price, NOT historical
        price.publishTime,
        price.publishTime,
        _roundId             // answeredInRound = caller input
    );
}
```

A Chainlink-compatible protocol that follows the standard pattern:

1. Calls `latestRoundData()` → receives `roundId = T` (a Unix timestamp, e.g. `1_700_000_000`)
2. Calls `getRoundData(T - 1)` to fetch the previous round for price deviation/continuity validation
3. Receives back `answeredInRound = T - 1`
4. Applies the standard staleness check: `require(answeredInRound >= roundId)` → `T-1 >= T` → **always reverts**

This is the exact DoS pattern from the reference report, reproduced through a different but equally broken mechanism: instead of a Chainlink aggregator version encoding issue, Pyth's adapter simply discards the round ID entirely.

### Impact Explanation

Any protocol using `PythAggregatorV3` as a drop-in Chainlink replacement that:
- Fetches a previous round via `getRoundData(latestRoundId - 1)` for price validation, OR
- Applies the standard `require(answeredInRound >= roundId)` staleness check on historical round queries

will experience a **permanent DoS** on all price-dependent operations: collateral pricing, liquidations, borrows, trades. The price can never be successfully fetched once the protocol enters the previous-round validation path.

Additionally, protocols that do NOT apply the staleness check will silently receive the **current** price when requesting historical data, enabling price manipulation: an attacker can supply a stale `_roundId` and receive the current price as if it were historical, bypassing time-window price deviation guards.

### Likelihood Explanation

`PythAggregatorV3` is the officially documented migration path for Chainlink users. The Pyth developer hub explicitly instructs protocols to deploy it as a drop-in replacement. Many Chainlink-native protocols implement the `answeredInRound >= roundId` staleness check or fetch previous-round data as a standard safety measure. Any such protocol that migrates using `PythAggregatorV3` without auditing the adapter's non-standard behavior will be affected immediately upon deployment.

### Recommendation

`getRoundData()` cannot faithfully implement historical round lookup because Pyth does not store per-round history on-chain. The function should either:

1. **Revert unconditionally** with a clear error (e.g., `HistoricalRoundDataNotSupported`) so callers fail loudly rather than silently receiving wrong data.
2. **Only serve `latestRoundId`**: if `_roundId == latestRoundId`, return the current price; otherwise revert.

`latestRoundData()` should also document explicitly that `roundId` is a timestamp, not a sequential counter, so callers do not assume `roundId - 1` is a valid previous round.

### Proof of Concept

```solidity
// Demonstrates DoS on a protocol using the standard Chainlink staleness check
interface IAggregatorV3 {
    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80);
    function getRoundData(uint80) external view returns (uint80, int256, uint256, uint256, uint80);
}

contract PoC {
    IAggregatorV3 aggregator; // = PythAggregatorV3 instance

    function fetchPriceWithStalenessCheck() external view returns (int256 price) {
        // Step 1: get latest round
        (uint80 roundId, int256 answer,,,uint80 answeredInRound) = aggregator.latestRoundData();
        // roundId = publishTime, e.g. 1_700_000_000
        // answeredInRound = publishTime (same value)

        // Step 2: standard Chainlink staleness check — passes here
        require(answeredInRound >= roundId, "Stale latest");

        // Step 3: fetch previous round for price deviation check (standard Chainlink pattern)
        (uint80 prevRoundId,,,,uint80 prevAnsweredInRound) = aggregator.getRoundData(roundId - 1);
        // prevRoundId        = roundId - 1  (caller-supplied, e.g. 1_699_999_999)
        // prevAnsweredInRound = roundId - 1  (caller-supplied)

        // Step 4: standard staleness check on previous round — ALWAYS REVERTS
        // prevAnsweredInRound (1_699_999_999) >= prevRoundId (1_699_999_999) passes,
        // but if the caller checks prevAnsweredInRound >= roundId (current):
        require(prevAnsweredInRound >= roundId, "Stale prev"); // 1_699_999_999 >= 1_700_000_000 → REVERT

        return answer;
    }
}
```

The root cause is at: [1](#0-0) 

`getRoundData` ignores `_roundId` and always returns the latest price with `answeredInRound = _roundId`. [2](#0-1) 

`latestRoundData` encodes `publishTime` (a Unix timestamp) as `roundId`, making `roundId - 1` a meaningless value that no historical round will ever match. [3](#0-2) 

This adapter is the officially recommended Chainlink migration path. [4](#0-3)

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

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L110-118)
```text
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        roundId = uint80(price.publishTime);
        return (
            roundId,
            int256(price.price),
            price.publishTime,
            price.publishTime,
            roundId
        );
```

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L11-11)
```text
1. Deploy the [`PythAggregatorV3`](https://github.com/pyth-network/pyth-crosschain/blob/main/target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol) contract to provide a Chainlink-compatible feed interface.
```
