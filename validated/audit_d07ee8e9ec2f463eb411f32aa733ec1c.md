### Title
Stale Price Returned Without Staleness Check in `PythAggregatorV3.latestRoundData()` and All Price-Reading Functions - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.sol` is the official Pyth-provided Chainlink `AggregatorV3Interface` adapter, explicitly recommended in Pyth's migration documentation as a drop-in replacement for Chainlink price feeds. Every price-reading function in this contract — `latestRoundData()`, `getRoundData()`, `latestAnswer()`, `latestTimestamp()`, and `decimals()` — calls `pyth.getPriceUnsafe(priceId)`, which performs **no staleness check** and returns a price from arbitrarily far in the past without reverting. Any protocol that deploys this adapter and reads prices through the standard Chainlink interface will silently receive stale prices with no on-chain protection.

---

### Finding Description

`PythAggregatorV3.sol` implements the Chainlink `AggregatorV3Interface` on top of Pyth. All five price-reading functions delegate to `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestRoundData — line 110
PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);

// getRoundData — line 89
PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);

// latestAnswer — line 54
PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);

// latestTimestamp — line 59
PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);

// decimals — line 41
PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
``` [1](#0-0) [2](#0-1) [3](#0-2) 

`getPriceUnsafe()` is explicitly documented in `IPyth.sol` as returning "the most recent price update in this contract **without any recency checks**" and that "the returned price update may be arbitrarily far in the past." The only guard it applies is a `publishTime == 0` check for feed existence:

```solidity
function getPriceUnsafe(bytes32 id) public view override returns (PythStructs.Price memory price) {
    PythInternalStructs.PriceInfo storage info = _state.latestPriceInfo[id];
    price.publishTime = info.publishTime;
    price.expo = info.expo;
    price.price = info.price;
    price.conf = info.conf;
    if (price.publishTime == 0) revert PythErrors.PriceFeedNotFound();
}
``` [4](#0-3) 

The safe alternative, `getPriceNoOlderThan(id, age)`, is available in `AbstractPyth.sol` and reverts with `PythErrors.StalePrice()` if `block.timestamp - price.publishTime > age`. It is never used in `PythAggregatorV3.sol`. [5](#0-4) 

Furthermore, `latestRoundData()` constructs its return values as:

```solidity
roundId = uint80(price.publishTime);
return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
``` [6](#0-5) 

This means `answeredInRound == roundId` always (trivially satisfied), `updatedAt == publishTime` (which can be hours or days old), and `answer` is the stale price — all without any revert or warning. A consuming protocol that applies the standard Chainlink staleness pattern (`require(answeredInRound >= roundId)`, `require(updatedAt != 0)`) will pass all checks even with a price that is arbitrarily old, because the adapter fabricates consistent but unchecked values.

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink drop-in replacement — as explicitly recommended in the official Pyth migration guide — and reads prices via `latestRoundData()` or `latestAnswer()` will silently consume stale prices. In Pyth's pull model, the on-chain price is only updated when someone calls `updatePriceFeeds()`. If no update has been submitted recently (e.g., during network congestion, market hours gaps, or simply because no keeper has run), the cached price can be hours or days old. The adapter returns this stale price with no revert, no staleness flag, and fabricated `roundId`/`updatedAt` values that pass naive Chainlink-style staleness checks. Downstream effects include: incorrect liquidation thresholds in lending protocols, mispriced collateral, incorrect NFT mint prices, and exploitable arbitrage by any user who observes the price divergence between the stale on-chain value and the true market price.

---

### Likelihood Explanation

The migration documentation at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx` explicitly instructs developers to deploy `PythAggregatorV3` as a drop-in Chainlink replacement and pass its address to existing Chainlink-consuming apps:

> "Deploy an instance of `PythAggregatorV3` for every feed... Pass the address of the `PythAggregatorV3` contract to your chainlink-compatible app." [7](#0-6) 

Protocols following this guide will not be aware that the adapter silently bypasses all staleness protection. The Pyth pull model makes stale prices a realistic steady-state condition — unlike Chainlink's push model, prices are only as fresh as the last `updatePriceFeeds()` call. Any unprivileged user transacting with a protocol using this adapter can exploit the stale price window.

---

### Recommendation

Replace all `getPriceUnsafe(priceId)` calls in `PythAggregatorV3.sol` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable constructor parameter (e.g., defaulting to 60 seconds for DeFi use cases). For `latestRoundData()` and `getRoundData()`, revert if the price is stale rather than returning fabricated round metadata. Example fix for `latestRoundData()`:

```solidity
uint public maxPriceAge; // set in constructor

function latestRoundData() external view returns (
    uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound
) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxPriceAge);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

Apply the same pattern to `getRoundData()`, `latestAnswer()`, `latestTimestamp()`, and `decimals()`.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to the Pyth contract and a valid `priceId`.
2. Call `updatePriceFeeds()` once to seed the on-chain price cache.
3. Wait longer than any reasonable staleness threshold (e.g., 1 hour) without calling `updatePriceFeeds()` again.
4. Call `latestRoundData()` on the adapter. It returns the 1-hour-old price with no revert.
5. The returned `updatedAt` is `price.publishTime` (1 hour ago), `answeredInRound == roundId` (trivially true), and `answer` is the stale price.
6. A lending protocol consuming this adapter will use the stale price for collateral valuation, enabling a user who knows the true current price to open under-collateralized positions or avoid valid liquidations. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L1-120)
```text
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import {PythStructs} from "./PythStructs.sol";
import {IPyth} from "./IPyth.sol";

// This interface is forked from the Zerolend Adapter found here:
// https://github.com/zerolend/pyth-oracles/blob/master/contracts/PythAggregatorV3.sol
// Original license found under licenses/zerolend-pyth-oracles.md

/**
 * @title A port of the ChainlinkAggregatorV3 interface that supports Pyth price feeds
 * @notice This does not store any roundId information on-chain. Please review the code before using this implementation.
 * Users should deploy an instance of this contract to wrap every price feed id that they need to use.
 */
contract PythAggregatorV3 {
    bytes32 public priceId;
    IPyth public pyth;

    constructor(address _pyth, bytes32 _priceId) {
        priceId = _priceId;
        pyth = IPyth(_pyth);
    }

    // Wrapper function to update the underlying Pyth price feeds. Not part of the AggregatorV3 interface but useful.
    function updateFeeds(bytes[] calldata priceUpdateData) public payable {
        // Update the prices to the latest available values and pay the required fee for it. The `priceUpdateData` data
        // should be retrieved from our off-chain Price Service API using the `hermes-client` package.
        // See section "How Pyth Works on EVM Chains" below for more information.
        uint fee = pyth.getUpdateFee(priceUpdateData);
        pyth.updatePriceFeeds{value: fee}(priceUpdateData);

        // refund remaining eth
        // solhint-disable-next-line no-unused-vars
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
    }

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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L43-50)
```text
## Deploy Adapter Contract

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.
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
