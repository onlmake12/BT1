### Title
`PythAggregatorV3.latestAnswer()` / `latestRoundData()` Return Stale Prices Without Staleness Enforcement, Silently Bypassing Standard Chainlink Staleness Checks - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter contract, published in `@pythnetwork/pyth-sdk-solidity`. Every price-returning function in the contract calls `pyth.getPriceUnsafe()`, which explicitly makes no recency guarantees. Additionally, `latestRoundData()` constructs `roundId` and `answeredInRound` as the same truncated value (`uint80(price.publishTime)`), which means the standard Chainlink staleness guard `require(answeredInRound >= roundId)` always passes — even when the cached Pyth price is arbitrarily old. Any protocol that migrates from Chainlink to `PythAggregatorV3` and retains its existing staleness checks will silently consume stale prices.

---

### Finding Description

Every price-returning function in `PythAggregatorV3` calls `getPriceUnsafe`:

```solidity
// latestAnswer — no staleness check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — no staleness check
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);   // truncated cast
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
    //                                                                           ^^^^^^^ answeredInRound == roundId always
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe` is explicitly documented as returning a price "from arbitrarily far in the past" with no recency guarantee: [3](#0-2) 

The same pattern applies to `getAnswer()`, `getRoundData()`, and `decimals()`: [4](#0-3) 

There are two compounding defects:

**Defect 1 — No staleness revert.** Unlike `getPriceNoOlderThan()` (which reverts with `StalePrice` when the price is too old), `getPriceUnsafe` returns whatever is cached on-chain, regardless of age. [5](#0-4) 

**Defect 2 — `answeredInRound == roundId` always.** The standard Chainlink staleness guard used by migrating protocols is:

```solidity
require(answeredInRound >= roundId, "stale data");
require(updatedAt != 0, "round not complete");
```

In `PythAggregatorV3.latestRoundData()`, both `roundId` and `answeredInRound` are set to `uint80(price.publishTime)`. They are always equal, so `answeredInRound >= roundId` is always `true`, even when the price is days old. The `updatedAt` field is set to `price.publishTime` (not `block.timestamp`), so a consumer checking `block.timestamp - updatedAt > maxAge` would catch staleness — but only if they implement that specific check, which is not the standard Chainlink pattern. [6](#0-5) 

---

### Impact Explanation

Any DeFi protocol (lending, derivatives, AMM) that:
1. Deploys `PythAggregatorV3` as a Chainlink drop-in replacement (the documented migration path), and
2. Retains its existing Chainlink staleness check (`answeredInRound >= roundId`)

will silently consume arbitrarily stale prices. Consequences include:
- Incorrect liquidations (liquidating healthy positions using a stale low price, or failing to liquidate insolvent positions using a stale high price)
- Incorrect collateral valuations leading to bad debt
- Incorrect trade execution / price manipulation via adversarial selection of stale prices

The `PythAggregatorV3` contract is the official Chainlink migration path documented at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx`, making this a systemic risk across all protocols that follow the migration guide. [7](#0-6) 

---

### Likelihood Explanation

- `PythAggregatorV3` is published in the official `@pythnetwork/pyth-sdk-solidity` package (v4.3.1) and is the recommended Chainlink migration path.
- Pyth is a pull oracle; the on-chain cache goes stale whenever no one calls `updateFeeds()`. This is a normal operating condition, not an edge case.
- Protocols migrating from Chainlink will naturally retain their existing staleness checks, which the `answeredInRound == roundId` invariant silently defeats.
- No privileged access is required; any unprivileged caller can trigger the stale-price path by simply not calling `updateFeeds()` before interacting with the consuming protocol.

---

### Recommendation

1. **Replace `getPriceUnsafe` with `getPriceNoOlderThan`** in all price-returning functions, using a configurable `maxAge` parameter set at construction time:

```solidity
uint256 public maxAge;

constructor(address _pyth, bytes32 _priceId, uint256 _maxAge) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    maxAge = _maxAge;
}

function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    return int256(price.price);
}
```

2. **Fix `latestRoundData` to surface staleness correctly.** Either revert on stale data (via `getPriceNoOlderThan`) or set `updatedAt = price.publishTime` and document clearly that consumers must check `block.timestamp - updatedAt` rather than `answeredInRound >= roundId`.

3. **Add a NatSpec warning** on `latestAnswer()` and `latestRoundData()` explicitly stating that the standard Chainlink staleness check (`answeredInRound >= roundId`) is not meaningful for this adapter.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract and a price feed (e.g. BTC/USD).
2. Call `updateFeeds()` once to populate the cache.
3. Wait 24 hours without calling `updateFeeds()` again (or simulate by advancing block time).
4. Call `latestRoundData()`:
   - `updatedAt` = yesterday's `publishTime`
   - `roundId` = `uint80(yesterdayTimestamp)`
   - `answeredInRound` = `uint80(yesterdayTimestamp)` (same value)
5. A consuming protocol checks `require(answeredInRound >= roundId)` → passes silently.
6. The protocol uses the 24-hour-old price for a liquidation or trade, causing financial loss.

The root cause is entirely within `PythAggregatorV3.sol` lines 53–119: `getPriceUnsafe` is called unconditionally, and `answeredInRound` is always set equal to `roundId`. [8](#0-7)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L53-119)
```text
    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return int256(price.price);
    }

    function latestTimestamp() public view returns (uint256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return price.publishTime;
    }

    function latestRound() public view returns (uint256) {
        // use timestamp as the round id
        return latestTimestamp();
    }

    function getAnswer(uint256) public view returns (int256) {
        return latestAnswer();
    }

    function getTimestamp(uint256) external view returns (uint256) {
        return latestTimestamp();
    }

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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L1-16)
```text
---
title: From Chainlink
description: Switch Chainlink AggregatorV3 feeds to Pyth price feeds in EVM applications
slug: /price-feeds/core/migrate-an-app-to-pyth/chainlink
---

This guide explains how to migrate an EVM application that uses Chainlink price feeds to Pyth price feeds.
Pyth provides a Chainlink-compatible interface for its price feeds to make this process simple.
There are two main steps to the migration:

1. Deploy the [`PythAggregatorV3`](https://github.com/pyth-network/pyth-crosschain/blob/main/target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol) contract to provide a Chainlink-compatible feed interface.
2. Schedule price updates for the feeds required by your app.

## Install Pyth SDKs

The `PythAggregatorV3` contract is provided in the [Pyth Price Feeds Solidity SDK](https://github.com/pyth-network/pyth-crosschain/tree/main/target_chains/ethereum/sdk/solidity).
```
