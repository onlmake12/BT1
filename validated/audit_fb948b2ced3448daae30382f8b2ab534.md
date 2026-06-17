### Title
`MockPyth.parsePriceFeedUpdatesUnique` Silently Drops Uniqueness Guarantee Due to Wrong Boolean Flag — (File: `target_chains/ethereum/sdk/solidity/MockPyth.sol`)

### Summary

`MockPyth.parsePriceFeedUpdatesUnique` is a wrapper that is supposed to enforce the uniqueness constraint (`checkUniqueness = true`) when delegating to `parsePriceFeedUpdatesWithConfig`. Instead, it passes `false` for `checkUniqueness`, making it functionally identical to `parsePriceFeedUpdates`. Any developer using `MockPyth` to test contracts that depend on the uniqueness guarantee will receive false-passing tests, and their production contracts may be vulnerable to scenarios the uniqueness check was designed to prevent.

### Finding Description

`parsePriceFeedUpdatesWithConfig` accepts three boolean flags in this order:

```
parsePriceFeedUpdatesWithConfig(
    updateData,
    priceIds,
    minAllowedPublishTime,
    maxAllowedPublishTime,
    bool checkUniqueness,          // 5th param
    bool checkUpdateDataIsMinimal, // 6th param
    bool storeUpdatesIfFresh       // 7th param
)
```

In the production contract `Pyth.sol`, `parsePriceFeedUpdatesUnique` correctly passes `true` for `checkUniqueness`:

```solidity
// Pyth.sol L623-631 — CORRECT
(priceFeeds, ) = parsePriceFeedUpdatesWithConfig(
    updateData, priceIds, minPublishTime, maxPublishTime,
    true,   // checkUniqueness ✓
    false,
    false
);
```

In `MockPyth.sol`, `parsePriceFeedUpdatesUnique` passes `false` for `checkUniqueness`, making it identical to `parsePriceFeedUpdates`:

```solidity
// MockPyth.sol L170-178 — BUG
(feeds, ) = parsePriceFeedUpdatesWithConfig(
    updateData, priceIds, minPublishTime, maxPublishTime,
    false,  // checkUniqueness ← should be true
    true,
    false
);
``` [1](#0-0) [2](#0-1) 

The `checkUniqueness` flag, when `true`, enforces the condition `prevPublishTime < minAllowedPublishTime`, ensuring the returned update is the **first** update published after `minPublishTime`. With `false`, this condition is skipped entirely. [3](#0-2) 

### Impact Explanation

`MockPyth` is distributed as part of the published `@pythnetwork/pyth-sdk-solidity` SDK for developers to use in their own contract test suites. A developer whose contract relies on `parsePriceFeedUpdatesUnique` to guarantee that only the **first** price update in a time window is accepted (e.g., to prevent replay of a later update within the same window) will write tests against `MockPyth` that pass even when the uniqueness condition is violated. Their production deployment against the real `Pyth.sol` will behave differently from their tests, potentially allowing non-unique price updates to be accepted in scenarios the developer believed were protected.

### Likelihood Explanation

Any developer who:
1. Uses `MockPyth` for unit testing (the intended use case of the SDK mock), and
2. Calls `parsePriceFeedUpdatesUnique` with update data containing multiple updates for the same price ID within the time window

will silently receive wrong behavior. The divergence between `MockPyth` and `Pyth.sol` is not documented, making this a realistic trap.

### Recommendation

Change `checkUniqueness` from `false` to `true` in `MockPyth.parsePriceFeedUpdatesUnique`:

```solidity
function parsePriceFeedUpdatesUnique(...) external payable override returns (...) {
    (feeds, ) = parsePriceFeedUpdatesWithConfig(
        updateData,
        priceIds,
        minPublishTime,
        maxPublishTime,
        true,   // checkUniqueness — was incorrectly false
        true,
        false
    );
}
``` [1](#0-0) 

Add a test that submits two updates for the same price ID within the time window and asserts that `parsePriceFeedUpdatesUnique` returns only the first one (and reverts if the first is not present), matching the behavior of the production `Pyth.sol`.

### Proof of Concept

1. Deploy `MockPyth`.
2. Create two update data entries for the same `priceId`, with `prevPublishTime = 100`, `publishTime = 200` (first update) and `prevPublishTime = 200`, `publishTime = 300` (second update).
3. Call `parsePriceFeedUpdatesUnique(updateData, priceIds, 150, 400)`.
4. **Expected (production `Pyth.sol`)**: Only the update with `publishTime = 200` is returned, because `prevPublishTime (100) < minPublishTime (150)`. The update with `publishTime = 300` is rejected because `prevPublishTime (200) >= minPublishTime (150)`.
5. **Actual (`MockPyth`)**: Both updates satisfy the range check; the uniqueness condition is never evaluated (`checkUniqueness = false`). The mock returns whichever update it encounters first, with no uniqueness enforcement — silently diverging from production behavior. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/sdk/solidity/MockPyth.sol (L89-100)
```text
    function parsePriceFeedUpdatesWithConfig(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minAllowedPublishTime,
        uint64 maxAllowedPublishTime,
        bool checkUniqueness,
        bool checkUpdateDataIsMinimal,
        bool storeUpdatesIfFresh
    )
        public
        payable
        returns (PythStructs.PriceFeed[] memory feeds, uint64[] memory slots)
```

**File:** target_chains/ethereum/sdk/solidity/MockPyth.sol (L128-139)
```text
                if (feeds[i].id == priceIds[i]) {
                    if (
                        minAllowedPublishTime <= publishTime &&
                        publishTime <= maxAllowedPublishTime &&
                        (!checkUniqueness ||
                            prevPublishTime < minAllowedPublishTime)
                    ) {
                        break;
                    } else {
                        feeds[i].id = 0;
                    }
                }
```

**File:** target_chains/ethereum/sdk/solidity/MockPyth.sol (L164-179)
```text
    function parsePriceFeedUpdatesUnique(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minPublishTime,
        uint64 maxPublishTime
    ) external payable override returns (PythStructs.PriceFeed[] memory feeds) {
        (feeds, ) = parsePriceFeedUpdatesWithConfig(
            updateData,
            priceIds,
            minPublishTime,
            maxPublishTime,
            false,
            true,
            false
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L612-632)
```text
    function parsePriceFeedUpdatesUnique(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minPublishTime,
        uint64 maxPublishTime
    )
        external
        payable
        override
        returns (PythStructs.PriceFeed[] memory priceFeeds)
    {
        (priceFeeds, ) = parsePriceFeedUpdatesWithConfig(
            updateData,
            priceIds,
            minPublishTime,
            maxPublishTime,
            true,
            false,
            false
        );
    }
```

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L149-163)
```text
    function parsePriceFeedUpdatesWithConfig(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minAllowedPublishTime,
        uint64 maxAllowedPublishTime,
        bool checkUniqueness,
        bool checkUpdateDataIsMinimal,
        bool storeUpdatesIfFresh
    )
        external
        payable
        returns (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        );
```
