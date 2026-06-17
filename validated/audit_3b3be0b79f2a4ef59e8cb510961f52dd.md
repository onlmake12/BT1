### Title
Subscription Manager Is Permanently Bound With No Transfer Function, Causing Irreversible ETH Lock - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary
In `Scheduler.sol`, the `subscriptionManager` role for each subscription is set once at creation time and can never be changed. There is no `transferManager` or equivalent function. If the manager address becomes inaccessible (e.g., a deprecated contract wallet, a Safe multisig that loses quorum, or a contract that self-destructs), all ETH held in that subscription is permanently locked and the subscription can never be updated or deactivated.

---

### Finding Description

At subscription creation, the manager is recorded as the transaction sender and stored in an immutable mapping:

```solidity
// Map subscription ID to manager
_state.subscriptionManager[subscriptionId] = msg.sender;
``` [1](#0-0) 

The `onlyManager` modifier gates all privileged operations — `updateSubscription` and `withdrawFunds` — exclusively to this address:

```solidity
if (_state.subscriptionManager[subscriptionId] == msg.sender) {
    _;
    return;
}
``` [2](#0-1) 

`withdrawFunds` is gated by `onlyManager`:

```solidity
function withdrawFunds(
    uint256 subscriptionId,
    uint256 amount
) external override onlyManager(subscriptionId) {
``` [3](#0-2) 

The `IScheduler` interface exposes no `transferManager`, `changeManager`, or admin-rescue function: [4](#0-3) 

There is no path — not even for the contract admin — to reassign the `subscriptionManager` mapping after creation.

---

### Impact Explanation

For any non-permanent subscription whose manager address becomes inaccessible:

1. **ETH permanently locked**: `withdrawFunds` is `onlyManager`; no other address can recover the balance.
2. **Subscription permanently active**: `updateSubscription` (which handles deactivation) is `onlyManager`; the subscription cannot be deactivated, so keepers continue to drain its balance until it hits zero.
3. **No admin escape hatch**: `SchedulerGovernance.sol` and the `IScheduler` interface contain no override for this scenario.

The locked ETH is real user funds deposited as subscription balance.

---

### Likelihood Explanation

The trigger condition is realistic and common in production:

- **Contract wallet migration**: A user creates a subscription from a Gnosis Safe at address `A`. They migrate to a new Safe at address `B`. Address `A` is now inaccessible. All funds in that subscription are permanently locked.
- **Deprecated protocol integration**: A DeFi protocol integrates Scheduler and creates subscriptions from its own contract address. If that contract is upgraded via a proxy that changes its address, or is deprecated, the subscriptions are orphaned.
- **Multisig quorum loss**: A multisig manager loses enough signers to fall below threshold. The subscription can never be updated or drained.

Any user who creates a subscription from a contract address (rather than an EOA) is exposed. Contract-based subscription managers are the expected use case for protocol integrations.

---

### Recommendation

Add a two-step manager transfer function, callable only by the current manager:

```solidity
function transferSubscriptionManager(
    uint256 subscriptionId,
    address newManager
) external onlyManager(subscriptionId) {
    require(newManager != address(0), "zero address");
    _state.pendingManager[subscriptionId] = newManager;
    emit SubscriptionManagerTransferInitiated(subscriptionId, msg.sender, newManager);
}

function acceptSubscriptionManager(uint256 subscriptionId) external {
    require(_state.pendingManager[subscriptionId] == msg.sender, "not pending manager");
    _state.subscriptionManager[subscriptionId] = msg.sender;
    delete _state.pendingManager[subscriptionId];
    emit SubscriptionManagerTransferred(subscriptionId, msg.sender);
}
```

Alternatively, add an admin-level rescue function restricted to the contract owner for emergency fund recovery.

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription from a contract wallet `ContractWalletV1`:
   ```solidity
   uint256 subId = scheduler.createSubscription{value: 1 ether}(params);
   // _state.subscriptionManager[subId] == address(ContractWalletV1)
   ```
2. `ContractWalletV1` is deprecated; its owner migrates to `ContractWalletV2`.
3. Attempt to withdraw from `ContractWalletV2`:
   ```solidity
   vm.prank(address(ContractWalletV2));
   scheduler.withdrawFunds(subId, 1 ether);
   // Reverts: Unauthorized — msg.sender != subscriptionManager[subId]
   ```
4. Attempt from any other address including the contract admin:
   ```solidity
   vm.prank(admin);
   scheduler.withdrawFunds(subId, 1 ether);
   // Reverts: Unauthorized
   ```
5. The 1 ETH is permanently locked. No function in `IScheduler` or `SchedulerGovernance` can recover it. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-75)
```text
    function createSubscription(
        SchedulerStructs.SubscriptionParams memory subscriptionParams
    ) external payable override returns (uint256 subscriptionId) {
        _validateSubscriptionParams(subscriptionParams);

        // Calculate minimum balance required for this subscription
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        // Set subscription to active
        subscriptionParams.isActive = true;

        subscriptionId = _state.subscriptionNumber++;

        // Store the subscription parameters
        _state.subscriptionParams[subscriptionId] = subscriptionParams;

        // Initialize subscription status
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        status.priceLastUpdatedAt = 0;
        status.balanceInWei = msg.value;
        status.totalUpdates = 0;
        status.totalSpent = 0;

        // Map subscription ID to manager
        _state.subscriptionManager[subscriptionId] = msg.sender;

        _addToActiveSubscriptions(subscriptionId);

        emit SubscriptionCreated(subscriptionId, msg.sender);
        return subscriptionId;
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L751-755)
```text
        // Manager is always allowed
        if (_state.subscriptionManager[subscriptionId] == msg.sender) {
            _;
            return;
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L1-60)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

import "@pythnetwork/pyth-sdk-solidity/IPyth.sol";
import "@pythnetwork/pyth-sdk-solidity/PythStructs.sol";
import "./SchedulerEvents.sol";
import "./SchedulerStructs.sol";

interface IScheduler is SchedulerEvents {
    /// @notice Creates a new subscription
    /// @dev Requires msg.value to be at least the minimum balance for the subscription (calculated by getMinimumBalance()).
    /// @param subscriptionParams The parameters for the subscription
    /// @return subscriptionId The ID of the newly created subscription
    function createSubscription(
        SchedulerStructs.SubscriptionParams calldata subscriptionParams
    ) external payable returns (uint256 subscriptionId);

    /// @notice Gets a subscription's parameters and status
    /// @param subscriptionId The ID of the subscription
    /// @return params The subscription parameters
    /// @return status The subscription status
    function getSubscription(
        uint256 subscriptionId
    )
        external
        view
        returns (
            SchedulerStructs.SubscriptionParams memory params,
            SchedulerStructs.SubscriptionStatus memory status
        );

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

    /// @notice Updates price feeds for a subscription.
    /// @dev The updateData must contain all price feeds for the subscription, not a subset or superset.
    /// @dev Internally, the updateData is verified using the Pyth contract and validates update conditions.
    ///      The call will only succeed if the update conditions for the subscription are met.
    /// @param subscriptionId The ID of the subscription
    /// @param updateData The price update data from Pyth
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external;

    /// @notice Returns the price of a price feed without any sanity checks.
    /// @dev This function returns the most recent price update in this contract without any recency checks.
    /// This function is unsafe as the returned price update may be arbitrarily far in the past.
    ///
    /// Users of this function should check the `publishTime` in the price to ensure that the returned price is
    /// sufficiently recent for their application. If you are considering using this function, it may be
```
