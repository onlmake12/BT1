### Title
`PythAggregatorV3.latestRoundData()` Returns Stale Price Without Any Staleness Check - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is the official Pyth SDK contract that wraps Pyth price feeds behind a Chainlink `AggregatorV3Interface`. Every price-returning function — `latestRoundData()`, `latestAnswer()`, `getRoundData()`, and `decimals()` — calls `pyth.getPriceUnsafe()`, which explicitly performs **no staleness check** and may return a price from arbitrarily far in the past. Additionally, `latestRoundData()` sets both `roundId` and `answeredInRound` to the same truncated value (`uint80(price.publishTime)`), making the standard Chainlink staleness guard `require(answeredInRound >= roundId)` a permanent tautology that always passes.

---

### Finding Description

In `PythAggregatorV3.sol`, every function that returns price data calls `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestAnswer — no staleness check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — no staleness check; roundId == answeredInRound always
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);   // truncated timestamp used as roundId
    return (
        roundId,
        int256(price.price),
        price.publishTime,
        price.publishTime,
        roundId   // answeredInRound == roundId always
    );
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe` is documented to return prices from arbitrarily far in the past with no recency guarantee:

> "This function returns the most recent price update in this contract without any recency checks. This function is unsafe as the returned price update may be arbitrarily far in the past." [3](#0-2) 

The safe alternative, `getPriceNoOlderThan`, exists and enforces a staleness threshold by reverting with `StalePrice`: [4](#0-3) 

`PythAggregatorV3` is the officially recommended migration path for Chainlink integrators: [5](#0-4) 

Two compounding issues exist in `latestRoundData()`:

1. **No staleness enforcement**: `getPriceUnsafe` is used instead of `getPriceNoOlderThan`, so a price that is hours or days old is returned without revert.
2. **Tautological roundId check**: `roundId = uint80(price.publishTime)` and `answeredInRound = roundId`, so the standard Chainlink guard `require(answeredInRound >= roundId, "Stale price")` is always satisfied regardless of actual price age. [6](#0-5) 

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink drop-in replacement and applies the standard Chainlink staleness pattern will silently consume arbitrarily stale prices. The standard guard:

```solidity
require(updatedAt > 0, "Round is not complete");
require(answeredInRound >= roundId, "Stale price");
```

always passes because `updatedAt = price.publishTime` (non-zero once any price is stored) and `answeredInRound == roundId` by construction. This enables an attacker to interact with a lending, derivatives, or collateral protocol at a stale price during any period of Pyth feed inactivity (network congestion, outage, market close), leading to incorrect liquidations, undercollateralized loans, or arbitrage extraction.

---

### Likelihood Explanation

`PythAggregatorV3` is the official Pyth-provided Chainlink migration adapter, actively documented and deployed by integrators. Pyth uses a pull-based update model, meaning the on-chain price is only as fresh as the last `updatePriceFeeds` call. During any gap in updates — which is normal in the pull model — `getPriceUnsafe` returns the last stored price with no revert. The vulnerability is reachable by any unprivileged transaction sender who calls a protocol that uses this adapter.

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` in all price-returning functions, with a configurable `maxAge` parameter set at construction time:

```solidity
uint public maxAge;

constructor(address _pyth, bytes32 _priceId, uint _maxAge) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    maxAge = _maxAge;
}

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

This causes `latestRoundData()` to revert with `StalePrice` when the price is too old, matching the behavior integrators expect from a Chainlink-compatible oracle.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a Pyth contract with a price feed.
2. Call `updatePriceFeeds` once to store a price at time `T`.
3. Wait until `block.timestamp > T + validTimePeriod` (price is now stale by Pyth's own threshold).
4. Call `latestRoundData()` on `PythAggregatorV3`.
5. Observe: the call **succeeds** and returns the stale price. `updatedAt = T`, `answeredInRound = roundId = uint80(T)`.
6. Apply the standard Chainlink guard: `require(answeredInRound >= roundId)` → `uint80(T) >= uint80(T)` → **passes**.
7. The consuming protocol proceeds with a stale price, enabling exploitation. [7](#0-6) [8](#0-7)

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

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L89-96)
```text
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return (
            _roundId,
            int256(price.price),
            price.publishTime,
            price.publishTime,
            _roundId
        );
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
