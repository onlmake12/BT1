### Title
`getPriceUnsafe()` Used Unconditionally in All Price Functions of `PythAggregatorV3` — No Staleness Guard on L2 Deployments - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary

`PythAggregatorV3`, Pyth's official Chainlink-compatible adapter contract published in `@pythnetwork/pyth-sdk-solidity`, calls `getPriceUnsafe()` in every price-returning function. This bypasses all staleness validation. When deployed on L2 chains (Arbitrum, Optimism, Base, etc.), if the sequencer goes down or price updates stop being submitted, the adapter silently returns arbitrarily stale prices to any protocol that uses it as a drop-in Chainlink replacement.

### Finding Description

`PythAggregatorV3.sol` implements Chainlink's `AggregatorV3Interface` and is explicitly recommended by Pyth for migrating Chainlink-dependent protocols. Every price-returning function in the contract calls `pyth.getPriceUnsafe(priceId)`:

- `latestAnswer()` — line 54
- `latestTimestamp()` — line 59
- `latestRound()` — line 64 (via `latestTimestamp()`)
- `getAnswer()` — line 69 (via `latestAnswer()`)
- `getTimestamp()` — line 73 (via `latestTimestamp()`)
- `getRoundData()` — line 89
- `latestRoundData()` — line 110

`getPriceUnsafe()` is explicitly documented in `IPyth.sol` as returning "the most recent price update in this contract **without any recency checks**" and notes the price "may be arbitrarily far in the past." [1](#0-0) 

The safe alternative, `getPriceNoOlderThan(id, age)`, is available in the same `IPyth` interface and reverts with `StalePrice` if the price is too old: [2](#0-1) 

`PythAggregatorV3` never calls it. There is no staleness threshold, no `publishTime` comparison, and no sequencer liveness check anywhere in the contract. [3](#0-2) 

### Impact Explanation

Any DeFi protocol (lending, derivatives, AMM) that deploys `PythAggregatorV3` on an L2 chain and calls `latestRoundData()` or `latestAnswer()` will receive the last cached Pyth price with no indication of its age. If the L2 sequencer goes down, no new Pyth price updates can be submitted on-chain (since `updatePriceFeeds` requires an L2 transaction). The adapter continues to return the pre-downtime price as if it were current. A user can then interact with the protocol directly through the L1 optimistic rollup contract using the stale price to:

- Borrow against collateral at an inflated (stale) valuation
- Avoid liquidation that should have been triggered
- Exploit arbitrage between the stale on-chain price and the true market price

This is the same class of financial loss described in the reference report, applied to every protocol that adopts `PythAggregatorV3` as a Chainlink replacement on any L2.

### Likelihood Explanation

- `PythAggregatorV3` is Pyth's officially published and actively recommended migration path for Chainlink users, documented at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx`.
- Pyth is deployed on Arbitrum, Optimism, Base, and other L2s where sequencer downtime is a known, documented risk (Chainlink maintains Sequencer Uptime Feeds specifically for this).
- The adapter is a drop-in replacement; integrators are unlikely to add their own staleness checks on top of what they expect the adapter to enforce.
- L2 sequencer outages have occurred historically on Arbitrum and Optimism. [4](#0-3) 

### Recommendation

Replace all `getPriceUnsafe(priceId)` calls in `PythAggregatorV3` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable constructor parameter (e.g., defaulting to `getValidTimePeriod()`). For L2 deployments, additionally expose a sequencer uptime feed check analogous to what Chainlink recommends:

```solidity
// Constructor addition
uint public maxPriceAge;
AggregatorV2V3Interface public sequencerUptimeFeed; // optional, for L2

function latestRoundData() external view returns (...) {
    if (address(sequencerUptimeFeed) != address(0)) {
        (, int256 answer, uint256 startedAt,,) = sequencerUptimeFeed.latestRoundData();
        if (answer == 1 || block.timestamp - startedAt <= GRACE_PERIOD) revert SequencerDown();
    }
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxPriceAge);
    ...
}
```

At minimum, replace `getPriceUnsafe` with `getPriceNoOlderThan` so that stale prices revert rather than silently propagate.

### Proof of Concept

1. Deploy `PythAggregatorV3` on Arbitrum pointing to the Pyth contract and an ETH/USD feed.
2. A lending protocol integrates it as its Chainlink price feed.
3. ETH/USD price is $3,000 at time T. The Arbitrum sequencer goes down.
4. While the sequencer is down, ETH price drops to $1,500 on all markets.
5. A user calls `latestRoundData()` on `PythAggregatorV3` — it calls `getPriceUnsafe()` and returns $3,000 (the stale cached value) with no revert or warning.
6. The user borrows against their ETH collateral at the $3,000 valuation, receiving twice the credit they should.
7. When the sequencer comes back and prices update, the user's position is immediately undercollateralized, causing bad debt for the protocol.

The root cause is entirely within Pyth's own `PythAggregatorV3.sol`: [5](#0-4)

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

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L11-21)
```text
    /// @notice Returns the price of a price feed without any sanity checks.
    /// @dev This function returns the most recent price update in this contract without any recency checks.
    /// This function is unsafe as the returned price update may be arbitrarily far in the past.
    ///
    /// Users of this function should check the `publishTime` in the price to ensure that the returned price is
    /// sufficiently recent for their application. If you are considering using this function, it may be
    /// safer / easier to use `getPriceNoOlderThan`.
    /// @return price - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPriceUnsafe(
        bytes32 id
    ) external view returns (PythStructs.Price memory price);
```
