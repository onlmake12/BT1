### Title
`PythAggregatorV3::latestAnswer()` and `latestRoundData()` expose stale prices without staleness enforcement - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3`, the official Pyth SDK contract providing a Chainlink AggregatorV3-compatible interface, calls `pyth.getPriceUnsafe()` in every price-returning function without any `publishTime` recency check. Any protocol that integrates `PythAggregatorV3` as a drop-in Chainlink oracle — exactly the pattern described in the external report — will silently receive arbitrarily stale prices with no revert or warning.

---

### Finding Description

`PythAggregatorV3` is the Pyth-provided SDK contract that wraps Pyth price feeds behind the Chainlink `AggregatorV3Interface`. Every function that returns a price value calls `pyth.getPriceUnsafe(priceId)` unconditionally:

- `latestAnswer()` — returns `int256(price.price)` with no age check
- `latestRoundData()` — returns `int256(price.price)` with no age check
- `getRoundData()` — returns `int256(price.price)` with no age check
- `decimals()` — reads `price.expo` with no age check [1](#0-0) [2](#0-1) 

`getPriceUnsafe` is explicitly documented to return a price from arbitrarily far in the past with no recency guarantee: [3](#0-2) 

The safe alternative, `getPriceNoOlderThan`, enforces a `publishTime` age bound and reverts with `StalePrice` if the threshold is exceeded: [4](#0-3) 

`PythAggregatorV3` never calls `getPriceNoOlderThan` and provides no `maxAge` configuration. The `latestTimestamp()` helper exists but is never consulted internally: [5](#0-4) 

---

### Impact Explanation

Protocols that use `PythAggregatorV3` as a Chainlink-compatible oracle — lending protocols, liquidation engines, derivatives platforms — will receive stale prices silently. A stale price during a volatile market period allows an attacker to:

1. Borrow against collateral at an inflated stale price, draining the lending pool.
2. Block or trigger incorrect liquidations by exploiting the divergence between the stale on-chain price and the true market price.

This matches the external report's impact class: **direct theft of user funds**.

---

### Likelihood Explanation

`PythAggregatorV3` is the official, Pyth-distributed Chainlink compatibility shim. It is the exact contract deployed by integrators such as the AaveOracle described in the external report. The attack requires no privileged access: any unprivileged transaction sender can interact with a downstream protocol that uses `PythAggregatorV3` while the on-chain price feed is stale. Price feeds can become stale during network congestion, keeper outages, or low-liquidity periods — all realistic conditions.

---

### Recommendation

Add a configurable `maxAge` parameter to the `PythAggregatorV3` constructor and replace every `getPriceUnsafe` call with `getPriceNoOlderThan(priceId, maxAge)`. This causes `latestAnswer()` and `latestRoundData()` to revert with `StalePrice` when the feed is too old, matching the behavior downstream consumers expect from a safe Chainlink-compatible oracle.

```solidity
constructor(address _pyth, bytes32 _priceId, uint256 _maxAge) {
    priceId = _priceId;
    pyth = IPyth(_pyth);
    maxAge = _maxAge;
}

function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    return int256(price.price);
}
```

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing at a live Pyth contract and a price feed ID.
2. Allow the on-chain price feed to go stale (no keeper update for > `getValidTimePeriod()` seconds).
3. Call `latestAnswer()` — it returns the stale price without reverting.
4. Call `pyth.getPriceNoOlderThan(priceId, 60)` directly — it reverts with `StalePrice`.
5. A downstream protocol using `latestAnswer()` (e.g., an AaveOracle-style contract) will use the stale price for collateral valuation, enabling the attacker to borrow at incorrect rates or avoid liquidation.

The NatSpec on `getPriceUnsafe` in the Pyth interface confirms the root cause: [6](#0-5)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L53-56)
```text
    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return int256(price.price);
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L58-61)
```text
    function latestTimestamp() public view returns (uint256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return price.publishTime;
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
