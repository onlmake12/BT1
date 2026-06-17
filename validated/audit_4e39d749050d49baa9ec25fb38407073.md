### Title
Missing Staleness Validation in `PythAggregatorV3` Price Functions Returns Arbitrarily Stale Prices — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter contract, explicitly designed for protocols migrating from Chainlink's `AggregatorV3Interface` to Pyth price feeds. Every price-returning function — `latestAnswer()`, `latestRoundData()`, and `getRoundData()` — calls `pyth.getPriceUnsafe()`, which returns prices from arbitrarily far in the past with zero staleness validation. This is the direct Pyth analog of the Chainlink minAnswer/maxAnswer circuit breaker issue: instead of a clamped boundary price, consuming protocols silently receive an arbitrarily stale price with no revert or warning.

---

### Finding Description

All price-returning functions in `PythAggregatorV3` use `getPriceUnsafe()`:

```solidity
// latestAnswer() — line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData() — line 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}

// getRoundData() — line 76-97
function getRoundData(uint80 _roundId) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return (_roundId, int256(price.price), price.publishTime, price.publishTime, _roundId);
}
```

`getPriceUnsafe()` is explicitly documented in `IPyth.sol` as: *"This function returns the most recent price update in this contract without any recency checks. This function is unsafe as the returned price update may be arbitrarily far in the past."*

There is no staleness check, no negative price check, and no zero price check in any of these functions. The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, which could be hours, days, or weeks old. Protocols that check `updatedAt` can detect this, but many Chainlink-compatible protocols do not, because Chainlink's own aggregators enforce freshness at the aggregator level.

The `PythAggregatorV3` contract is the official Pyth SDK contract recommended for Chainlink migration, as documented in the Chainlink migration guide.

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink drop-in replacement and calls `latestRoundData()` or `latestAnswer()` will silently receive a stale price with no revert. If the Pyth price feed is not updated on-chain (e.g., due to network issues, keeper failure, or deliberate non-updating), the protocol continues operating at the last cached price, which may be significantly different from the current market price. This enables:

- **Borrowing at incorrect collateral valuations** — an asset that has crashed in value is still priced at its last on-chain value, allowing over-borrowing
- **Liquidations at incorrect prices** — healthy positions may be liquidated or insolvent positions may be protected
- **Arbitrage at the protocol's expense** — attackers can exploit the price discrepancy between the stale on-chain price and the real market price

This is structurally identical to the Venus/LUNA incident described in M-12: the oracle continues to report an old price while the real market price has moved dramatically.

---

### Likelihood Explanation

The likelihood is high because:

1. `PythAggregatorV3` is the official Pyth SDK contract explicitly recommended for Chainlink migration
2. Protocols migrating from Chainlink expect `latestRoundData()` to behave like Chainlink's — returning a recent price or reverting
3. Many Chainlink-compatible protocols do not check `updatedAt` from `latestRoundData()` because Chainlink aggregators enforce freshness internally
4. The contract's own comment only warns about `roundId` not being stored, not about the staleness issue in price functions
5. Any unprivileged user can trigger the exploit simply by interacting with the consuming protocol when the price feed is stale — no special access is required

---

### Recommendation

Replace `getPriceUnsafe()` with `getPriceNoOlderThan()` in all price-returning functions. Add a configurable `maxAge` constructor parameter:

```solidity
constructor(address _pyth, bytes32 _priceId, uint _maxAge) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    maxAge = _maxAge; // e.g., 60 seconds for DeFi lending
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

Additionally, add a non-positive price check (analogous to `PythPriceOracleGetter`'s `InvalidNonPositivePrice` check) to guard against negative or zero prices being returned.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a Pyth price feed for ETH/USD
2. Update the price feed once (e.g., ETH = $3000)
3. Wait for the price to become stale (no on-chain update for >60 seconds)
4. ETH market price drops to $1000
5. Call `latestRoundData()` on `PythAggregatorV3` — it returns `answer = 3000e8` with no revert
6. A lending protocol using this adapter allows borrowing against ETH collateral at $3000 instead of $1000
7. Attacker deposits ETH, borrows at the inflated $3000 price, and walks away with excess borrowed assets

The root cause is entirely within Pyth's own `PythAggregatorV3.sol`: [1](#0-0) [2](#0-1) [3](#0-2) 

The `getPriceUnsafe()` function's documented behavior confirms the root cause: [4](#0-3) 

The contrast with the safer `getPriceNoOlderThan()` — which reverts on stale prices — is clear: [5](#0-4) 

The Chainlink migration guide explicitly directs protocols to use `PythAggregatorV3` as a drop-in replacement, making this a high-likelihood attack surface: [6](#0-5)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L53-56)
```text
    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return int256(price.price);
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L76-97)
```text
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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L44-51)
```text

First, deploy the `PythAggregatorV3` contract from `@pythnetwork/pyth-sdk-solidity` as a replacement for your application's Chainlink price feeds.
`PythAggregatorV3` is an adapter contract that wraps the Pyth contract and implements Chainlink's `AggregatorV3Interface`.

One important difference between Pyth and Chainlink is that the Pyth contract holds data for all price feeds; in contrast, Chainlink has separate instances of `AggregatorV3Interface` for each feed.
The adapter contract resolves this discrepancy by wrapping a single Pyth price feed.
Users should deploy an instance of this adapter for every required price feed, then point their existing app to the addresses of the deployed adapter contracts.

```
