### Title
`PythAggregatorV3`: All Price-Reading Functions Use `getPriceUnsafe()` With No Staleness Enforcement — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink `AggregatorV3Interface`-compatible adapter contract. Every price-reading function in the contract — `latestAnswer()`, `latestTimestamp()`, `latestRound()`, `getAnswer()`, `getTimestamp()`, `getRoundData()`, and `latestRoundData()` — calls `pyth.getPriceUnsafe(priceId)`, which is explicitly documented to return prices from arbitrarily far in the past with no recency guarantee. The contract adds no staleness check of its own. This is a direct structural analog to the Y2K `ControllerPeggedAssetV2` bug where `updatedAt` from Chainlink's `latestRoundData()` was never validated.

---

### Finding Description

In `PythAggregatorV3.sol`, every function that reads a price delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}
```

```solidity
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe()` is documented in `IPyth.sol` as: *"This function is unsafe as the returned price update may be arbitrarily far in the past."* [3](#0-2) 

Pyth's own staleness-safe API is `getPriceNoOlderThan(id, age)`, implemented in `AbstractPyth.sol`, which reverts with `StalePrice` if `block.timestamp - price.publishTime > age`: [4](#0-3) 

`PythAggregatorV3` never calls this safe variant. The contract is explicitly positioned as a migration path for Chainlink-dependent protocols: [5](#0-4) 

Two distinct staleness exposure surfaces exist:

1. **`latestAnswer()`** — returns only `int256(price.price)` with no timestamp. A caller has no way to detect staleness from this function's return value alone.
2. **`latestRoundData()`** — returns `updatedAt = price.publishTime`, so a diligent consumer *could* check it. However, the contract itself enforces nothing, and the Y2K bug class exists precisely because many Chainlink consumers do not check `updatedAt`.

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink adapter and reads prices via `latestAnswer()` or `latestRoundData()` (without manually checking `updatedAt`) will silently consume arbitrarily stale Pyth prices. Concrete downstream harms include:

- **Lending/borrowing protocols**: stale collateral prices allow under-collateralized borrows or block valid liquidations.
- **Depeg controllers** (the exact Y2K scenario): a stale peg price triggers or suppresses depeg events incorrectly.
- **Derivatives/perps**: stale mark prices cause incorrect funding rates or allow profitable position manipulation.

The Pyth documentation itself identifies this as a primary risk class and recommends `getPriceNoOlderThan()` for all production use: [6](#0-5) 

---

### Likelihood Explanation

- `PythAggregatorV3` is Pyth's official, published SDK contract for Chainlink migration. It is actively recommended in Pyth's developer documentation.
- Protocols migrating from Chainlink to Pyth via this adapter inherit the assumption that the adapter handles oracle safety — the same assumption that caused the Y2K bug.
- Pyth price feeds can go stale during network outages, market closures, or congestion. The window of staleness is real and exploitable.
- An unprivileged attacker needs only to call `latestAnswer()` or trigger a protocol action that reads it during a stale window — no special access required.

---

### Recommendation

Replace `getPriceUnsafe()` with `getPriceNoOlderThan()` in all price-reading functions, with a configurable `maxAge` parameter set at construction time:

```diff
+   uint256 public maxAge;

-   constructor(address _pyth, bytes32 _priceId) {
+   constructor(address _pyth, bytes32 _priceId, uint256 _maxAge) {
        priceId = _priceId;
        pyth = IPyth(_pyth);
+       maxAge = _maxAge;
    }

    function latestAnswer() public view virtual returns (int256) {
-       PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
+       PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
        return int256(price.price);
    }

    function latestRoundData() external view returns (...) {
-       PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
+       PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
        ...
    }
```

This mirrors the fix applied to Y2K's `ControllerPeggedAssetV2` and aligns with Pyth's own best-practices guidance.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a Pyth contract and a price feed ID.
2. Call `pyth.updatePriceFeeds()` to set a price at time `T`.
3. Advance block time by more than `getValidTimePeriod()` seconds (e.g., `vm.warp(block.timestamp + 120)`).
4. Call `aggregator.latestAnswer()` — it returns the price from time `T` with no revert.
5. Call `pyth.getPriceNoOlderThan(priceId, 60)` directly — it reverts with `StalePrice`.
6. The adapter silently returns the stale price while Pyth's own safe API correctly rejects it.

This demonstrates that `PythAggregatorV3` bypasses Pyth's built-in staleness protection, exposing any consumer to the exact vulnerability class described in the Y2K M-1 report. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L11-15)
```text
/**
 * @title A port of the ChainlinkAggregatorV3 interface that supports Pyth price feeds
 * @notice This does not store any roundId information on-chain. Please review the code before using this implementation.
 * Users should deploy an instance of this contract to wrap every price feed id that they need to use.
 */
```

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

**File:** apps/developer-hub/content/docs/price-feeds/core/best-practices.mdx (L36-40)
```text
Integrators should be careful to avoid accidentally using a stale price.
The Pyth SDKs guard against this failure mode by incorporating a staleness check by default.
Querying the current price will fail if too much time has elapsed since the last update.
The SDKs expose this failure condition in an idiomatic way: for example, the Rust SDK may return `None`, and the Solidity SDK may revert the transaction.
The SDK provides a sane default for the staleness threshold, but users may configure it to suit their use case.
```
