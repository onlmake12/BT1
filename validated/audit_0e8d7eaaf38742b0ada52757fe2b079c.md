### Title
`Scheduler::getMinimumBalance` Does Not Account for Pyth Fee and Gas Cost, Causing `updatePriceFeeds` to Revert for Subscriptions Funded at the Advertised Minimum - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getMinimumBalance` is the public view function integrators use to determine how much ETH to deposit when creating or maintaining a subscription. However, `updatePriceFeeds` enforces two additional balance checks — one for the dynamic Pyth oracle fee and one for the gas-cost-based keeper fee — that are not reflected in `getMinimumBalance`. A subscription funded at exactly the value returned by `getMinimumBalance` will revert every time a keeper attempts to call `updatePriceFeeds`, causing price feeds to go stale.

---

### Finding Description

`getMinimumBalance` is defined as:

```solidity
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256 minimumBalanceInWei)
{
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
}
```

It returns a static product of `numPriceFeeds × minimumBalancePerFeed`, where `minimumBalancePerFeed` is a governance-set constant.

`updatePriceFeeds` enforces two separate balance checks that are **not** captured by this formula:

**Check 1 — Pyth oracle fee (dynamic, data-dependent):**
```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
status.balanceInWei -= pythFee;
```

**Check 2 — Keeper fee (dynamic, gas-price-dependent):**
```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

The actual minimum balance required for a single successful update is:

```
pythFee(updateData) + (GAS_OVERHEAD + actualGasUsed) × tx.gasprice + singleUpdateKeeperFeeInWei × N
```

Both `pythFee` and `gasCost` are dynamic at execution time. `getMinimumBalance` returns only the static `N × minimumBalancePerFeed` component, which cannot capture these dynamic costs regardless of how the admin sets `minimumBalancePerFeed`.

The IScheduler NatSpec explicitly advertises this function as the authoritative minimum:

```
/// @notice Returns the minimum balance an active subscription of a given size needs to hold.
```

Integrators who fund their subscription to exactly `getMinimumBalance()` will have a subscription that passes all deposit/withdrawal checks but whose `updatePriceFeeds` calls revert every time.

---

### Impact Explanation

- Subscriptions funded at the advertised minimum balance will have every `updatePriceFeeds` call revert with `InsufficientBalance`, causing price feeds to go permanently stale.
- Keepers waste gas on failed transactions and may stop servicing the subscription.
- Downstream protocols consuming prices from a stale Scheduler subscription may act on incorrect data.
- Integrators are misled by the public interface into believing their subscription is adequately funded when it is not.

---

### Likelihood Explanation

High. `getMinimumBalance` is the only public view function for determining required funding. Every integrator building on top of Scheduler will call it. The Pyth fee is non-zero on all production deployments, and gas prices are always positive, so the gap between `getMinimumBalance` and the actual required balance is always present. The existing test `testUpdatePriceFeedsRevertsInsufficientBalanceForKeeperFee` already demonstrates that a subscription funded with `mockPythFee + minKeeperFee` still reverts because the actual gas cost of `updatePriceFeeds` exceeds the static estimate.

---

### Recommendation

`getMinimumBalance` should incorporate the Pyth fee and a conservative gas cost estimate so that a subscription funded at the returned value is guaranteed to support at least one update without reverting. One approach:

```solidity
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256 minimumBalanceInWei)
{
    uint256 keeperFee = uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    // Add a conservative estimate of the Pyth fee (e.g., singleUpdateFeeInWei × numPriceFeeds)
    uint256 estimatedPythFee = IPyth(_state.pyth).singleUpdateFeeInWei() * numPriceFeeds;
    // Add a conservative gas cost estimate at a reference gas price
    uint256 estimatedGasCost = GAS_OVERHEAD * tx.gasprice; // or a stored reference price
    return keeperFee + estimatedPythFee + estimatedGasCost;
}
```

Alternatively, document clearly that `getMinimumBalance` is a floor for subscription activation only, and provide a separate `getEstimatedUpdateCost(numPriceFeeds, updateData)` view function that returns the true cost of a single `updatePriceFeeds` call.

---

### Proof of Concept

```solidity
function testPOC_MinimumBalanceDoesNotCoverUpdateCost() public {
    // Create subscription funded at exactly getMinimumBalance
    SchedulerStructs.SubscriptionParams memory params =
        createDefaultSubscriptionParams(2, address(reader));
    uint256 minimumBalance = scheduler.getMinimumBalance(
        uint8(params.priceIds.length)
    );
    uint256 subscriptionId = scheduler.createSubscription{value: minimumBalance}(params);

    // Prepare a valid price update
    uint64 publishTime = SafeCast.toUint64(block.timestamp);
    (PythStructs.PriceFeed[] memory priceFeeds, uint64[] memory slots) =
        createMockPriceFeedsWithSlots(publishTime, params.priceIds.length);
    mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
    bytes[] memory updateData = createMockUpdateData(priceFeeds);

    // updatePriceFeeds reverts even though balanceInWei == getMinimumBalance
    // because pythFee + gasCost exceeds the minimum balance
    vm.prank(pusher);
    vm.expectRevert(SchedulerErrors.InsufficientBalance.selector);
    scheduler.updatePriceFeeds(subscriptionId, updateData);
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L290-297)
```text
        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L844-854)
```text
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L118-122)
```text
    /// @notice Returns the minimum balance an active subscription of a given size needs to hold.
    /// @param numPriceFeeds The number of price feeds in the subscription.
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view returns (uint256 minimumBalanceInWei);
```
