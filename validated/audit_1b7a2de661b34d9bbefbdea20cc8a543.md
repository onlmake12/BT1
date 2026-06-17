### Title
`getMinimumBalance` Excludes Pyth Fee, Making Minimum-Funded Subscriptions Non-Functional — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.getMinimumBalance()` computes the minimum required subscription balance as `numPriceFeeds × minimumBalancePerFeed`, but omits the dynamic Pyth protocol fee (`pythFee`). When `updatePriceFeeds` executes, the Pyth fee is deducted from the subscription balance *before* the keeper fee check. A subscription funded at exactly `getMinimumBalance()` will have its balance reduced below what is needed to pay the keeper, causing the transaction to revert. The subscription is non-functional despite appearing properly funded, and the user's funds are locked in the minimum balance floor while the subscription remains active.

### Finding Description

`getMinimumBalance` is the canonical function used by subscribers, keepers, and the contract itself to enforce a funding floor:

```solidity
function getMinimumBalance(uint8 numPriceFeeds) external view override returns (uint256 minimumBalanceInWei) {
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
}
``` [1](#0-0) 

This value is enforced at `createSubscription`, `updateSubscription`, `addFunds`, and `withdrawFunds`. However, `updatePriceFeeds` deducts **two** separate fees from the subscription balance in sequence:

**Step 1 — Pyth fee deducted first:**
```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
if (status.balanceInWei < pythFee) { revert ...; }
status.balanceInWei -= pythFee;
status.totalSpent += pythFee;
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...);
``` [2](#0-1) 

**Step 2 — Keeper fee checked against the already-reduced balance:**
```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

`getMinimumBalance` only accounts for the keeper fee component (`minimumBalancePerFeed`). The Pyth fee is dynamic and is never included in the minimum balance formula. The two parameters are entirely separate:

```solidity
function _initialize(
    address admin,
    address pythAddress,
    uint128 minimumBalancePerFeed,
    uint128 singleUpdateKeeperFeeInWei
) internal {
``` [4](#0-3) 

### Impact Explanation

A subscriber who funds their subscription at exactly `getMinimumBalance()` — the value the contract itself advertises as sufficient — will have a subscription that:

1. Passes all creation/activation checks.
2. Cannot be updated: after `pythFee` is deducted, the remaining balance is `minimumBalance − pythFee`, which is less than `totalKeeperFee`, causing `_processFeesAndPayKeeper` to revert.
3. Locks the user's funds: `withdrawFunds` enforces the minimum balance floor while the subscription is active, so the user cannot recover the locked portion without first deactivating the subscription.

The subscription is non-functional despite appearing valid. Keepers who attempt to service it waste gas on guaranteed-to-revert transactions. The `getMinimumBalance` API is misleading: it returns a value that is insufficient to execute even a single update.

### Likelihood Explanation

Any subscriber who calls `getMinimumBalance(numFeeds)` to determine how much ETH to deposit — the natural and documented way to fund a subscription — will create a non-functional subscription. The Pyth fee is non-zero on all production deployments. This is a straightforward, unprivileged user action with no special preconditions.

### Recommendation

Include the Pyth fee in `getMinimumBalance`. Since the Pyth fee is dynamic, query it at the time of the check:

```solidity
function getMinimumBalance(uint8 numPriceFeeds) external view override returns (uint256 minimumBalanceInWei) {
    uint256 keeperFloor = uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    // Estimate Pyth fee for one update of numPriceFeeds feeds
    uint256 pythFeeEstimate = IPyth(_state.pyth).getUpdateFee(...); // use a representative estimate
    return keeperFloor + pythFeeEstimate;
}
```

Alternatively, document clearly that `getMinimumBalance` does not include the Pyth fee and that subscribers must add a Pyth fee buffer on top of the returned value.

### Proof of Concept

1. Deploy `SchedulerUpgradeable` with `minimumBalancePerFeed = 0.001 ether` and `singleUpdateKeeperFeeInWei = 0.0005 ether` for 2 price feeds.
2. `getMinimumBalance(2)` returns `0.002 ether`.
3. Subscriber calls `createSubscription{value: 0.002 ether}(...)` — succeeds.
4. Pyth contract charges `pythFee = 0.001 ether` per update (realistic on mainnet).
5. Keeper calls `updatePriceFeeds(subscriptionId, updateData)`:
   - `status.balanceInWei -= 0.001 ether` → remaining = `0.001 ether`
   - `totalKeeperFee = gasCost + 0.001 ether` > `0.001 ether` → **revert `InsufficientBalance`**
6. The subscription is permanently stuck. The subscriber cannot withdraw the `0.002 ether` minimum balance floor while the subscription is active. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L16-30)
```text
    function _initialize(
        address admin,
        address pythAddress,
        uint128 minimumBalancePerFeed,
        uint128 singleUpdateKeeperFeeInWei
    ) internal {
        require(admin != address(0), "admin is zero address");
        require(pythAddress != address(0), "pyth is zero address");

        _state.pyth = pythAddress;
        _state.admin = admin;
        _state.subscriptionNumber = 1;
        _state.minimumBalancePerFeed = minimumBalancePerFeed;
        _state.singleUpdateKeeperFeeInWei = singleUpdateKeeperFeeInWei;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
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

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-854)
```text
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
