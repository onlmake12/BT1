### Title
`PythAggregatorV3.latestRoundData()` and `latestAnswer()` Return Stale Prices Without Staleness Check — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary
`PythAggregatorV3`, Pyth's official Chainlink `AggregatorV3Interface` adapter in the production Solidity SDK, calls `getPriceUnsafe()` in every price-reading function — `latestAnswer()`, `latestRoundData()`, and `getRoundData()` — without any staleness check. Protocols that deploy this adapter as a drop-in Chainlink replacement will silently receive arbitrarily old prices, enabling price manipulation and incorrect financial decisions.

### Finding Description
`PythAggregatorV3.sol` is Pyth's production Solidity SDK contract that wraps a Pyth price feed behind the Chainlink `AggregatorV3Interface`. It is the recommended migration path for Chainlink-compatible protocols (per Pyth's official migration guide).

Every price-reading function in the contract delegates to `pyth.getPriceUnsafe(priceId)`:

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
```

`getPriceUnsafe()` is explicitly documented as returning a price "from arbitrarily far in the past" with no recency guarantee. The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, which may be hours or days old, but the contract never reverts or signals staleness. The correct Pyth API for staleness-checked reads is `getPriceNoOlderThan(id, age)`, which reverts with `StalePrice` if the price is too old. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
Any protocol that deploys `PythAggregatorV3` as its Chainlink-compatible oracle and calls `latestAnswer()` or `latestRoundData()` will receive a stale price with no on-chain indication that the data is outdated. Downstream effects include:

- **Incorrect liquidations**: A stale (artificially low or high) price can trigger or block liquidations that should not occur.
- **Incorrect collateral valuation**: Lending protocols using this adapter will mis-price collateral, enabling under-collateralized borrowing.
- **Adversarial price selection**: As documented in Pyth's own best practices, the pull-oracle model allows a user to select which historical price update to submit. Without a staleness check, an attacker can submit an old, favorable price and then interact with the protocol at that stale price. [4](#0-3) 

### Likelihood Explanation
Pyth's official migration guide explicitly instructs Chainlink users to deploy `PythAggregatorV3` as a drop-in replacement and pass its address to their existing Chainlink-compatible app. Protocols following this guide will not add their own staleness check because they expect the oracle adapter to handle it — as Chainlink's own aggregators do via heartbeat and deviation thresholds. The likelihood of a deployed protocol being affected is high given the adapter is the recommended migration path. [5](#0-4) 

### Recommendation
Replace all `getPriceUnsafe()` calls in `PythAggregatorV3` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable constructor parameter (e.g., defaulting to `3600` seconds). This mirrors the staleness guarantee that Chainlink consumers expect:

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
    ...
}
``` [3](#0-2) 

### Proof of Concept

1. A protocol (e.g., a lending market) deploys `PythAggregatorV3` following Pyth's migration guide, passing it as the oracle to their Chainlink-compatible collateral valuation logic.
2. The Pyth on-chain price for the feed is last updated at `T=0` (e.g., ETH/USD = $3000).
3. At `T=3600` (1 hour later), the real ETH price has dropped to $2000, but no one has submitted a price update to the Pyth contract.
4. An attacker calls `latestAnswer()` or `latestRoundData()` on `PythAggregatorV3`. The contract calls `getPriceUnsafe()`, which returns the 1-hour-old price of $3000 with no revert.
5. The lending protocol uses $3000 as the collateral price, allowing the attacker to borrow against over-valued collateral or avoid a liquidation that should have been triggered at $2000.
6. No privileged access is required; the attacker only needs to avoid submitting a fresh price update (or simply act before anyone else does). [6](#0-5) [7](#0-6)

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

**File:** apps/developer-hub/content/docs/price-feeds/core/best-practices.mdx (L44-52)
```text
Pull updates give users of Pyth Network some ability to select which price to use in a transaction.
This ability is highly circumscribed by various constraints: on-chain prices must move forward in time and cannot be from too far in the past.
However, users can still choose any price update that satisfies these constraints.
This ability is functionally equivalent to latency: it allows users to see the price in the future before using a price from the past.

The simplest way to guard against this attack vector is to incorporate a **staleness check** to ensure that the price used in a transaction is sufficiently recent.

The Pyth SDK provides the [getPriceNoOlderThan()](https://api-reference.pyth.network/price-feeds/evm/getPriceNoOlderThan) method to help users guard against this attack vector. This method returns the most recent price update that is not older than a specified threshold.
Highly latency-sensitive protocols may wish to reduce the threshold to a few seconds to better suit their needs.
```

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L44-51)
```text

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.

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
