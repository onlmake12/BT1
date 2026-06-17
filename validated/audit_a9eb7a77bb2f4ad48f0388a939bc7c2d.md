### Title
Missing Price Staleness Validation in `PythAggregatorV3.latestAnswer()` / `latestRoundData()` — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3` is the official Pyth Solidity SDK adapter that implements Chainlink's `AggregatorV3Interface`, recommended for all Chainlink-to-Pyth migrations. Every price-returning function in the contract calls `pyth.getPriceUnsafe()` with no staleness check. Any downstream protocol that calls `latestAnswer()`, `latestRoundData()`, or `getRoundData()` will silently receive a price that may be arbitrarily old, with no revert or signal of staleness.

---

### Finding Description

`PythAggregatorV3` is shipped as part of `@pythnetwork/pyth-sdk-solidity` and is the canonical adapter Pyth recommends for protocols migrating from Chainlink. The Pyth documentation explicitly instructs integrators to deploy this contract and point their existing Chainlink-compatible apps at it.

Every price-reading function in the contract uses `getPriceUnsafe()`:

```solidity
// latestAnswer — no staleness check
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

// latestRoundData — no staleness check
function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}
```

`getPriceUnsafe()` is explicitly documented to return a price from **arbitrarily far in the past** with no recency guarantee. The Pyth SDK provides `getPriceNoOlderThan(id, age)` precisely to enforce freshness, but `PythAggregatorV3` never calls it.

The contrast with the safe pattern is clear: `AbstractPyth.getPriceNoOlderThan()` enforces `diff(block.timestamp, price.publishTime) > age → revert StalePrice()`, but `PythAggregatorV3` bypasses this entirely.

---

### Impact Explanation

Protocols that integrate `PythAggregatorV3` as a drop-in Chainlink replacement (lending protocols, derivative protocols, AMMs) will call `latestRoundData()` or `latestAnswer()` expecting a fresh price. If the on-chain price feed has not been updated recently (e.g., during a network outage, market closure, or keeper failure), the adapter silently returns the last cached price with no revert. This leads to:

- Incorrect collateral valuation in lending protocols → bad debt or unjust liquidations
- Incorrect settlement prices in derivative protocols → exploitable by adversarial selection
- Incorrect swap rates in AMMs → arbitrage at the expense of LPs

The `updatedAt` field returned by `latestRoundData()` is set to `price.publishTime`, so a careful integrator *could* check it — but the adapter itself provides no enforcement, and the Chainlink migration guide does not warn integrators to add their own staleness check.

**Impact: Medium** — financial loss to users of protocols built on this adapter.

---

### Likelihood Explanation

`PythAggregatorV3` is the official, actively-promoted Chainlink migration path. The Pyth documentation at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx` instructs integrators to deploy it without mentioning the need for a downstream staleness check. Price feeds can go stale during network outages, market closures (e.g., equities outside trading hours), or keeper failures. An attacker can wait for staleness and then interact with any protocol using this adapter.

**Likelihood: Medium** — staleness events are realistic and the adapter is widely recommended.

---

### Recommendation

Replace `getPriceUnsafe()` calls in `latestAnswer()`, `latestRoundData()`, and `getRoundData()` with `getPriceNoOlderThan(priceId, maxAge)`, where `maxAge` is a configurable constructor parameter (e.g., defaulting to `getValidTimePeriod()`):

```solidity
uint256 public maxPriceAge; // set in constructor, e.g. 60 seconds

function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxPriceAge);
    return int256(price.price);
}

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxPriceAge);
    ...
}
```

This mirrors the pattern already used in `AbstractPyth.getPriceNoOlderThan()` and aligns with Pyth's own best-practices documentation.

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing at the Pyth contract for ETH/USD.
2. Allow the on-chain ETH/USD price feed to go stale (e.e., no keeper update for >60 seconds, or simulate with a fork where `block.timestamp` is advanced).
3. Call `latestRoundData()` on the adapter.
4. Observe: the call succeeds and returns the old price with no revert. The `updatedAt` field reflects the stale `publishTime`.
5. A lending protocol using this adapter would accept the stale price for collateral valuation, enabling an attacker to borrow against over-valued or under-valued collateral.

**Root cause in code:** [1](#0-0) [2](#0-1) 

**Safe pattern that is absent:** [3](#0-2) 

**Official migration guide that recommends this adapter without staleness warning:** [4](#0-3)

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
