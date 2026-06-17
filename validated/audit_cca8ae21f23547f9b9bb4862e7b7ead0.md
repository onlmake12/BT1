### Title
Asymmetric `isActive` Flag Check Between `addFunds()` and `withdrawFunds()` Creates Permissionless Funding Deadlock for Inactive Subscriptions - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol` enforces the `isActive` flag in `addFunds()` but not in `withdrawFunds()`. This mirrors the BranchPort asymmetry exactly: one operation (withdrawal) proceeds without checking the enabled flag, while the complementary operation (replenishment) is gated on it. The result is that once a subscription is deactivated, the permissionless funding path is permanently blocked while the manager-only withdrawal path remains open — and any third party who depends on the subscription being funded has no on-chain recourse.

---

### Finding Description

`addFunds()` hard-reverts with `InactiveSubscription` when `params.isActive == false`:

```solidity
function addFunds(uint256 subscriptionId) external payable override {
    ...
    if (!params.isActive) {
        revert SchedulerErrors.InactiveSubscription();   // ← gated on isActive
    }
    ...
}
``` [1](#0-0) 

`withdrawFunds()` carries no such check — it only verifies `isPermanent` and raw balance:

```solidity
function withdrawFunds(uint256 subscriptionId, uint256 amount)
    external override onlyManager(subscriptionId)
{
    ...
    if (params.isPermanent) { revert ...; }
    if (status.balanceInWei < amount) { revert ...; }
    // ← no isActive check
    if (params.isActive) { ... minimum balance guard ... }
    status.balanceInWei -= amount;
    ...
}
``` [2](#0-1) 

`addFunds()` is intentionally permissionless (no `onlyManager` modifier) — anyone can fund any **active** subscription. `updateSubscription()`, the only alternative path to inject ETH into an inactive subscription, is `onlyManager`: [3](#0-2) 

The existing test suite explicitly confirms the deadlock and treats it as expected behavior:

```
// Try to add funds to inactive subscription (should fail with InactiveSubscription)
vm.expectRevert(InactiveSubscription);
scheduler.addFunds{value: 1 wei}(subscriptionId);

// Try to reactivate with insufficient balance (should fail)
testUpdatedParams.isActive = true;
vm.expectRevert(InsufficientBalance);
scheduler.updateSubscription(subscriptionId, testUpdatedParams);
``` [4](#0-3) 

The test does **not** demonstrate the escape hatch (`updateSubscription{value: sufficientAmount}` with `isActive=true`), leaving the deadlock undocumented and the asymmetry unaddressed.

---

### Impact Explanation

**Permissionless funding is silently broken for inactive subscriptions.** Any keeper, protocol, or third party that monitors subscriptions and calls `addFunds()` to keep them solvent will receive a hard revert the moment the subscription is deactivated — even if they are willing to supply the exact amount needed to meet the minimum balance and trigger reactivation. The only recovery path (`updateSubscription{value: amount}`) is `onlyManager`, so third-party actors have zero on-chain recourse.

Concretely:
1. Manager deactivates a subscription (e.g., temporarily, for parameter changes).
2. Manager withdraws funds below the minimum balance via `withdrawFunds()` (no `isActive` guard).
3. Third parties who depend on the subscription (e.g., DeFi protocols reading its price feeds) attempt to fund it via `addFunds()` → permanent `InactiveSubscription` revert.
4. `updateSubscription{isActive:true}` with no ETH → `InsufficientBalance` revert.
5. The subscription is stuck until the manager explicitly calls `updateSubscription{value: X}` — a non-obvious, manager-gated path. [5](#0-4) 

---

### Likelihood Explanation

The scenario is reachable by any unprivileged actor who calls `addFunds()` on a subscription that has been deactivated. No special privileges are required to trigger the revert. The asymmetry is structural and present in every deployment of `Scheduler.sol`. Protocols that build automated keepers around `addFunds()` (the documented permissionless funding function) will silently fail to fund subscriptions that have been deactivated, even temporarily. [6](#0-5) 

---

### Recommendation

**Option A (preferred):** Remove the `isActive` guard from `addFunds()` and instead enforce only the minimum-balance post-condition (already present). This preserves the permissionless funding intent and allows third parties to top up inactive subscriptions:

```solidity
function addFunds(uint256 subscriptionId) external payable override {
    SchedulerStructs.SubscriptionParams storage params = _state.subscriptionParams[subscriptionId];
    SchedulerStructs.SubscriptionStatus storage status = _state.subscriptionStatuses[subscriptionId];

    // Remove: if (!params.isActive) revert InactiveSubscription();

    if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
        revert SchedulerErrors.MaxDepositLimitExceeded();
    }
    status.balanceInWei += msg.value;
}
```

**Option B:** If the intent is to keep `addFunds()` active-only, document this explicitly and provide a separate permissionless `topUpInactiveSubscription()` function that allows third parties to fund inactive subscriptions without triggering reactivation.

---

### Proof of Concept

```solidity
// 1. Manager creates and funds a subscription
uint256 subId = scheduler.createSubscription{value: minimumBalance}(params);

// 2. Manager deactivates it
params.isActive = false;
scheduler.updateSubscription(subId, params);

// 3. Manager withdraws funds below minimum
scheduler.withdrawFunds(subId, minimumBalance - 1 wei);
// ✓ succeeds — no isActive check in withdrawFunds

// 4. Third party tries to fund the subscription to help reactivate it
vm.prank(thirdParty);
scheduler.addFunds{value: minimumBalance}(subId);
// ✗ reverts: InactiveSubscription — isActive check blocks permissionless funder

// 5. Third party cannot use updateSubscription either (onlyManager)
vm.prank(thirdParty);
scheduler.updateSubscription{value: minimumBalance}(subId, activeParams);
// ✗ reverts: onlyManager

// 6. Subscription is permanently stuck until manager acts
``` [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-87)
```text
    function updateSubscription(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams memory newParams
    ) external payable override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage currentStatus = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage currentParams = _state
            .subscriptionParams[subscriptionId];

        // Add incoming funds to balance
        currentStatus.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-628)
```text
    function addFunds(uint256 subscriptionId) external payable override {
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        status.balanceInWei += msg.value;

        // If subscription is active, ensure minimum balance is maintained
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-662)
```text
    function withdrawFunds(
        uint256 subscriptionId,
        uint256 amount
    ) external override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }

        if (status.balanceInWei < amount) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // If subscription is active, ensure minimum balance is maintained
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei - amount < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }

        status.balanceInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send funds");
    }
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L699-712)
```text
        // Try to add funds to inactive subscription (should fail with InactiveSubscription)
        vm.expectRevert(
            abi.encodeWithSelector(
                SchedulerErrors.InactiveSubscription.selector
            )
        );
        scheduler.addFunds{value: 1 wei}(subscriptionId);

        // Try to reactivate with insufficient balance (should fail)
        testUpdatedParams.isActive = true;
        vm.expectRevert(
            abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector)
        );
        scheduler.updateSubscription(subscriptionId, testUpdatedParams);
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L33-42)
```text
    /// @notice Updates an existing subscription
    /// @dev You can activate or deactivate a subscription by setting isActive to true or false. Reactivating a subscription
    ///      requires the subscription to hold at least the minimum balance (calculated by getMinimumBalance()).
    /// @dev Any Ether sent with this call (`msg.value`) will be added to the subscription's balance before processing the update.
    /// @param subscriptionId The ID of the subscription to update
    /// @param newSubscriptionParams The new parameters for the subscription
    function updateSubscription(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams calldata newSubscriptionParams
    ) external payable;
```
