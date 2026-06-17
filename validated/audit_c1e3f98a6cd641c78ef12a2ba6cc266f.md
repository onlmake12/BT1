### Title
Stale Price Returned by `PythAggregatorV3` Without Staleness Enforcement — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is the official Pyth-provided Chainlink-compatible adapter contract, distributed as part of the Pyth Solidity SDK and actively recommended in Pyth's migration guide. Every price-reading function in this contract calls `getPriceUnsafe()`, which returns a price from arbitrarily far in the past with no staleness check. Any protocol that integrates this adapter — expecting Chainlink-like freshness guarantees — will silently consume stale prices.

---

### Finding Description

`PythAggregatorV3.sol` implements the Chainlink `AggregatorV3Interface` by wrapping Pyth price feeds. All five price-reading functions — `latestRoundData()`, `getRoundData()`, `latestAnswer()`, `latestTimestamp()`, and `decimals()` — call `pyth.getPriceUnsafe(priceId)` directly:

```solidity
// Line 54
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// Line 110
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
}
```

`getPriceUnsafe()` is explicitly documented in `IPyth.sol` as returning "the most recent price update in this contract without any recency checks" and that "the returned price update may be arbitrarily far in the past."

The contract does provide a separate `updateFeeds()` function, but it is not part of the `AggregatorV3Interface` and is never called atomically within any of the read functions. There is no on-chain enforcement that `updateFeeds()` was called before `latestRoundData()` is consumed.

Chainlink-compatible protocols that call `latestRoundData()` typically check `updatedAt` for staleness, but `PythAggregatorV3` sets `updatedAt = price.publishTime` — the Pyth publish timestamp — which can be arbitrarily old if the feed has not been pushed recently. The contract provides no revert path for stale data.

---

### Impact Explanation

Any DeFi protocol (lending, derivatives, liquidation engine) that:
1. Deploys `PythAggregatorV3` following Pyth's official migration guide, and
2. Relies on `latestRoundData()` or `latestAnswer()` for critical decisions (collateral valuation, liquidation triggers, position sizing)

...will use a price that may be hours or days old during periods of low push-feed activity or network congestion. This is directly analogous to the Fei Protocol bug: a critical protocol function reads a price without first ensuring it is fresh.

Concrete impacts:
- **Undercollateralized borrowing**: A stale high price for collateral allows over-borrowing.
- **Missed liquidations**: A stale price that hasn't reflected a crash prevents timely liquidation.
- **Incorrect derivative settlement**: Positions settled at stale prices cause direct fund loss.

---

### Likelihood Explanation

The `PythAggregatorV3` contract is the **official** Pyth-provided migration path for Chainlink users, documented at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx`. Any protocol following this guide and not separately scheduling push updates will be exposed. The Pyth documentation itself notes that push feeds may not cover all price IDs, making the stale-read scenario realistic. An unprivileged attacker can exploit this by simply not calling `updateFeeds()` before interacting with a protocol that uses this adapter — or by timing transactions to occur when the on-chain price is stale.

---

### Recommendation

Add an on-chain staleness check inside `latestRoundData()` and `getRoundData()`. The contract should either:

1. Accept a `maxAge` parameter and call `getPriceNoOlderThan()` instead of `getPriceUnsafe()`, or
2. Revert if `block.timestamp - price.publishTime > validTimePeriod` (mirroring `AbstractPyth.sol`'s `getPriceNoOlderThan` logic).

At minimum, add a `require` guard:
```solidity
require(
    block.timestamp - price.publishTime <= validTimePeriod,
    "Stale price"
);
```

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a Pyth contract where the last `updatePriceFeeds()` call was 2 hours ago.
2. Call `latestRoundData()` — it returns `updatedAt = price.publishTime` from 2 hours ago with no revert.
3. A lending protocol consuming this adapter accepts the 2-hour-old price as current, allowing a borrower to take out a loan against collateral whose real value has dropped 30%.
4. No privileged access is required; any user can trigger the lending protocol's borrow function.

**Root cause in code:** [1](#0-0) 

`latestRoundData()` calls `getPriceUnsafe()` with no staleness enforcement. [2](#0-1) 

`latestAnswer()` has the same issue. [3](#0-2) 

`getPriceUnsafe()` is explicitly documented as having no recency checks. [4](#0-3) 

The correct pattern — `getPriceNoOlderThan()` with a staleness revert — exists in `AbstractPyth.sol` but is not used by `PythAggregatorV3`.

### Citations

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
