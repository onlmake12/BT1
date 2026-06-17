### Title
Stale Price Returned Without Revert in All `PythAggregatorV3` Price Functions — (`File: target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3`, Pyth's official Chainlink-compatible adapter contract published in `@pythnetwork/pyth-sdk-solidity` and actively promoted in the Chainlink migration guide, uses `getPriceUnsafe()` in every single price-reading function — including `latestAnswer()`, `latestRoundData()`, `getRoundData()`, and `decimals()` — with no staleness check whatsoever. Any downstream protocol that integrates this adapter will silently receive arbitrarily stale prices without any revert, directly mirroring the vulnerability class of the reported issue.

---

### Finding Description

`PythAggregatorV3` implements the Chainlink `AggregatorV3Interface` as a drop-in replacement for Chainlink price feeds. Every price-reading function in the contract delegates to `pyth.getPriceUnsafe(priceId)`:

```solidity
function latestAnswer() public view virtual returns (int256) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return int256(price.price);
}

function latestRoundData() external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    roundId = uint80(price.publishTime);
    return (roundId, int256(price.price), price.publishTime, price.publishTime, roundId);
}

function getRoundData(uint80 _roundId) external view returns (...) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    ...
}

function decimals() public view virtual returns (uint8) {
    PythStructs.Price memory price = pyth.getPriceUnsafe(priceId);
    return uint8(-1 * int8(price.expo));
}
```

`getPriceUnsafe()` is explicitly documented in `IPyth.sol` as returning "the most recent price update in this contract without any recency checks" and that "the returned price update may be arbitrarily far in the past." No staleness check is applied at any layer within `PythAggregatorV3`.

The `latestRoundData()` function is particularly dangerous: it is the Chainlink-recommended replacement for the deprecated `latestAnswer()`, and downstream protocols that call it expect the returned `updatedAt` field to reflect a recent, valid price. Instead, `updatedAt` is set to `price.publishTime`, which can be hours or days old, and the function never reverts. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

Any protocol that deploys `PythAggregatorV3` as its Chainlink-compatible price oracle — which is the explicit purpose of this contract — will silently consume stale prices during:

- **Market closure periods** (e.g., equities, commodities): Pyth price feeds for these assets are not updated outside trading hours. The on-chain cached price can be days old.
- **Network outages or push-schedule lapses**: If the keeper responsible for calling `updateFeeds()` stops, the cached price freezes indefinitely.
- **Adversarial selection**: An attacker can deliberately avoid calling `updateFeeds()` before interacting with a protocol, ensuring the protocol reads a stale, favorable price.

Downstream protocols using this adapter for collateral valuation (lending), liquidation triggers, or derivative settlement will operate on incorrect prices. This can lead to:
- Undercollateralized loans being opened against stale high collateral prices
- Liquidations being blocked or triggered incorrectly
- Derivative settlement at wrong prices [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

Likelihood is **high** because:

1. `PythAggregatorV3` is the official, Pyth-published Chainlink migration adapter, actively promoted in the developer documentation at `apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx`. Protocols following the migration guide will deploy it as-is.
2. Protocols migrating from Chainlink typically call `latestRoundData()` and check `updatedAt` against a threshold — but the `updatedAt` value returned is `price.publishTime`, which is the Pyth publish time, not the time the on-chain cache was last updated. If the protocol's staleness threshold is loose (e.g., 24 hours), stale prices pass silently.
3. The `updateFeeds()` function is a separate, optional call. Nothing in the adapter forces a price update before reading. Any caller can read the price without updating it.
4. Pyth's own documentation and skill notes explicitly warn: "never `getPriceUnsafe` in production DeFi" — yet the SDK adapter uses it exclusively. [6](#0-5) [7](#0-6) 

---

### Recommendation

Replace all `getPriceUnsafe()` calls in `PythAggregatorV3` with `getPriceNoOlderThan()` using a configurable staleness threshold. The threshold should be set at construction time and be adjustable by the contract owner:

```solidity
contract PythAggregatorV3 {
    bytes32 public priceId;
    IPyth public pyth;
    uint public maxStaleness; // e.g., 3600 seconds

    constructor(address _pyth, bytes32 _priceId, uint _maxStaleness) {
        priceId = _priceId;
        pyth = IPyth(_pyth);
        maxStaleness = _maxStaleness;
    }

    function latestAnswer() public view virtual returns (int256) {
        PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxStaleness);
        return int256(price.price);
    }

    function latestRoundData() external view returns (...) {
        PythStructs.Price memory price = pyth.getPriceNoOlderThan(priceId, maxStaleness);
        ...
    }
}
```

This ensures that all price-reading functions revert with `StalePrice` if the cached price is too old, matching the behavior downstream protocols expect from a Chainlink-compatible feed. [8](#0-7) 

---

### Proof of Concept

1. Deploy `PythAggregatorV3` pointing to the Pyth contract and a price feed ID (e.g., ETH/USD).
2. Call `updateFeeds()` once to populate the on-chain cache.
3. Wait 24+ hours without calling `updateFeeds()` again (simulating a push-schedule lapse or market closure).
4. Call `latestRoundData()` — it returns the 24-hour-old price with `updatedAt` set to the old `publishTime`, without reverting.
5. A lending protocol using this adapter will value collateral at the stale price, allowing an attacker to borrow against an asset whose true current price has dropped significantly.

```solidity
// Attacker PoC
PythAggregatorV3 adapter = PythAggregatorV3(deployedAdapterAddress);

// After 24h without updateFeeds():
(, int256 answer, , uint256 updatedAt, ) = adapter.latestRoundData();
// answer = stale price from 24h ago, no revert
// updatedAt = old publishTime, protocol may accept it if threshold is loose
``` [2](#0-1) [9](#0-8)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L26-38)
```text
    function updateFeeds(bytes[] calldata priceUpdateData) public payable {
        // Update the prices to the latest available values and pay the required fee for it. The `priceUpdateData` data
        // should be retrieved from our off-chain Price Service API using the `hermes-client` package.
        // See section "How Pyth Works on EVM Chains" below for more information.
        uint fee = pyth.getUpdateFee(priceUpdateData);
        pyth.updatePriceFeeds{value: fee}(priceUpdateData);

        // refund remaining eth
        // solhint-disable-next-line no-unused-vars
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
    }
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

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L88-97)
```text
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

**File:** apps/developer-hub/content/docs/price-feeds/core/migrate-an-app-to-pyth/chainlink.mdx (L11-11)
```text
1. Deploy the [`PythAggregatorV3`](https://github.com/pyth-network/pyth-crosschain/blob/main/target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol) contract to provide a Chainlink-compatible feed interface.
```

**File:** apps/developer-hub/src/app/SKILL.md/route.ts (L65-65)
```typescript
- **Staleness threshold**: Use \`getPriceNoOlderThan(id, maxAge)\`, never \`getPriceUnsafe\` in production
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
