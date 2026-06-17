### Title
Stale Price Returned Without Staleness Check in `latestRoundData()` and `latestAnswer()` — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary

`PythAggregatorV3.sol` is Pyth's official Chainlink AggregatorV3-compatible adapter, shipped as part of the production EVM SDK. Every price-returning function in this contract — `latestAnswer()`, `latestRoundData()`, `getRoundData()`, and `decimals()` — calls `pyth.getPriceUnsafe(priceId)` with no staleness check and no positive-price validation. Any protocol that deploys this adapter as a drop-in Chainlink oracle will silently consume arbitrarily stale or non-positive prices.

### Finding Description

`PythAggregatorV3.sol` implements the Chainlink `AggregatorV3Interface` on top of Pyth price feeds. All four price-reading functions delegate to `getPriceUnsafe`:

```solidity
// line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// line 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe` is explicitly documented as returning a price "from arbitrarily far in the past" with no recency guarantee: [3](#0-2) 

The safe alternative, `getPriceNoOlderThan`, enforces a staleness bound and reverts on stale data: [4](#0-3) 

Neither `latestRoundData()` nor `latestAnswer()` perform:
1. A staleness check (comparing `price.publishTime` against `block.timestamp`)
2. A positive-price check (`price.price > 0`), even though `PythStructs.Price.price` is `int64` and can be zero or negative [5](#0-4) 

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink-compatible oracle (lending protocols, derivatives, liquidation engines) will:

- Receive a stale price from `latestRoundData()` / `latestAnswer()` without any revert during a Pyth feed outage or network congestion period.
- Potentially receive a zero or negative `answer`, which downstream protocols may misinterpret (e.g., treating a negative price as a very large `uint256` after an unsafe cast, or accepting a zero price as valid collateral valuation).

This directly enables incorrect liquidations, undercollateralized borrows, or mispriced derivative settlements — all triggered by an unprivileged transaction sender who simply calls the integrating protocol at the right moment.

### Likelihood Explanation

Pyth price feeds can become stale during network outages, guardian set transitions, or periods of low keeper activity. The `PythAggregatorV3` adapter is the official SDK artifact for Chainlink-compatible integrations, making it the natural choice for protocols migrating from Chainlink. The attack requires no privileged access: an attacker only needs to observe that the on-chain Pyth price is stale and then interact with a protocol using this adapter.

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` in all price-returning functions, using a configurable `maxAge` parameter set at construction time. Additionally, add a positive-price guard:

```solidity
uint public maxAge; // set in constructor

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    require(price.price > 0, "Non-positive price");
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing at a live Pyth EVM contract and a valid `priceId`.
2. Allow the on-chain Pyth price to go stale (e.g., stop calling `updatePriceFeeds` for longer than the valid time period).
3. Call `latestRoundData()` on the adapter — it returns the stale price with no revert.
4. A lending protocol reading this adapter will use the stale price for collateral valuation, enabling an attacker to borrow against overvalued or undervalued collateral.

The root cause is entirely within Pyth's own SDK file: [6](#0-5)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L1-119)
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
