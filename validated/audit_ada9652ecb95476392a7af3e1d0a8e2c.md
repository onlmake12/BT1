### Title
`latestRoundData()` Returns Stale Price Without Staleness Validation - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.latestRoundData()` calls `pyth.getPriceUnsafe()` unconditionally, returning an arbitrarily old price with no on-chain recency check. Any protocol that integrates this Chainlink-compatible adapter and relies on `latestRoundData()` for financial decisions can be exploited using a stale price.

---

### Finding Description

`PythAggregatorV3` is a production Solidity contract in the Pyth EVM SDK that wraps Pyth price feeds behind the Chainlink `AggregatorV3Interface`. Every read function — `latestRoundData()`, `getRoundData()`, `latestAnswer()`, `latestTimestamp()`, and `decimals()` — calls `pyth.getPriceUnsafe(priceId)`: [1](#0-0) 

`getPriceUnsafe` is explicitly documented in `IPyth.sol` as returning a price that "may be arbitrarily far in the past" with no recency guarantee: [2](#0-1) 

The safer alternative, `getPriceNoOlderThan(id, age)`, is available in the same interface and is used correctly in the Aave adapter (`PythPriceOracleGetter.sol`): [3](#0-2) 

`PythAggregatorV3` has no equivalent `age` parameter, no configurable `validTimePeriod`, and no revert path for stale data. The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, which can be arbitrarily old: [4](#0-3) 

---

### Impact Explanation

Any DeFi protocol (lending, derivatives, liquidation engine) that integrates `PythAggregatorV3` as a drop-in Chainlink replacement and calls `latestRoundData()` without adding its own staleness check will silently consume an arbitrarily old price. This can lead to:

- Undercollateralized loans being opened or liquidations being blocked/triggered incorrectly
- Derivative positions priced at stale values, enabling risk-free profit extraction
- Flash-loan attacks exploiting the gap between the stale on-chain price and the true market price

---

### Likelihood Explanation

`PythAggregatorV3` is the canonical Chainlink-compatible adapter shipped in the official Pyth Solidity SDK. Protocols migrating from Chainlink or using it as a drop-in replacement will naturally call `latestRoundData()` and trust the returned `updatedAt` value. The Chainlink ecosystem convention is that `updatedAt` reflects a recent heartbeat; here it reflects an unchecked `publishTime`. The mismatch is non-obvious and the contract carries only a generic "please review" disclaimer with no staleness guard.

---

### Recommendation

Replace `getPriceUnsafe` with `getPriceNoOlderThan` and add a configurable `maxAge` parameter to `PythAggregatorV3`, mirroring the pattern in `PythPriceOracleGetter`:

```solidity
uint256 public maxAge; // set in constructor

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    ...
}
```

This matches the `validTimePeriodSeconds` pattern already used in `PythPriceOracleGetter.sol`. [5](#0-4) 

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract and a price feed ID.
2. Allow the price feed to go stale (no `updatePriceFeeds` call for > N seconds).
3. Call `latestRoundData()` — it returns the old `price.price` and an old `updatedAt` without reverting.
4. A lending protocol using this adapter will accept the stale price as current, enabling an attacker to borrow against inflated/deflated collateral values.

The root cause is entirely within `PythAggregatorV3.sol` lines 110–118: `getPriceUnsafe` is called with no age bound, and no revert path exists for stale data. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L53-56)
```text
    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return int256(price.price);
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L89-96)
```text
        PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
        return (
            _roundId,
            int256(price.price),
            price.publishTime,
            price.publishTime,
            _roundId
        );
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

**File:** target_chains/ethereum/contracts/contracts/aave/PythPriceOracleGetter.sol (L29-50)
```text
    constructor(
        address pyth,
        address[] memory assets,
        bytes32[] memory priceIds,
        address baseCurrency,
        uint256 baseCurrencyUnit,
        uint validTimePeriodSeconds
    ) {
        if (baseCurrencyUnit == 0) {
            revert InvalidBaseCurrencyUnit();
        }
        PythAssetRegistry.setPyth(pyth);
        PythAssetRegistry.setAssetsSources(assets, priceIds);
        PythAssetRegistry.setBaseCurrency(baseCurrency, baseCurrencyUnit);
        BASE_CURRENCY = _registryState.BASE_CURRENCY;
        BASE_CURRENCY_UNIT = _registryState.BASE_CURRENCY_UNIT;
        if ((10 ** baseNumDecimals(baseCurrencyUnit)) != baseCurrencyUnit) {
            revert InvalidBaseCurrencyUnit();
        }
        BASE_NUM_DECIMALS = baseNumDecimals(baseCurrencyUnit);
        PythAssetRegistry.setValidTimePeriodSeconds(validTimePeriodSeconds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/aave/PythPriceOracleGetter.sol (L63-66)
```text
        PythStructs.Price memory price = pyth().getPriceNoOlderThan(
            priceId,
            PythAssetRegistry.validTimePeriodSeconds()
        );
```
