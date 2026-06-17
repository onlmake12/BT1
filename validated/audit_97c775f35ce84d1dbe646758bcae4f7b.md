### Title
Stale Price Returned Without Validity Check in `PythAggregatorV3.latestRoundData()` — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter, recommended for protocols migrating from Chainlink. Every price-reading function in the contract — including `latestRoundData()`, `latestAnswer()`, `getRoundData()`, `latestTimestamp()`, and `getAnswer()` — calls `getPriceUnsafe()` with no staleness check, no price-validity check, and no negative-price guard. Additionally, `answeredInRound` is always set equal to `roundId` (both derived from `price.publishTime`), so the standard Chainlink staleness guard `require(answeredInRound >= roundId)` trivially passes even for arbitrarily old prices.

---

### Finding Description

All public price-reading functions in `PythAggregatorV3` delegate to `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestAnswer — no staleness, no sign check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);   // price.price is int64, can be negative
}

// latestRoundData — answeredInRound == roundId always
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime,
            price.publishTime, roundId);  // answeredInRound == roundId always
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe()` is explicitly documented as returning a price "from arbitrarily far in the past" with no recency guarantee: [3](#0-2) 

The only guard in `getPriceUnsafe` is a revert when `publishTime == 0` (feed never seen), which does not protect against stale data: [4](#0-3) 

Three concrete defects:

1. **No staleness check**: `getPriceUnsafe` is used instead of `getPriceNoOlderThan`. If no one calls `updateFeeds()` for hours or days, the adapter silently returns the last cached price.
2. **`answeredInRound` spoofing**: Both `roundId` and `answeredInRound` are set to `uint80(price.publishTime)`, so they are always equal. Any downstream consumer applying the standard Chainlink guard `require(answeredInRound >= roundId, "Stale price")` will always pass, receiving a false safety signal.
3. **No negative-price guard**: `price.price` is `int64` and can be negative. `latestAnswer()` and `latestRoundData()` return it as `int256` without checking `price > 0`, unlike the Aave adapter in the same repo which explicitly checks `price.price <= 0`. [5](#0-4) 

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a drop-in Chainlink replacement and calls `latestRoundData()` or `latestAnswer()` will:

- Receive a price from an arbitrarily old Pyth update with no revert.
- Pass the standard Chainlink staleness check (`answeredInRound >= roundId`) even for stale data.
- Potentially receive a negative price without revert.

This enables an attacker to exploit price discrepancies between the stale on-chain price and the true market price — for example, borrowing against an asset whose real price has dropped but whose stale on-chain price is still high, or liquidating positions at incorrect prices. Impact is on user assets and is conditional on the Pyth feed not being updated (e.g., keeper downtime, network congestion).

---

### Likelihood Explanation

`PythAggregatorV3` is the officially recommended migration path from Chainlink, documented in the developer hub and distributed via the `@pythnetwork/pyth-sdk-solidity` npm package. Any protocol that follows the migration guide and does not add its own staleness check is exposed. The Pyth pull model requires an active keeper to call `updateFeeds()`; if that keeper stops or is delayed, the stale-price window opens. This is a realistic operational scenario. [6](#0-5) 

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` using a configurable `maxAge` parameter in all price-reading functions. Add a constructor parameter for `maxAge`. Add a `price > 0` guard before returning. Example for `latestRoundData()`:

```solidity
uint public maxAge; // set in constructor

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    require(price.price > 0, "Invalid price");
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime,
            price.publishTime, roundId);
}
```

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract and a price feed ID.
2. Do **not** call `updateFeeds()` for longer than the protocol's acceptable staleness window (e.g., 1 hour).
3. Call `latestRoundData()`. It returns the last cached price with no revert.
4. Observe that `answeredInRound == roundId` (both equal `uint80(publishTime)`), so `require(answeredInRound >= roundId)` passes.
5. A downstream lending protocol using this adapter will accept the stale price as valid, allowing an attacker to borrow against an overvalued collateral or avoid a liquidation that should have triggered. [7](#0-6) [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/aave/PythPriceOracleGetter.sol (L68-71)
```text
        // Aave is not using any price feeds < 0 for now.
        if (price.price <= 0) {
            revert InvalidNonPositivePrice();
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
