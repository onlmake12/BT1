### Title
Keeper Can Drain Subscription Balances via Uncapped `tx.gasprice` in `_processFeesAndPayKeeper` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `_processFeesAndPayKeeper` function computes keeper reimbursement using `tx.gasprice` with no upper bound. Because `updatePriceFeeds` is permissionless (no keeper registration required), any unprivileged caller can submit the transaction with an artificially inflated gas price, causing the contract to extract far more from the subscription balance than the actual transaction cost and transfer the surplus to the attacker.

---

### Finding Description

In `_processFeesAndPayKeeper`, the keeper fee is calculated as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is entirely attacker-controlled. The keeper pays `gas_used × tx.gasprice` to the block producer, but receives `(gas_used + GAS_OVERHEAD) × tx.gasprice` from the subscription balance. The net extraction from the subscription per call is:

```
GAS_OVERHEAD × tx.gasprice  +  singleUpdateKeeperFeeInWei × numPriceIds
```

`GAS_OVERHEAD` is hardcoded at `30000`: [2](#0-1) 

There is no cap on `tx.gasprice` anywhere in the payment path. The only guard is a balance check that reverts if the subscription cannot cover `totalKeeperFee`: [3](#0-2) 

`updatePriceFeeds` has no access control — any address may call it once update conditions are met: [4](#0-3) 

This is confirmed by the README: *"Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`."* [5](#0-4) 

---

### Impact Explanation

A malicious keeper can drain a subscription's entire balance in a single call (or a small number of calls) by submitting `updatePriceFeeds` with a gas price calibrated to extract the maximum amount the subscription can pay. The subscription manager's deposited funds — intended to pay for legitimate price updates over time — are stolen. Downstream readers relying on the subscription for price data will find it deactivated (balance below minimum) after the attack.

**Severity**: High — direct theft of user funds (subscription balances) held in the contract.

---

### Likelihood Explanation

- The attack requires no special privilege; any EOA can call `updatePriceFeeds`.
- The attacker must wait for update conditions (heartbeat or deviation) to be satisfied, but these are routine and occur frequently by design.
- The attacker pays `gas_used × tx.gasprice` to the block producer but receives `(gas_used + GAS_OVERHEAD) × tx.gasprice` from the subscription. The attack is profitable whenever `subscription_balance > gas_used × tx.gasprice`, which is trivially satisfiable by choosing `tx.gasprice` just below `subscription_balance / (gas_used + GAS_OVERHEAD)`.
- On chains with low base fees (many L2s where Pulse is deployed), the cost to the attacker is minimal.

**Likelihood**: Medium-High.

---

### Recommendation

Introduce a maximum gas price cap in `_processFeesAndPayKeeper`:

```solidity
uint256 MAX_GAS_PRICE = 500 gwei; // governance-configurable
uint256 effectiveGasPrice = Math.min(tx.gasprice, MAX_GAS_PRICE);
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Alternatively, make `MAX_GAS_PRICE` an admin-settable parameter (alongside `singleUpdateKeeperFeeInWei`) so it can be adjusted per chain.

---

### Proof of Concept

1. Subscription manager calls `createSubscription` with 2 ETH balance, 2 price feeds, heartbeat = 60 s.
2. 60 seconds pass; heartbeat condition is met.
3. Attacker (any EOA) calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 3000 gwei` (legacy transaction or EIP-1559 with `maxPriorityFeePerGas` set to inflate effective price).
4. Inside `_processFeesAndPayKeeper`:
   - `gas_used ≈ 200 000` (realistic for the full `updatePriceFeeds` call)
   - `gasCost = (200 000 + 30 000) × 3000 gwei = 0.69 ETH`
   - `keeperSpecificFee = singleUpdateKeeperFeeInWei × 2` (small)
   - `totalKeeperFee ≈ 0.69 ETH` — within the 2 ETH balance, so no revert.
5. Attacker pays `200 000 × 3000 gwei = 0.6 ETH` to the block producer.
6. Attacker receives `≈ 0.69 ETH` from the subscription.
7. **Net profit ≈ 0.09 ETH** per update call, with the subscription balance depleted by `0.69 ETH`.
8. Attacker repeats on the next heartbeat interval, draining the remaining balance.
9. Subscription balance falls below minimum; subscription is effectively bricked for the manager and all downstream readers. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L28-29)
```text
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-62)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
