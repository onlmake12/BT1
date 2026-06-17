### Title
Stale Price Returned Without Validation in Chainlink-Compatible Adapter - (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary
`PythAggregatorV3.sol` is a production Pyth SDK contract that implements the Chainlink `AggregatorV3Interface`. Every price-reading function (`latestRoundData`, `getRoundData`, `latestAnswer`, `latestTimestamp`, `decimals`) calls `pyth.getPriceUnsafe()`, which explicitly makes no staleness guarantees and can return a price from arbitrarily far in the past. No staleness check, no `price > 0` guard, and no meaningful round-completeness check are applied before returning data to callers.

### Finding Description

`PythAggregatorV3.sol` is distributed as part of the official Pyth EVM Solidity SDK for protocols that want a drop-in Chainlink-compatible oracle. All five price-reading functions use `getPriceUnsafe`:

```solidity
// line 41
function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return uint8(-1 * int8(price.expo));
}

// line 53-56
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// line 89
function getRoundData(...) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return (_roundId, int256(price.price), price.publishTime, price.publishTime, _roundId);
}

// line 110
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

`IPyth.getPriceUnsafe` is explicitly documented: *"This function returns the most recent price update in this contract without any recency checks. This function is unsafe as the returned price update may be arbitrarily far in the past."*

Three specific defects exist:

1. **No staleness check**: `publishTime` is never compared against `block.timestamp`. A price that is hours or days old is returned silently.
2. **No positive-price guard**: `price.price` is `int64` and can be zero or negative. No `require(price.price > 0)` is present.
3. **Fake round-completeness**: `answeredInRound` is set to `roundId = uint80(price.publishTime)`. Any downstream consumer checking `answeredInRound >= roundId` will always pass trivially, providing false assurance of round completeness. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink-compatible price source (lending, derivatives, stablecoins) will silently consume stale prices. During a network outage or Pyth update gap, the on-chain price can lag the true market price by an unbounded amount. This enables:

- **Incorrect liquidations**: A stale high price prevents a borrower from being liquidated when they should be, or a stale low price triggers wrongful liquidation.
- **Mispriced collateral**: Protocols computing collateral ratios will use an outdated price, allowing over-borrowing or blocking valid withdrawals.
- **Arbitrage against the protocol**: An attacker who knows the true market price has moved significantly can interact with the protocol using the stale on-chain price to extract value.

The `int256(price.price)` cast with no positivity check also means a zero or negative price (possible if the Pyth feed is misconfigured or uninitialized) propagates directly to callers.

### Likelihood Explanation

`PythAggregatorV3.sol` is the official Pyth SDK adapter for Chainlink-compatible integrations and is actively used by protocols. Any unprivileged user can trigger the vulnerable path simply by calling a function on a protocol that reads from this adapter (e.g., a borrow, liquidate, or swap call). No special role or key is required. The Pyth network has experienced update gaps in the past, making stale-price windows realistic. [4](#0-3) 

### Recommendation

Replace all `getPriceUnsafe` calls with `getPriceNoOlderThan` using a configurable `maxAge` parameter, and add a positivity check on the returned price:

```solidity
uint256 public maxPriceAge; // e.g., 60 seconds, set in constructor

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
    // Reverts with PythErrors.StalePrice if price is too old
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxPriceAge);
    require(price.price > 0, "PythAggregatorV3: price <= 0");
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

Apply the same pattern to `latestAnswer`, `getRoundData`, `latestTimestamp`, and `decimals`. [5](#0-4) 

### Proof of Concept

1. A lending protocol deploys `PythAggregatorV3` pointing to a BTC/USD Pyth feed.
2. Pyth's off-chain price service experiences a 30-minute outage; no `updatePriceFeeds` calls are made.
3. The on-chain Pyth price is now 30 minutes stale (e.g., $60,000) while the true market price has dropped to $50,000.
4. An attacker calls the lending protocol's `borrow()` function. The protocol calls `latestRoundData()` on `PythAggregatorV3`, which calls `getPriceUnsafe()` and returns the stale $60,000 price with no revert.
5. The attacker borrows against the inflated collateral value, extracting funds the protocol would not have permitted at the true price.
6. The `answeredInRound == roundId` check that downstream consumers may apply passes trivially (both are `uint80(price.publishTime)`), giving false confidence that the data is fresh. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L7-16)
```text
// This interface is forked from the Zerolend Adapter found here:
// https://github.com/zerolend/pyth-oracles/blob/master/contracts/PythAggregatorV3.sol
// Original license found under licenses/zerolend-pyth-oracles.md

/**
 * @title A port of the ChainlinkAggregatorV3 interface that supports Pyth price feeds
 * @notice This does not store any roundId information on-chain. Please review the code before using this implementation.
 * Users should deploy an instance of this contract to wrap every price feed id that they need to use.
 */
contract PythAggregatorV3 {
```

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
