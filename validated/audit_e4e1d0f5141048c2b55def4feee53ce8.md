### Title
Excess ETH Permanently Locked in Permanent Subscriptions via Unrestricted `addFunds` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `addFunds` function in `Scheduler.sol` allows any caller to deposit ETH into any active subscription. For subscriptions marked `isPermanent`, the `withdrawFunds` function unconditionally reverts, and `updateSubscription` also unconditionally reverts. There is no admin or governance escape hatch. Any ETH deposited into a permanent subscription beyond what keepers will ever consume is permanently locked with no recovery path.

### Finding Description
`addFunds` is a public payable function with no caller restriction: [1](#0-0) 

It accepts `msg.value` unconditionally and adds it to `status.balanceInWei`. For permanent subscriptions it only enforces a per-transaction cap (`MAX_DEPOSIT_LIMIT`), not a total-balance cap: [2](#0-1) 

`withdrawFunds` unconditionally reverts for permanent subscriptions: [3](#0-2) 

`updateSubscription` also unconditionally reverts for permanent subscriptions: [4](#0-3) 

There is no admin-level or governance-level function in `Scheduler.sol` that can recover ETH from a permanent subscription's balance. The only outflow is keeper payments via `_processFeesAndPayKeeper`. [5](#0-4) 

### Impact Explanation
Any ETH deposited into a permanent subscription via `addFunds` is irrecoverable by the depositor, the subscription manager, or the protocol admin. If a permanent subscription is overfunded relative to its actual keeper consumption (e.g., the subscription is for a low-frequency feed, or the keeper fee drops over time), the excess ETH is permanently locked in the contract. A third party can also call `addFunds` on any active permanent subscription, permanently locking their own ETH in that subscription with no recovery path. The contract has no governance or admin mechanism to drain or redirect a permanent subscription's balance.

### Likelihood Explanation
The `addFunds` function is intentionally open to any caller (confirmed by the test `testAnyoneCanAddFunds`). Subscription managers of permanent subscriptions may reasonably attempt to top up their balance and overshoot. The per-transaction `MAX_DEPOSIT_LIMIT` check does not prevent accumulation of excess balance across multiple calls. This is a realistic operational scenario. [6](#0-5) 

### Recommendation
1. Add a maximum total-balance cap for permanent subscriptions in `addFunds`, or
2. Provide an admin/governance function to recover excess balance from permanent subscriptions, or
3. Refund any ETH sent to `addFunds` that would push the balance above a configurable ceiling for permanent subscriptions.

### Proof of Concept
1. A subscription manager creates a permanent subscription with `createSubscription{value: minimumBalance}(params)` where `params.isPermanent = true`.
2. The manager later calls `addFunds{value: 100 ether}(subscriptionId)` — this succeeds and adds 100 ETH to `status.balanceInWei`.
3. The manager realizes they overfunded and calls `withdrawFunds(subscriptionId, 99 ether)` — this reverts with `CannotUpdatePermanentSubscription`.
4. The manager calls `updateSubscription(subscriptionId, newParams)` to deactivate — this also reverts with `CannotUpdatePermanentSubscription`.
5. The 100 ETH is permanently locked; the only outflow is keeper payments which may consume it over an arbitrarily long time horizon, or never if the subscription's feed activity is low. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-661)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
```text
    function _processFeesAndPayKeeper(
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
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

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
    }
```
