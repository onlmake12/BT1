### Title
`PythAggregatorV3.latestRoundData()` Returns Stale Price Without Staleness Validation — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter, recommended in the migration guide as a drop-in replacement for `AggregatorV3Interface`. Every price-returning function in the contract — including `latestAnswer()`, `latestRoundData()`, and `getRoundData()` — internally calls `pyth.getPriceUnsafe()`, which performs **no staleness check**. Additionally, `latestRoundData()` sets `answeredInRound == roundId` unconditionally, causing the standard Chainlink staleness guard (`require(answeredInRound >= roundId)`) to always pass, even when the underlying Pyth price is arbitrarily old.

---

### Finding Description

`PythAggregatorV3.latestRoundData()` is implemented as:

```solidity
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId); // no staleness check
    roundId = uint80(price.publishTime);
    return (
        roundId,
        int256(price.price),
        price.publishTime,
        price.publishTime,  // updatedAt = publishTime (could be hours/days old)
        roundId             // answeredInRound == roundId always
    );
}
``` [1](#0-0) 

`getPriceUnsafe()` is explicitly documented as returning a price "from arbitrarily far in the past" with no freshness guarantee: [2](#0-1) 

The Pyth SDK provides `getPriceNoOlderThan()` as the safe alternative, which reverts on stale data: [3](#0-2) 

The adapter's `latestAnswer()` has the same flaw: [4](#0-3) 

Because `answeredInRound` is always set equal to `roundId` (both derived from `price.publishTime`), the canonical Chainlink staleness guard `require(answeredInRound >= roundId, "stale")` is trivially satisfied regardless of how old the price is. The `updatedAt` field is set to `price.publishTime`, which could be hours or days in the past.

---

### Impact Explanation

Protocols that migrated from Chainlink to Pyth using `PythAggregatorV3` (as recommended in the official migration guide) and apply the standard Chainlink staleness check (`answeredInRound >= roundId`) receive a false "fresh" signal for arbitrarily stale prices. This allows financial transactions — trades, liquidations, collateral valuations — to execute against stale oracle data, enabling value extraction identical to the Tigris finding: a price that should cause a revert passes validation because the staleness check is structurally bypassed.

---

### Likelihood Explanation

The Pyth migration guide explicitly recommends deploying `PythAggregatorV3` as a drop-in Chainlink replacement: [5](#0-4) 

Any protocol that followed this guide and applies the standard `answeredInRound >= roundId` staleness pattern is affected. This is a common, well-documented Chainlink integration pattern, making the likelihood of affected deployments high.

---

### Recommendation

Replace `getPriceUnsafe()` with `getPriceNoOlderThan()` in `latestRoundData()` and `latestAnswer()`, using a configurable `maxAge` parameter set at construction time:

```solidity
uint256 public maxAge; // set in constructor, e.g. 60 seconds

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

This ensures the adapter reverts on stale data, consistent with how Pyth's own safe API behaves. [6](#0-5) 

---

### Proof of Concept

1. A protocol deploys `PythAggregatorV3` following the official migration guide and calls `latestRoundData()` with the standard Chainlink staleness guard: `require(answeredInRound >= roundId, "stale")`.
2. The Pyth on-chain price for the feed has not been updated for several hours (e.g., due to a network outage or low update frequency).
3. `latestRoundData()` calls `getPriceUnsafe()`, which returns the hours-old price without reverting.
4. `answeredInRound = roundId = uint80(price.publishTime)` — the staleness check passes trivially.
5. The protocol executes a trade or liquidation against the stale price. The transaction should have reverted but proceeds, enabling the caller to profit from the price discrepancy between the stale on-chain price and the true current market price. [7](#0-6)

### Citations

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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L44-51)
```text

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.

```
