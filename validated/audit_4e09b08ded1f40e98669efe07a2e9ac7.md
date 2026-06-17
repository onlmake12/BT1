### Title
`PythAggregatorV3.latestRoundData()` and `latestAnswer()` Return Unvalidated Stale Prices via `getPriceUnsafe()` — (`target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is Pyth's official Chainlink-compatible adapter contract, designed as a drop-in replacement for `AggregatorV3Interface`. Every price-returning function in the contract — `latestAnswer()`, `latestRoundData()`, `getRoundData()`, `latestTimestamp()`, and `decimals()` — calls `pyth.getPriceUnsafe(priceId)`, which explicitly performs **no staleness validation** and can return a price from arbitrarily far in the past. Any protocol that integrates `PythAggregatorV3` as a Chainlink feed replacement will silently consume stale prices in financial calculations, enabling an attacker to exploit the price discrepancy.

---

### Finding Description

`PythAggregatorV3.sol` is the official Pyth SDK contract for migrating Chainlink-dependent protocols to Pyth price feeds. Its entire public API delegates to `pyth.getPriceUnsafe(priceId)`:

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
``` [1](#0-0) [2](#0-1) 

`getPriceUnsafe()` is documented in `IPyth.sol` as returning "the most recent price update in this contract **without any recency checks**" and that "the returned price update may be arbitrarily far in the past": [3](#0-2) 

The safe alternative, `getPriceNoOlderThan(id, age)`, enforces a staleness bound and reverts if the price is too old: [4](#0-3) 

`PythAggregatorV3` never calls `getPriceNoOlderThan`. There is no `maxAge` parameter, no staleness revert, and no on-chain warning to callers. The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, which a careful caller *could* check manually — but the contract itself enforces nothing, mirroring exactly the `ethPerCvx(false)` pattern from H-03. [5](#0-4) 

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as a Chainlink-compatible price source and calls `latestRoundData()` or `latestAnswer()` will receive a price that may be arbitrarily stale. Concrete financial consequences include:

- **Token over-minting**: If the real price has risen since the stale snapshot, the protocol's TVL is understated, and a depositor is minted more shares than they are entitled to (identical to the H-03 scenario).
- **Incorrect collateral valuation**: Lending protocols using `PythAggregatorV3` for collateral pricing may allow under-collateralized borrows or block valid liquidations.
- **Liquidation manipulation**: A stale low price can trigger illegitimate liquidations; a stale high price can prevent legitimate ones.

The severity is amplified because `PythAggregatorV3` is the **official Pyth migration path** for Chainlink users, so the affected surface is every protocol that followed Pyth's own migration guide. [6](#0-5) 

---

### Likelihood Explanation

- `PythAggregatorV3` is shipped in the official `@pythnetwork/pyth-sdk-solidity` package and is the recommended migration path from Chainlink.
- Pyth feeds can become stale during network congestion, guardian set transitions, or brief outages — the best-practices documentation explicitly acknowledges this risk.
- An attacker needs only to observe that the on-chain Pyth price is stale (publicly visible) and submit a transaction to a protocol using `PythAggregatorV3` before the price is refreshed. No privileged access is required. [7](#0-6) 

---

### Recommendation

Replace every `getPriceUnsafe` call in `PythAggregatorV3` with `getPriceNoOlderThan`, gated by a configurable `maxAge` set at construction time:

```diff
+   uint public maxAge;

-   constructor(address _pyth, bytes32 _priceId) {
+   constructor(address _pyth, bytes32 _priceId, uint _maxAge) {
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

This mirrors the fix applied in H-03 (changing `ethPerCvx(false)` → `ethPerCvx(true)`) and ensures that callers of the Chainlink-compatible interface receive only validated, recent prices.

---

### Proof of Concept

1. Protocol `P` deploys `PythAggregatorV3` for the ETH/USD feed and uses `latestRoundData()` to price collateral in a lending market.
2. The Pyth ETH/USD feed on-chain is stale by 2 hours (e.g., due to a brief network outage). The cached price is $2,000; the real price is $2,400.
3. Attacker calls `deposit()` on protocol `P`. `P` calls `PythAggregatorV3.latestRoundData()`, which calls `getPriceUnsafe()` and returns $2,000 with no revert.
4. Protocol `P` calculates collateral value using the stale $2,000 price, allowing the attacker to borrow 20% more than they should be entitled to.
5. The attacker withdraws the excess borrow, leaving the protocol under-collateralized.

The root cause — `getPriceUnsafe` with no staleness guard — is entirely within `PythAggregatorV3.sol`, a production Pyth contract. [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L7-15)
```text
// This interface is forked from the Zerolend Adapter found here:
// https://github.com/zerolend/pyth-oracles/blob/master/contracts/PythAggregatorV3.sol
// Original license found under licenses/zerolend-pyth-oracles.md

/**
 * @title A port of the ChainlinkAggregatorV3 interface that supports Pyth price feeds
 * @notice This does not store any roundId information on-chain. Please review the code before using this implementation.
 * Users should deploy an instance of this contract to wrap every price feed id that they need to use.
 */
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

**File:** apps/developer-hub/content/docs/price-feeds/core/best-practices.mdx (L31-39)
```text

Alternatively, a network outage (at the internet level, blockchain level, or at multiple data providers) may prevent the protocol from producing new price updates.
(Such outages are unlikely, but integrators should still be prepared for the possibility.)
In such cases, Pyth may return a stale price for the product.

Integrators should be careful to avoid accidentally using a stale price.
The Pyth SDKs guard against this failure mode by incorporating a staleness check by default.
Querying the current price will fail if too much time has elapsed since the last update.
The SDKs expose this failure condition in an idiomatic way: for example, the Rust SDK may return `None`, and the Solidity SDK may revert the transaction.
```
