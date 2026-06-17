### Title
`PythAggregatorV3.latestRoundData()` and `latestAnswer()` Return Stale Prices Without Staleness Validation — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter, marketed as a drop-in replacement for `AggregatorV3Interface`. Every price-returning function in the contract calls `getPriceUnsafe()`, which is explicitly documented as returning a price "from arbitrarily far in the past" with no recency check. No staleness guard exists anywhere in the adapter. During a market crash or Pyth feed outage, the adapter silently returns the last cached (inflated) price, exposing any downstream protocol to the same class of incorrect-pricing impact described in the external report.

---

### Finding Description

`PythAggregatorV3.sol` implements the Chainlink `AggregatorV3Interface` by wrapping a single Pyth price feed. Every function that returns a price delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestAnswer — line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — line 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}

// getRoundData — line 76-97
function getRoundData(uint80 _roundId) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return (_roundId, int256(price.price), price.publishTime, price.publishTime, _roundId);
}
```

`IPyth.getPriceUnsafe` is documented as:

> "This function returns the most recent price update in this contract **without any recency checks**. This function is unsafe as the returned price update **may be arbitrarily far in the past**." [1](#0-0) 

`AbstractPyth.getPriceNoOlderThan` — the safe alternative — enforces a staleness bound and reverts with `StalePrice` if the price is too old: [2](#0-1) 

`PythAggregatorV3` never calls `getPriceNoOlderThan` and never inspects `publishTime` before returning the price. [3](#0-2) [4](#0-3) 

Additionally, `latestAnswer()` returns only `int256` — no timestamp at all — so callers have no mechanism to detect staleness from that function's return value. [3](#0-2) 

---

### Impact Explanation

Any protocol that integrates `PythAggregatorV3` as a Chainlink feed replacement and calls `latestAnswer()` or `latestRoundData()` will receive the last cached Pyth price with no indication that it is stale. During a market crash or a Pyth network outage:

- The on-chain price cache is not updated.
- `getPriceUnsafe` returns the last stored (pre-crash, inflated) price.
- The adapter returns this inflated price to the consuming protocol.
- The protocol mints excess stablecoins, under-collateralizes loans, or misprices derivatives — identical to the LUNA-crash scenario in the external report.

The impact is **incorrect asset pricing leading to protocol insolvency or excess minting**, matching the external report's impact class exactly.

---

### Likelihood Explanation

- `PythAggregatorV3` is part of Pyth's official EVM Solidity SDK and is explicitly documented as the migration path for Chainlink users.
- The [Chainlink migration guide](https://github.com/pyth-network/pyth-crosschain/blob/main/apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx) instructs protocols to deploy this contract and point their existing app at it.
- Protocols migrating from Chainlink typically trust the adapter to behave like a Chainlink feed; many do not add a secondary staleness check on `updatedAt`.
- No privileged access is required. Any read call to `latestAnswer()` or `latestRoundData()` by any user or protocol triggers the vulnerable path.
- Feed outages and market crashes are realistic, precedented events (LUNA, UST, FTX). [5](#0-4) 

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` in all price-returning functions, parameterized by a configurable `validTimePeriod`:

```solidity
uint256 public validTimePeriod;

constructor(address _pyth, bytes32 _priceId, uint256 _validTimePeriod) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    validTimePeriod = _validTimePeriod;
}

function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, validTimePeriod);
    return int256(price.price);
}

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, validTimePeriod);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

This causes the adapter to revert with `StalePrice` when the feed has not been updated within the acceptable window, preventing downstream protocols from consuming an inflated stale price.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing at a Pyth contract and a price feed (e.g., ETH/USD).
2. Call `updatePriceFeeds` once to seed the price at $3000.
3. Advance block time by 24 hours (simulating a feed outage or crash).
4. Call `latestAnswer()` — it returns `3000_00000000` (the stale pre-crash price) with no revert.
5. Call `latestRoundData()` — it returns `updatedAt = <24 hours ago>` and `answer = 3000_00000000`.
6. A protocol consuming this adapter without its own staleness check on `updatedAt` will price ETH at $3000 even if the true market price is $100.

The root cause is confirmed at: [6](#0-5) 

with `getPriceUnsafe` defined as returning prices with no recency guarantee at: [1](#0-0)

### Citations

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

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L40-119)
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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L44-51)
```text

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.

```
