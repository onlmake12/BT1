### Title
Hardcoded `msg.sender` Recipient in `withdrawFunds` Prevents Smart Contract Managers from Recovering Subscription Balance — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.withdrawFunds` unconditionally transfers the withdrawn ETH to `msg.sender` with no option to specify a different recipient address. Any subscription manager that is a smart contract without a `receive()` or `fallback()` function (e.g., a multisig with a custom guard, an AA wallet, or a purpose-built manager contract) will have its entire subscription balance permanently locked in the Scheduler contract.

---

### Finding Description

In `Scheduler.sol`, the `withdrawFunds` function is gated by the `onlyManager` modifier and then sends the withdrawn ETH unconditionally to `msg.sender`:

```solidity
function withdrawFunds(
    uint256 subscriptionId,
    uint256 amount
) external override onlyManager(subscriptionId) {
    ...
    status.balanceInWei -= amount;

    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "Failed to send funds");   // <-- always reverts if manager cannot receive ETH
}
``` [1](#0-0) 

The subscription manager is recorded at creation time as `msg.sender`:

```solidity
_state.subscriptionManager[subscriptionId] = msg.sender;
``` [2](#0-1) 

There is no `recipient` parameter and no alternative withdrawal path. If the manager is a smart contract that cannot receive native ETH (no `receive()` / `fallback()`), every call to `withdrawFunds` will revert at the `require(sent, ...)` check, and the balance stored in `status.balanceInWei` is irrecoverable. [3](#0-2) 

The same pattern exists in `Entropy.withdraw`:

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "withdrawal to msg.sender failed");
``` [4](#0-3) 

---

### Impact Explanation

A subscription manager that is a smart contract without ETH-receive capability (e.g., a multisig with a reverting fallback, a DAO treasury contract, or an AA wallet whose execution module does not accept native transfers) will permanently lose all ETH deposited into the subscription. The `status.balanceInWei` accounting is decremented before the transfer, so even if the call is retried it will always fail — the balance is gone from the contract's perspective but the ETH never leaves. The only recovery path would be a contract upgrade, which requires governance action and is not guaranteed.

---

### Likelihood Explanation

Multisigs (Gnosis Safe, etc.) and AA wallets are the standard tooling for protocol teams managing on-chain subscriptions. While Gnosis Safe does include a `receive()` function, many custom treasury contracts, DAO executor contracts, and AA wallet implementations do not. The Scheduler is designed for protocol-level use, making smart-contract managers the expected common case rather than an edge case. Any such manager that lacks ETH-receive capability will trigger this permanent fund lock on the first `withdrawFunds` call.

---

### Recommendation

Add an optional `recipient` parameter to `withdrawFunds`, defaulting to `msg.sender` when not specified:

```solidity
function withdrawFunds(
    uint256 subscriptionId,
    uint256 amount,
    address recipient   // new parameter; pass address(0) to default to msg.sender
) external override onlyManager(subscriptionId) {
    ...
    address target = (recipient == address(0)) ? msg.sender : recipient;
    status.balanceInWei -= amount;
    (bool sent, ) = target.call{value: amount}("");
    require(sent, "Failed to send funds");
}
```

Apply the same fix to `Entropy.withdraw` and `Entropy.withdrawAsFeeManager`.

---

### Proof of Concept

1. Deploy a `ManagerContract` with no `receive()` function.
2. Call `Scheduler.createSubscription{value: minimumBalance}(params)` from `ManagerContract` — it becomes the subscription manager.
3. Call `ManagerContract.triggerWithdraw(subscriptionId, amount)` which internally calls `Scheduler.withdrawFunds(subscriptionId, amount)`.
4. The `msg.sender.call{value: amount}("")` targets `ManagerContract`, which has no `receive()`, so `sent == false`.
5. `require(sent, "Failed to send funds")` reverts.
6. `status.balanceInWei` was already decremented — the ETH is permanently locked.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L69-69)
```text
        _state.subscriptionManager[subscriptionId] = msg.sender;
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

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L24-30)
```text
        mapping(uint256 => SchedulerStructs.SubscriptionParams) subscriptionParams;
        /// Sub ID -> subscription status (metadata about their sub)
        mapping(uint256 => SchedulerStructs.SubscriptionStatus) subscriptionStatuses;
        /// Sub ID -> price ID -> latest parsed price update for the subscribed feed
        mapping(uint256 => mapping(bytes32 => PythStructs.PriceFeed)) priceUpdates;
        /// Sub ID -> manager address
        mapping(uint256 => address) subscriptionManager;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L163-164)
```text
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");
```
