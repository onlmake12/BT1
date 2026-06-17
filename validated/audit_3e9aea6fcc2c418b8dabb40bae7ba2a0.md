### Title
`PythAggregatorV3.latestRoundData()` / `latestAnswer()` Use `getPriceUnsafe()` With No Staleness Enforcement, Silently Returning Arbitrarily Stale Prices - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter contract, published in `@pythnetwork/pyth-sdk-solidity` and explicitly recommended for protocols migrating from Chainlink. Every price-returning function in the contract — `latestRoundData()`, `latestAnswer()`, `getRoundData()`, and `decimals()` — calls `pyth.getPriceUnsafe(priceId)`, which by design returns prices from arbitrarily far in the past with no staleness check and no revert. There is no on-chain guard preventing a protocol from consuming a price that is hours or days old.

---

### Finding Description

`PythAggregatorV3` implements the Chainlink `AggregatorV3Interface` as a drop-in replacement. All four price-reading functions delegate to `getPriceUnsafe`:

```solidity
// latestAnswer — line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — line 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);   // truncated timestamp used as roundId
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe` is explicitly documented in `IPyth.sol` as returning "the most recent price update in this contract without any recency checks" and warns that "the returned price update may be arbitrarily far in the past." [3](#0-2) 

The safe alternative, `getPriceNoOlderThan(id, age)`, reverts with `StalePrice` if `|block.timestamp - publishTime| > age`. [4](#0-3) 

`PythAggregatorV3` never calls it. There is no configurable `maxAge`, no revert path, and no staleness flag in any of its functions.

**Compounding issue — `answeredInRound` spoofing:** `latestRoundData` sets both `roundId` and `answeredInRound` to `uint80(price.publishTime)`. This means `answeredInRound == roundId` is always true, which causes protocols that apply the standard Chainlink staleness guard `require(answeredInRound >= roundId)` to pass unconditionally, even when the price is days old. [5](#0-4) 

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as its Chainlink-compatible price oracle — which is the contract's explicit purpose per the official migration guide — will silently consume stale prices whenever the on-chain Pyth feed has not been recently updated. Concrete impacts:

- **Lending/borrowing protocols**: Collateral valued at a stale (inflated) price allows over-borrowing; stale (deflated) prices trigger wrongful liquidations.
- **Derivatives/perpetuals**: Positions can be opened or closed at prices that no longer reflect market reality.
- **Standard Chainlink staleness guards are bypassed**: Because `answeredInRound == roundId` is always true, the most common downstream check provides no protection.

The migration guide explicitly directs protocols to pass `PythAggregatorV3` addresses to existing Chainlink-consuming apps, meaning the affected surface is every protocol that followed Pyth's own migration documentation. [6](#0-5) 

---

### Likelihood Explanation

Pyth is a pull oracle. If no keeper or scheduler is actively pushing updates for a given feed, the on-chain price can become arbitrarily stale. This is a documented operational reality: the migration guide itself notes that apps "may need to schedule these price updates themselves." During network congestion, keeper downtime, or for less-liquid feeds with infrequent updates, the price stored in the Pyth contract can lag by minutes to hours. Any user who monitors the on-chain price lag can time their interaction with a downstream protocol to exploit the stale value.

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` in all price-returning functions, and add a configurable `maxAge` parameter to the constructor:

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

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

This causes `latestRoundData()` to revert with `StalePrice` when the feed is stale, matching the behavior downstream protocols expect from a Chainlink feed that has exceeded its heartbeat.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to the Pyth contract and a price feed ID.
2. Allow the on-chain Pyth price to become stale (no `updatePriceFeeds` call for > N seconds).
3. Call `latestRoundData()` — it returns the old price with `updatedAt = old_publishTime` and `answeredInRound == roundId` (always equal).
4. A downstream lending protocol consuming this adapter accepts the stale price as valid because:
   - The function did not revert.
   - `answeredInRound >= roundId` passes.
5. An attacker uses the stale (e.g., inflated) price to borrow more than the current market value of their collateral, then withdraws, leaving the protocol with bad debt.

The root cause is entirely within Pyth's own SDK code at `PythAggregatorV3.sol` lines 41, 54, 59, 89, and 110 — all of which call `getPriceUnsafe` with no staleness enforcement. [7](#0-6)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L40-56)
```text
    function decimals() public view virtual returns (uint8) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return uint8(-1 * int8(price.expo));
    }

    function description() public pure returns (string memory) {
        return "A port of a chainlink aggregator powered by pyth network feeds";
    }

    function version() public pure returns (uint256) {
        return 1;
    }

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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L44-97)
```text

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.

The following `forge` deployment script demonstrates the expected deployment process:

```solidity copy
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "forge-std/Script.sol";
import { PythAggregatorV3 } from "@pythnetwork/pyth-sdk-solidity/PythAggregatorV3.sol";
import { ChainlinkApp } from "./ChainlinkApp.sol";

contract PythAggregatorV3Deployment is Script {
  function run() external {
    uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
    vm.startBroadcast(deployerPrivateKey);

    // Get the address for your ecosystem from:
    // https://docs.pyth.network/price-feeds/core/contract-addresses/evm
    address pythPriceFeedsContract = 0xff1a0f4744e8582DF1aE09D5611b887B6a12925C;
    // Get the price feed ids from:
    // https://docs.pyth.network/price-feeds/core/price-feeds
    bytes32 ethFeedId = 0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace;
    bytes32 solFeedId = 0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d;

    // Deploy an instance of PythAggregatorV3 for every feed.
    PythAggregatorV3 ethAggregator = new PythAggregatorV3(
      pythPriceFeedsContract,
      ethFeedId
    );
    PythAggregatorV3 solAggregator = new PythAggregatorV3(
      pythPriceFeedsContract,
      solFeedId
    );

    // Pass the address of the PythAggregatorV3 contract to your chainlink-compatible app.
    ChainlinkApp app = new ChainlinkApp(
      address(ethAggregator),
      address(solAggregator)
    );

    vm.stopBroadcast();
  }
}

```

Please see the [Chainlink Migration Example](https://github.com/pyth-network/pyth-examples/tree/main/price_feeds/evm/chainlink_migration) for a runnable version of the example above.
```
