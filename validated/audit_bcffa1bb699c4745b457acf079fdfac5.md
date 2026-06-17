### Title
Stale Price Returned Without Revert in `PythAggregatorV3` — All Price-Reading Functions Use `getPriceUnsafe()` With No Staleness Enforcement - (File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol)

---

### Summary

`PythAggregatorV3`, Pyth's official Chainlink-compatible adapter contract, implements every price-reading function — `latestAnswer()`, `latestRoundData()`, `getRoundData()`, and `decimals()` — by calling `pyth.getPriceUnsafe()`. This function explicitly makes no recency guarantee and may return a price from arbitrarily far in the past. No staleness check, revert, or age validation is performed anywhere in the contract. A downstream DeFi protocol that deploys this adapter as a Chainlink feed drop-in will silently receive stale prices with no on-chain protection.

---

### Finding Description

`PythAggregatorV3.sol` is Pyth's officially published SDK contract for migrating Chainlink-based protocols to Pyth price feeds. Every price-reading function in the contract delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
// Line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// Line 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}

// Line 76-97
function getRoundData(uint80 _roundId) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
}

// Line 40-43
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
}
```

`IPyth.getPriceUnsafe()` is explicitly documented as returning a price "from arbitrarily far in the past" with no recency checks. The safe alternative, `getPriceNoOlderThan(id, age)`, is available in `AbstractPyth.sol` and reverts with `StalePrice` if the price is too old.

A second compounding issue: `latestRoundData()` sets `answeredInRound = roundId = uint80(price.publishTime)`. The standard Chainlink staleness guard used by downstream protocols is `require(answeredInRound >= roundId)`. Since both values are always identical here, this check always passes regardless of how stale the price is, defeating the consumer's own staleness defense. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

Any DeFi protocol (lending, derivatives, perpetuals) that deploys `PythAggregatorV3` as a Chainlink feed replacement and calls `latestRoundData()` or `latestAnswer()` will receive a stale price without any revert. In Pyth's pull model, prices are only updated when someone submits `updateFeeds()`. If no update has been submitted recently (e.g., due to network congestion, keeper failure, or adversarial inaction), the on-chain price can be arbitrarily old.

An attacker can exploit this by:
- Borrowing against collateral valued at a stale (inflated) price, extracting more than the collateral is worth.
- Avoiding liquidation by relying on a stale (favorable) price that has not been updated.
- Triggering incorrect liquidations of healthy positions using a stale (deflated) price.

**Impact: 3/5** — Incorrect price used in financial decisions; potential for bad debt or loss of funds in protocols using this adapter.

---

### Likelihood Explanation

`PythAggregatorV3` is the officially recommended migration path for Chainlink users, documented in Pyth's developer hub. Protocols that deploy it may not call `updateFeeds()` atomically before every price read, especially if they assume the Chainlink-compatible interface handles freshness internally (as real Chainlink feeds do). In Pyth's pull model, price staleness is a realistic and documented failure mode.

**Likelihood: 3/5** — Realistic in any deployment where `updateFeeds()` is not atomically called before every price read, which is the common pattern for Chainlink-style integrations. [5](#0-4) 

---

### Recommendation

Replace all `getPriceUnsafe()` calls in `PythAggregatorV3` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable parameter set at construction time. Alternatively, revert with a clear error if `block.timestamp - price.publishTime` exceeds a configurable staleness threshold. This mirrors the behavior of real Chainlink feeds, which revert or return a zero answer when the sequencer or feed is down.

```solidity
uint256 public maxAge; // set in constructor

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    ...
}
``` [4](#0-3) 

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract and a price feed ID (e.g., ETH/USD).
2. Call `updateFeeds()` once to seed the price.
3. Wait 24 hours without calling `updateFeeds()` again.
4. Call `latestRoundData()`. It returns the 24-hour-old price with no revert.
5. The returned `answeredInRound == roundId` (both equal `uint80(price.publishTime)`), so the standard Chainlink check `require(answeredInRound >= roundId)` passes.
6. A lending protocol using this adapter accepts the stale price as valid and allows over-borrowing against collateral whose real value has dropped significantly. [6](#0-5) [7](#0-6)

### Citations

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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L1-12)
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
```
