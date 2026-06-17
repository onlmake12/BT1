### Title
Scheduler Subscription Balance Drained at Arbitrary `tx.gasprice` With No Keeper Gas Price Cap - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `_processFeesAndPayKeeper` function reimburses any caller of `updatePriceFeeds` for the full gas cost at the transaction's `tx.gasprice` with no upper bound. Because `updatePriceFeeds` has no access control and `SubscriptionParams` contains no `maxGasPrice` field, any unprivileged keeper can submit the update transaction during peak network congestion (or deliberately set a high gas price) and drain a subscriber's pre-deposited balance far faster than the subscriber anticipated.

---

### Finding Description

`_processFeesAndPayKeeper` computes the keeper reimbursement as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is fully attacker-controlled: any EOA can call `updatePriceFeeds` with an arbitrarily high gas price. The function has no `onlyKeeper` or similar guard:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
``` [2](#0-1) 

The `SubscriptionParams` struct contains no `maxGasPrice` field that would let a subscriber cap what they are willing to pay per gas unit:

```solidity
struct SubscriptionParams {
    bytes32[] priceIds;
    address[] readerWhitelist;
    bool whitelistEnabled;
    bool isActive;
    bool isPermanent;
    UpdateCriteria updateCriteria;
}
``` [3](#0-2) 

The `SubscriptionStatus` struct only tracks `balanceInWei` and `totalSpent`; there is no on-chain record of the gas price at which updates were executed, so subscribers cannot detect or prevent overcharging: [4](#0-3) 

---

### Impact Explanation

A subscriber deposits ETH expecting their balance to last for a predictable number of updates at normal gas prices. A keeper executing at 100× the normal gas price drains the balance 100× faster. For `isPermanent` subscriptions — which cannot be updated or have funds withdrawn — this is irreversible: the subscriber cannot recover the remaining balance or change parameters to mitigate the drain. [5](#0-4) 

Even for non-permanent subscriptions, the subscriber may not notice until the balance is already exhausted, at which point the subscription silently stops being updated (price feeds go stale).

---

### Likelihood Explanation

`updatePriceFeeds` is intentionally permissionless — the codebase explicitly notes that `getActiveSubscriptions` has no access control so keepers can discover subscriptions. Any address can call `updatePriceFeeds` at any gas price. A rational keeper is incentivized to execute at the highest gas price the subscription balance can cover, since they receive the full reimbursement. This is not a theoretical edge case; it is the dominant strategy for a profit-maximizing keeper. [6](#0-5) 

---

### Recommendation

Add a `maxGasPriceInWei` field to `SubscriptionParams`. In `_processFeesAndPayKeeper`, cap the effective gas price used for reimbursement:

```solidity
uint256 effectiveGasPrice = params.maxGasPriceInWei > 0
    ? Math.min(tx.gasprice, params.maxGasPriceInWei)
    : tx.gasprice;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

This mirrors the recommendation in the referenced report (`maxGasCost` parameter) and gives subscribers a hard on-chain guarantee about their maximum per-update cost.

---

### Proof of Concept

1. Alice creates a subscription with `isPermanent = true` and deposits 1 ETH, expecting ~10,000 updates at 0.1 gwei gas price.
2. Bob (an unprivileged keeper) monitors the mempool and calls `updatePriceFeeds` with `gasPrice = 100 gwei` (1000× normal) whenever the heartbeat or deviation trigger fires.
3. Each update now costs ~1000× more from Alice's balance.
4. Alice's 1 ETH balance is exhausted after ~10 updates instead of ~10,000.
5. Because `isPermanent = true`, Alice cannot withdraw the remaining balance or change `maxGasPrice`.
6. Alice's subscription goes stale; any protocol relying on it reads outdated prices. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-641)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L683-684)
```text
    // This function is intentionally public with no access control to allow keepers to discover active subscriptions
    function getActiveSubscriptions(
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L844-863)
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

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L9-16)
```text
    struct SubscriptionParams {
        bytes32[] priceIds; // Array of Pyth price feed IDs to subscribe to
        address[] readerWhitelist; // Optional array of addresses allowed to read prices
        bool whitelistEnabled; // Whether to enforce whitelist or allow anyone to read
        bool isActive; // Whether the subscription is active
        bool isPermanent; // Whether the subscription can be updated
        UpdateCriteria updateCriteria; // When to update the price feeds
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L19-24)
```text
    struct SubscriptionStatus {
        uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
        uint256 balanceInWei; // Balance that will be used to fund the subscription's upkeep.
        uint256 totalUpdates; // Tracks update count across all feeds in the subscription (increments by number of feeds per update)
        uint256 totalSpent; // Counter of total fees paid for subscription upkeep in wei.
    }
```
