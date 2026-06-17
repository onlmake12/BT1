### Title
Malicious Keeper Can Drain Subscription Balance by Manipulating `tx.gasprice` in `_processFeesAndPayKeeper` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` uses `tx.gasprice` directly and without any cap to compute the keeper's gas reimbursement. Because `tx.gasprice` is entirely under the keeper's control (the keeper is the transaction sender), a malicious keeper can set an arbitrarily high gas price to extract `GAS_OVERHEAD × tx.gasprice` profit per update call, draining a subscription's balance far faster than the subscription manager intended — analogous to the 1inch maker manipulating the taking amount through a hook.

---

### Finding Description

`_processFeesAndPayKeeper` computes the total keeper payment as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

The keeper's net economic position per update call is:

| Item | Amount |
|---|---|
| Revenue from subscription | `(gasUsed + GAS_OVERHEAD) × tx.gasprice + keeperSpecificFee` |
| Cost paid to validators/protocol | `gasUsed × tx.gasprice` |
| **Net profit** | **`GAS_OVERHEAD × tx.gasprice + keeperSpecificFee`** |

The gas cost the keeper pays to the network is fully reimbursed by the subscription. The `GAS_OVERHEAD` component is pure profit that scales linearly with `tx.gasprice`. There is no upper bound on `tx.gasprice` enforced anywhere in the contract.

The subscription manager deposits funds expecting to pay a predictable cost per update. The `getMinimumBalance` function is used to size the deposit: [2](#0-1) 

But the actual per-update cost is unbounded because `tx.gasprice` is keeper-controlled. The subscription manager has no mechanism to cap the gas price a keeper may use.

The `updatePriceFeeds` entry point is permissionless — anyone can call it: [3](#0-2) 

The README confirms this design: "Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`." [4](#0-3) 

---

### Impact Explanation

A malicious keeper can drain a subscription's entire deposited balance in a small number of updates (or even a single update) by setting `tx.gasprice` to an arbitrarily high value. The keeper pays the inflated gas cost upfront but is fully reimbursed from the subscription balance, netting `GAS_OVERHEAD × tx.gasprice` per call. Subscription managers who deposited funds expecting N updates at normal gas prices will find their balance exhausted after far fewer updates. For subscriptions with large balances (e.g., permanent subscriptions), the loss can be substantial.

---

### Likelihood Explanation

The attack requires no special privilege, no leaked key, and no governance majority. Any EOA can call `updatePriceFeeds`. The keeper must front the gas cost but is immediately reimbursed from the subscription, so the net capital requirement is zero (only a temporary float). The attack is repeatable on every update cycle. On chains with high `baseFee` or where the keeper can set a high `maxPriorityFeePerGas`, the profit per call is significant.

---

### Recommendation

Introduce a configurable maximum gas price cap in `_processFeesAndPayKeeper`:

```solidity
uint256 effectiveGasPrice = Math.min(tx.gasprice, _state.maxKeeperGasPriceInWei);
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

The `maxKeeperGasPriceInWei` parameter should be set by the admin and kept in line with realistic network gas prices. Alternatively, use a time-weighted average gas price oracle rather than the raw `tx.gasprice`.

---

### Proof of Concept

1. Subscription manager creates a subscription with 1 ETH balance, expecting 1000 updates at 10 gwei gas price.
2. Malicious keeper calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 10,000 gwei` (1000× normal).
3. Assume `gasUsed = 200,000` and `GAS_OVERHEAD = 50,000` (from `SchedulerConstants`).
4. Keeper pays to network: `200,000 × 10,000 gwei = 2 ETH`.
5. Subscription pays keeper: `(200,000 + 50,000) × 10,000 gwei = 2.5 ETH`.
6. Keeper net profit: `50,000 × 10,000 gwei = 0.5 ETH` extracted from the subscription in one call.
7. The subscription's 1 ETH balance is drained in 2 updates instead of the expected 1000.

The subscription manager had no way to prevent this — there is no `maxGasPrice` parameter, no slippage check, and no threshold analogous to the 1inch `thresholdAmount` that would revert when the actual cost exceeds the expected cost. [2](#0-1)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-62)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
