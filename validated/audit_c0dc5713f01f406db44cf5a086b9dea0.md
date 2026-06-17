### Title
Keeper-Controlled `tx.gasprice` Inflates Fee Extraction from Subscription Balances — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes the keeper reimbursement using `tx.gasprice` — a value fully controlled by the transaction sender — with no upper bound or validation. Any permissionless keeper can set an arbitrarily high gas price, causing the contract to drain a subscription's ETH balance far beyond the actual network cost of the update.

### Finding Description

In `_processFeesAndPayKeeper`, the gas reimbursement is calculated as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
// ...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
```

`tx.gasprice` is set by the caller and is not capped anywhere in the contract. Because `updatePriceFeeds` is permissionless (no keeper registration required per the README), any actor can call it with an inflated gas price. The contract then transfers `totalKeeperFee` — which scales linearly with `tx.gasprice` — directly to `msg.sender` from the subscription's deposited balance.

This is structurally identical to the reported vulnerability class: a manipulable on-chain value (`tx.gasprice`, analogous to a Curve pool spot balance) is read without validity bounds and used directly in a financial accounting function, allowing an attacker to inflate the output to their benefit.

### Impact Explanation

- **Subscription balance drain**: A keeper submitting with `tx.gasprice = 10,000 gwei` instead of the market rate of `10 gwei` causes the subscription to pay 1,000× the fair gas cost per update, exhausting the subscription's ETH balance in a fraction of the expected number of updates.
- **Direct profit extraction (validator/block-builder path)**: If the keeper is also the block producer (feasible on PoS chains, L2 sequencers, or via MEV infrastructure), they pay the inflated gas price to themselves and receive the same inflated amount from the subscription — a net profit equal to `(inflated_gasprice - actual_gasprice) * gasUsed` per call.
- **Griefing without profit**: Even without being a validator, a keeper can grief a subscription manager by draining their deposited funds, forcing them to constantly top up or lose service.

### Likelihood Explanation

- `updatePriceFeeds` is explicitly permissionless; no registration or stake is required to act as a keeper.
- Setting `tx.gasprice` to an arbitrary value is a one-line change in any transaction submission tool.
- On EVM chains where the attacker controls block inclusion (L2 sequencers, small PoS chains, MEV bundles), the profit path is direct and risk-free.
- The only on-chain guard is `if (status.balanceInWei < totalKeeperFee)`, which only prevents over-drafting — it does not bound the gas price.

### Recommendation

Introduce a maximum gas price cap in `_processFeesAndPayKeeper`. The contract should either:
1. Accept a `maxGasPrice` parameter set by the subscription manager at subscription creation time, and cap `tx.gasprice` to that value in the fee calculation; or
2. Read the block's `block.basefee` and add a configurable priority fee cap, computing `effectiveGasPrice = min(tx.gasprice, block.basefee + maxPriorityFeeCapWei)` before multiplying.

This mirrors the standard recommendation for the reported class: validate the return value before using it in financial accounting.

### Proof of Concept

1. Subscription manager creates a subscription and deposits 1 ETH.
2. Attacker (keeper) monitors the subscription for an eligible update trigger.
3. Attacker calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 100,000 gwei` (vs. market rate of ~10 gwei).
4. Inside `_processFeesAndPayKeeper`:
   - `gasUsed ≈ 200,000` (typical for the function)
   - `gasCost = (200,000 + GAS_OVERHEAD) * 100,000 gwei ≈ 20+ ETH`
   - If `status.balanceInWei < totalKeeperFee`, the call reverts (no harm, attacker retries with a lower but still inflated price that fits within the balance).
   - At `tx.gasprice = 4,000 gwei`: `gasCost ≈ 0.8 ETH`, draining the 1 ETH subscription in a single update instead of the ~100 updates the manager budgeted for at 10 gwei.
5. If the attacker is the block builder, they receive the 0.8 ETH from the subscription and pay ~0.008 ETH in actual network cost — netting ~0.79 ETH profit. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L58-62)
```markdown
### Keeper Network & Incentives

- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
