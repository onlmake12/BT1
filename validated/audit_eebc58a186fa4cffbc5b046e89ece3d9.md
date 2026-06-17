### Title
Attacker-Controlled `tx.gasprice` in Keeper Fee Calculation Drains Subscription Balances - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes the keeper fee using `tx.gasprice` directly, with no upper bound. Because `updatePriceFeeds` is callable by any unprivileged address, a malicious keeper can submit the transaction with an arbitrarily inflated gas price, draining the subscription's entire balance in a single call.

---

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds` is a permissionless `external` function — no access control modifier restricts who may call it. [1](#0-0) 

At the end of a successful update, `_processFeesAndPayKeeper` is invoked: [2](#0-1) 

The critical line is:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```

`tx.gasprice` is entirely attacker-controlled. There is no cap, no sanity check, and no comparison against a reasonable market rate. The only guard is:

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This guard prevents the fee from *exceeding* the balance, but it does not prevent the attacker from setting `tx.gasprice` to a value that makes `totalKeeperFee` equal to (or just below) `status.balanceInWei`, thereby extracting the entire subscription balance in one transaction. [3](#0-2) 

---

### Impact Explanation

- Any active subscription's entire ETH balance can be drained by a single unprivileged transaction.
- The attacker receives the inflated keeper fee directly via `msg.sender.call{value: totalKeeperFee}("")`.
- After drainage, the subscription can no longer pay for future Pyth fee or keeper fee, halting all price feed updates for that subscription.
- Subscription owners who deposited funds (potentially up to `MAX_DEPOSIT_LIMIT`) lose their entire balance.
- This is a direct theft of user funds, not merely a denial of service.

**Impact: Critical** — direct loss of deposited user funds across all active subscriptions.

---

### Likelihood Explanation

- `updatePriceFeeds` requires no privileged role; any EOA can call it.
- Valid `updateData` is freely obtainable from the public Pyth price service.
- The attacker can estimate gas usage off-chain (e.g., via `eth_estimateGas`) and compute the exact `tx.gasprice` needed to drain the target subscription.
- No special conditions, governance majority, or leaked keys are required.
- The attack is profitable: the attacker receives the drained ETH directly.

**Likelihood: High** — trivially reachable by any unprivileged transaction sender.

---

### Recommendation

Cap `tx.gasprice` to a reasonable maximum before computing `gasCost`. For example:

```solidity
uint256 effectiveGasPrice = tx.gasprice < MAX_GAS_PRICE ? tx.gasprice : MAX_GAS_PRICE;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

`MAX_GAS_PRICE` should be set to a governance-configurable value reflecting realistic network conditions (e.g., 500 gwei). Alternatively, use a TWAP or EIP-1559 base fee oracle to bound the reimbursable gas price.

---

### Proof of Concept

1. Subscription owner creates a subscription with 1 ETH balance and 2 price feeds.
2. Attacker estimates that `updatePriceFeeds` consumes ~300,000 gas (including `GAS_OVERHEAD`).
3. Attacker computes: `tx.gasprice_needed = (1 ETH - singleUpdateKeeperFeeInWei * 2) / 300,000 ≈ 3,333 gwei`.
4. Attacker calls `updatePriceFeeds(subscriptionId, validUpdateData)` with `tx.gasprice = 3,333 gwei`.
5. `_processFeesAndPayKeeper` computes `totalKeeperFee ≈ 1 ETH`, passes the balance check, deducts the full balance, and transfers it to the attacker.
6. Subscription balance is now ~0 wei; all future updates revert with `InsufficientBalance`. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-280)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

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
