### Title
Inconsistent Balance Check in `updatePriceFeeds` Omits Keeper Fee, Causing Preventable Transaction Failures — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.updatePriceFeeds` performs two separate balance checks: one upfront for the Pyth fee only, and a second one inside `_processFeesAndPayKeeper` for the keeper fee. When a subscription's balance falls in the range `[pythFee, pythFee + totalKeeperFee)`, the first check passes, the Pyth fee is deducted and forwarded, but the transaction then reverts at the keeper fee check — wasting the keeper's gas and preventing the price update from landing.

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds` executes the following sequence:

**First check (line 295) — only guards against the Pyth fee:**
```solidity
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [1](#0-0) 

**Pyth fee is then deducted and ETH is forwarded (lines 305–319):**
```solidity
status.balanceInWei -= pythFee;
status.totalSpent += pythFee;
...
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...);
``` [2](#0-1) 

**Second check (line 852, inside `_processFeesAndPayKeeper`) — guards against the keeper fee on the already-reduced balance:**
```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

The keeper fee is computed as:
```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [4](#0-3) 

The upfront check should be `status.balanceInWei < pythFee + minimumKeeperFee`, but it is only `status.balanceInWei < pythFee`. The `getMinimumBalance` function also does not include the Pyth fee in its calculation:
```solidity
return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
``` [5](#0-4) 

### Impact Explanation

When `pythFee ≤ status.balanceInWei < pythFee + totalKeeperFee`:

1. The first check passes.
2. The Pyth fee is deducted from `status.balanceInWei` and ETH is forwarded to the Pyth contract.
3. The keeper fee check in `_processFeesAndPayKeeper` fails and the entire transaction reverts.
4. All state changes are rolled back, but the **keeper has already consumed gas** for the full execution path (including the Pyth call).
5. The price update does not land, breaking the subscription's update guarantee.

Subscriptions that have been drained close to the minimum balance (which itself does not account for the Pyth fee) are permanently stuck in this failure zone: every keeper attempt reverts, no price update occurs, and keepers are economically disincentivized from retrying.

### Likelihood Explanation

This is reachable by any unprivileged keeper calling `updatePriceFeeds`. The condition is naturally reached as subscriptions spend down their balance over time. The Pyth fee is dynamic (`pyth.getUpdateFee(updateData)`), so even a subscription funded above the static `getMinimumBalance` threshold can enter the failure zone if the Pyth fee rises or if gas prices are high. No privileged access is required.

### Recommendation

Replace the upfront single-fee check with a combined check that includes a lower-bound estimate of the keeper fee:

```solidity
uint256 minKeeperFee = (GAS_OVERHEAD * tx.gasprice)
    + (uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length);

if (status.balanceInWei < pythFee + minKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

Additionally, update `getMinimumBalance` to incorporate the Pyth fee so that the minimum balance enforced at subscription creation and top-up is sufficient to cover a full update cycle.

### Proof of Concept

```
Scenario:
  status.balanceInWei  = 100 wei
  pythFee              = 80 wei
  totalKeeperFee       = 30 wei
  Total needed         = 110 wei

Step 1 — first check:
  100 < 80  →  false  →  passes (incorrectly)

Step 2 — Pyth fee deducted:
  status.balanceInWei = 100 - 80 = 20 wei

Step 3 — keeper fee check:
  20 < 30  →  true  →  revert InsufficientBalance

Result: transaction reverts, keeper loses gas, price update fails.
With the correct upfront check:
  100 < 80 + 30  →  100 < 110  →  true  →  revert early, before wasting gas on the Pyth call.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L294-297)
```text
        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-319)
```text
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L734-739)
```text
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view override returns (uint256 minimumBalanceInWei) {
        // TODO: Consider adding a base minimum balance independent of feed count
        return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-849)
```text
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L851-854)
```text
        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```
