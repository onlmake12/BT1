### Title
`PythAggregatorV3.latestRoundData()` Returns Stale Price Without Freshness Check — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter contract. Every price-returning function in it — `latestAnswer()`, `latestTimestamp()`, `latestRound()`, `getAnswer()`, `getTimestamp()`, `getRoundData()`, and `latestRoundData()` — calls `pyth.getPriceUnsafe()` with no staleness check. Any protocol that deploys this adapter and calls `latestRoundData()` (the standard Chainlink integration pattern) will silently receive a price that may be arbitrarily old.

### Finding Description

`PythAggregatorV3.sol` is the official Pyth SDK contract for protocols that want to consume Pyth prices through the standard Chainlink `AggregatorV3Interface`. Every price-returning function delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
// latestAnswer — no staleness check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — no staleness check
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
``` [1](#0-0) [2](#0-1) 

The `IPyth` interface explicitly documents that `getPriceUnsafe` "returns the most recent price update in this contract **without any recency checks**" and that "this function is unsafe as the returned price update may be **arbitrarily far in the past**": [3](#0-2) 

Pyth provides `getPriceNoOlderThan(id, age)` precisely to enforce freshness, but `PythAggregatorV3` never uses it. [4](#0-3) 

The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, which is the Pyth publish timestamp — it can be hours or days old with no on-chain revert. [5](#0-4) 

### Impact Explanation

Protocols that integrate `PythAggregatorV3` as a drop-in Chainlink replacement (lending markets, perpetuals, collateral managers) will call `latestRoundData()` and receive a stale price with no revert. This is the exact pattern the referenced Sentiment report describes:

- Collateral can be overvalued during a flash crash, allowing under-collateralized borrowing or preventing necessary liquidations.
- Loan-to-value calculations become inaccurate, enabling bad debt accumulation.
- Any protocol that checks `updatedAt` from the returned tuple will see `price.publishTime`, which is the Pyth publish time — not the block timestamp — and may still be stale relative to the current block.

### Likelihood Explanation

`PythAggregatorV3` is the official Pyth-provided Chainlink compatibility shim, distributed as part of the `@pythnetwork/pyth-sdk-solidity` package. Protocols integrating Pyth via the Chainlink interface will deploy this contract directly. The Pyth pull model means prices are only updated when someone submits an update; during periods of low activity or network congestion, the cached price can become significantly stale. An attacker can monitor the on-chain `publishTime` and exploit the stale price window without any privileged access — only a standard transaction is required.

### Recommendation

Add a configurable `maxAge` parameter to the contract and replace all `getPriceUnsafe` calls with `getPriceNoOlderThan`:

```solidity
uint256 public maxAge; // set in constructor, e.g. 3600 seconds

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxAge);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

This mirrors the fix applied in the Sentiment report and aligns with Pyth's own best-practices documentation, which states integrators should use `getPriceNoOlderThan()` to guard against stale prices. [6](#0-5) 

### Proof of Concept

1. A protocol deploys `PythAggregatorV3` pointing to the Pyth contract and a price feed (e.g., ETH/USD).
2. The Pyth on-chain price is last updated at `T=0` with `publishTime = T`.
3. At `T + 2h`, the market price of ETH drops 30% but no one has submitted a Pyth update.
4. An attacker calls `latestRoundData()` on the adapter. It calls `getPriceUnsafe`, which returns the 2-hour-old price with no revert.
5. The attacker uses the inflated stale price to borrow against overvalued collateral or avoid liquidation, causing bad debt to the protocol.
6. The `updatedAt` field in the returned tuple equals `price.publishTime = T`, which is 2 hours in the past — but `PythAggregatorV3` never checks `block.timestamp - updatedAt` against any threshold. [7](#0-6)

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

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L23-31)
```text
    /// @notice Returns the price that is no older than `age` seconds of the current time.
    /// @dev This function is a sanity-checked version of `getPriceUnsafe` which is useful in
    /// applications that require a sufficiently-recent price. Reverts if the price wasn't updated sufficiently
    /// recently.
    /// @return price - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPriceNoOlderThan(
        bytes32 id,
        uint age
    ) external view returns (PythStructs.Price memory price);
```
