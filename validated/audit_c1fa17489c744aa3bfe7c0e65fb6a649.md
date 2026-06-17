### Title
Missing Stale Price Check in `PythAggregatorV3.latestRoundData()` and `latestAnswer()` — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.sol` is Pyth's official Chainlink `AggregatorV3Interface` adapter. Every price-reading function in the contract — `latestRoundData()`, `latestAnswer()`, `getRoundData()`, `decimals()`, and `latestTimestamp()` — calls `pyth.getPriceUnsafe(priceId)`, which explicitly skips all staleness enforcement. No `publishTime` age check is performed before returning the price. Any protocol that deploys this adapter and calls `latestRoundData()` without independently validating `updatedAt` will silently consume an arbitrarily old price.

---

### Finding Description

`PythAggregatorV3` is the official Pyth SDK contract for protocols that want a Chainlink-compatible oracle interface. Its `latestRoundData()` implementation is:

```solidity
function latestRoundData()
    external view
    returns (uint80 roundId, int256 answer, uint256 startedAt,
             uint256 updatedAt, uint80 answeredInRound)
{
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId); // ← no staleness check
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime,
            price.publishTime, roundId);
}
``` [1](#0-0) 

`getPriceUnsafe()` is documented to return a price "from arbitrarily far in the past" with no recency guarantee: [2](#0-1) 

The same unsafe call is used in every other price-reading entry point:

- `latestAnswer()` — line 54
- `decimals()` — line 41
- `getRoundData()` — line 89
- `latestTimestamp()` — line 59 [3](#0-2) 

By contrast, Pyth's own `AbstractPyth.getPriceNoOlderThan()` enforces a maximum age and reverts with `StalePrice` if the price is too old: [4](#0-3) 

And Pyth's Aave integration (`PythPriceOracleGetter`) correctly uses `getPriceNoOlderThan` with a configurable `validTimePeriodSeconds`: [5](#0-4) 

`PythAggregatorV3` provides no equivalent protection.

---

### Impact Explanation

Protocols that integrate `PythAggregatorV3` as a drop-in Chainlink oracle replacement — for collateral pricing, liquidation thresholds, fee conversion, or any other price-sensitive operation — will silently receive stale prices whenever the Pyth feed has not been recently updated. This can lead to:

- **Incorrect liquidations** (under- or over-liquidating positions based on an outdated price)
- **Mispriced collateral** (borrowing more than allowed against stale collateral values)
- **Fee/conversion errors** (analogous to the original report's `convertFeeToEth()` using a stale ETH/USD rate)

The severity matches the original report (Medium) because the impact is financial loss to protocol users, not just observability noise.

---

### Likelihood Explanation

Pyth is a pull oracle — prices are only updated when someone calls `updateFeeds()`. If no keeper or user submits a fresh update, the on-chain price can become arbitrarily stale. This is a realistic condition during:

- Network congestion
- Low-activity periods
- Deliberate griefing (attacker withholds updates to exploit a stale price)

Any unprivileged user can trigger the vulnerable path simply by calling a function on a protocol that uses `PythAggregatorV3.latestRoundData()` without first calling `updateFeeds()`.

---

### Recommendation

Replace `getPriceUnsafe` in `latestRoundData()` and `latestAnswer()` with a staleness-checked variant. The adapter should either:

1. Accept a `maxAge` parameter in the constructor and call `pyth.getPriceNoOlderThan(priceId, maxAge)`, reverting with `StalePrice` if the price is too old, or
2. At minimum, add an explicit `require(block.timestamp - price.publishTime <= maxAge, "Stale price")` guard after the `getPriceUnsafe` call.

The `validTimePeriodSeconds` pattern already used in `PythPriceOracleGetter` and `PythAssetRegistry` is the correct model to follow. [6](#0-5) 

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to a live Pyth contract and a price feed ID.
2. Do **not** call `updateFeeds()` — allow the on-chain price to age beyond any reasonable heartbeat (e.g., 1 hour).
3. Call `latestRoundData()`. Observe that it returns the stale price without reverting.
4. Compare `updatedAt` (= `price.publishTime`) against `block.timestamp` — the difference will exceed any safe threshold, yet the call succeeds and returns the old price as if it were current.
5. A protocol using this return value for collateral pricing or fee conversion will compute incorrect amounts, enabling profitable exploitation. [1](#0-0)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L40-60)
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

**File:** target_chains/ethereum/contracts/contracts/aave/PythPriceOracleGetter.sol (L63-66)
```text
        PythStructs.Price memory price = pyth().getPriceNoOlderThan(
            priceId,
            PythAssetRegistry.validTimePeriodSeconds()
        );
```

**File:** target_chains/ethereum/contracts/contracts/aave/PythAssetRegistry.sol (L15-19)
```text
        /// Maximum acceptable time period before price is considered to be stale.
        /// This includes attestation delay, block time, and potential clock drift
        /// between the source/target chains.
        uint validTimePeriodSeconds;
    }
```
