### Title
`PythAggregatorV3.latestAnswer()` / `latestRoundData()` Return Arbitrarily Stale Prices With No Staleness Guard — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter, explicitly recommended for deployment on L2 networks (Arbitrum, Optimism). Every price-reading function in the contract calls `pyth.getPriceUnsafe()`, which by definition returns prices from arbitrarily far in the past with no recency check. No staleness guard exists anywhere in the adapter. Downstream protocols that use this adapter as a drop-in Chainlink replacement and rely on `latestAnswer()` or `latestRoundData()` will silently consume stale prices.

---

### Finding Description

`PythAggregatorV3` implements Chainlink's `AggregatorV3Interface` and is the canonical migration path for protocols moving from Chainlink to Pyth on EVM chains, including L2s.

Every price-reading function delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
// PythAggregatorV3.sol lines 53-55
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// lines 99-119
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

`getPriceUnsafe` is documented in `IPyth.sol` as:

> "This function returns the most recent price update in this contract without any recency checks. This function is unsafe as the returned price update may be arbitrarily far in the past."

The same pattern applies to `decimals()` (line 41), `getRoundData()` (line 89), and `latestTimestamp()` (line 59) — all call `getPriceUnsafe` with no staleness check.

The `AbstractPyth.sol` implementation of `getPriceNoOlderThan` shows the correct pattern that is deliberately not used here:

```solidity
// AbstractPyth.sol lines 50-60
function getPriceNoOlderThan(bytes32 id, uint age) public view virtual override
    returns (PythStructs.Price memory price) {
    price = getPriceUnsafe(id);
    if (diff(block.timestamp, price.publishTime) > age)
        revert PythErrors.StalePrice();
    return price;
}
```

`PythAggregatorV3` never calls this checked variant.

---

### Impact Explanation

Any protocol that integrates `PythAggregatorV3` as a Chainlink feed replacement and calls `latestAnswer()` or `latestRoundData()` without manually inspecting `publishTime` will silently receive prices from arbitrarily far in the past. On L2 networks (Arbitrum, Optimism — both listed as deployment targets in the contract manager config), sequencer downtime or Pyth update interruptions cause the on-chain cached price to freeze. The adapter returns this frozen price as if it were current, with no revert or error signal. Downstream lending protocols, perpetuals, or liquidation engines that rely on this adapter can:

- Execute liquidations at incorrect prices
- Accept under-collateralized positions
- Settle derivatives at stale rates

---

### Likelihood Explanation

The Chainlink migration guide (`apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx`) explicitly recommends deploying `PythAggregatorV3` on L2 networks including Arbitrum and Optimism, and shows example deployment to `0xff1a0f4744e8582DF1aE09D5611b887B6a12925C` (the Arbitrum/Optimism Pyth contract address). Protocols following this guide will deploy the adapter and call `latestAnswer()` / `latestRoundData()` expecting Chainlink-equivalent behavior, which includes implicit staleness protection. The adapter provides none. The NatSpec notice ("Please review the code before using this implementation") is insufficient to prevent this misuse at scale.

---

### Recommendation

Replace `getPriceUnsafe` calls in `latestAnswer()`, `latestRoundData()`, and `getRoundData()` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable constructor parameter. Alternatively, add an explicit staleness check after each `getPriceUnsafe` call:

```solidity
uint256 public maxAge; // set in constructor

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    require(
        block.timestamp - price.publishTime <= maxAge,
        "PythAggregatorV3: stale price"
    );
    ...
}
```

---

### Proof of Concept

1. Deploy `PythAggregatorV3` on Arbitrum pointing to the live Pyth contract.
2. Stop pushing price updates (simulate sequencer downtime or Pyth update halt).
3. Wait longer than any reasonable staleness threshold (e.g., 10 minutes).
4. Call `latestAnswer()` — it returns the pre-halt price with no revert.
5. Call `latestRoundData()` — `updatedAt` equals the old `publishTime`, but the function succeeds silently.
6. A downstream lending protocol that checks `block.timestamp - updatedAt < threshold` will correctly detect staleness — but only if it implements that check itself. The adapter provides no protection.

The root cause is entirely within Pyth's production SDK: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
