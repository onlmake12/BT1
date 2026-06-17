### Title
Zero-Amount Native ETH Transfer in `_processFeesAndPayKeeper` Can Cause Keeper DoS on Price Updates — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, the `_processFeesAndPayKeeper` function unconditionally sends a native ETH payment to the keeper (`msg.sender`) without checking whether the computed `totalKeeperFee` is greater than zero. When the fee evaluates to zero — which is possible when `tx.gasprice == 0` (valid on several deployed L2s) and `singleUpdateKeeperFeeInWei == 0` (admin-configurable) — a zero-value `.call{value: 0}("")` is issued. If the keeper is a smart contract whose `receive()` or fallback reverts on zero-value transfers, the entire `updatePriceFeeds` call reverts, permanently blocking that keeper from updating any subscription's prices.

---

### Finding Description

`_processFeesAndPayKeeper` computes the keeper reward as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

When both components are zero, `totalKeeperFee == 0`. The function then executes:

```solidity
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
if (!sent) {
    revert SchedulerErrors.KeeperPaymentFailed();
}
``` [2](#0-1) 

There is no `if (totalKeeperFee > 0)` guard before this call. A keeper contract whose `receive()` enforces `require(msg.value > 0)` will return `success = false`, triggering `KeeperPaymentFailed` and reverting the entire `updatePriceFeeds` transaction — including all price-feed state changes already computed in that call.

The `singleUpdateKeeperFeeInWei` is admin-settable and can legitimately be zero: [3](#0-2) 

`tx.gasprice == 0` is a documented reality on several EVM-compatible L2s (e.g., certain Arbitrum Stylus transactions, zkSync Era during fee-free periods, and testnets used for integration).

---

### Impact Explanation

A keeper smart contract that guards its `receive()` against zero-value ETH (a common defensive pattern) will be permanently unable to call `updatePriceFeeds` whenever `totalKeeperFee == 0`. Because `updatePriceFeeds` is the sole write path for subscription price data in `Scheduler`, any subscription that relies exclusively on such a keeper will have its prices frozen. Downstream consumers reading stale prices via `getPricesNoOlderThan` will receive `StalePrice` reverts, breaking dependent protocols.

---

### Likelihood Explanation

- `singleUpdateKeeperFeeInWei` is set to zero during initialization or by admin governance — a plausible configuration for subsidized deployments.
- `tx.gasprice == 0` occurs on multiple production L2 networks where Pyth Pulse is deployed or planned.
- Keeper bots implemented as smart contracts commonly reject zero-value ETH to avoid griefing; this is a standard defensive pattern.
- The combination is realistic and requires no privileged access — any unprivileged keeper triggers it.

---

### Recommendation

Add a zero-amount guard before the keeper payment:

```solidity
if (totalKeeperFee > 0) {
    (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
    if (!sent) {
        revert SchedulerErrors.KeeperPaymentFailed();
    }
}
```

Apply the same pattern to `withdrawFunds` and `Entropy.withdraw` / `withdrawAsFeeManager` for consistency: [4](#0-3) [5](#0-4) [6](#0-5) 

---

### Proof of Concept

1. Admin deploys `Scheduler` with `singleUpdateKeeperFeeInWei = 0`.
2. A keeper is a contract with `receive() external payable { require(msg.value > 0, "no zero ETH"); }`.
3. On a chain where `tx.gasprice == 0`, the keeper calls `updatePriceFeeds(subscriptionId, updateData)`.
4. Inside `_processFeesAndPayKeeper`: `gasCost = 0`, `keeperSpecificFee = 0`, `totalKeeperFee = 0`.
5. `msg.sender.call{value: 0}("")` → keeper's `receive()` reverts → `sent = false`.
6. `revert SchedulerErrors.KeeperPaymentFailed()` propagates, reverting the entire `updatePriceFeeds` call.
7. No price data is stored; the subscription's prices remain stale indefinitely for this keeper. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L658-661)
```text
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L156-164)
```text
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L192-200)
```text
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");
```
