### Title
Uncapped `tx.gasprice` in `_processFeesAndPayKeeper` Allows Any Keeper to Drain Subscription Balances — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler._processFeesAndPayKeeper` reimburses the caller for gas using `tx.gasprice` with no upper bound. Because `updatePriceFeeds` has no access control, any unprivileged actor can call it with an arbitrarily inflated gas price, causing the subscription balance to be drained far beyond the legitimate cost of the update.

---

### Finding Description

`updatePriceFeeds` is a permissionless function — any address may call it for any active subscription. [1](#0-0) 

At the end of a successful update, `_processFeesAndPayKeeper` is called: [2](#0-1) 

Inside that function the keeper fee has two components:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [3](#0-2) 

`tx.gasprice` is set by the transaction sender and is subject to no cap anywhere in the contract. The full `totalKeeperFee` is then transferred to `msg.sender`: [4](#0-3) 

The only guard is that `totalKeeperFee` must not exceed the remaining subscription balance: [5](#0-4) 

This means a caller can set `tx.gasprice` to any value that makes `totalKeeperFee` equal to (or just below) the entire subscription balance, draining it in a single transaction.

The `GAS_OVERHEAD` constant is 30 000 gas: [6](#0-5) 

The attacker pays the network `gasUsed × tx.gasprice` but receives `(gasUsed + GAS_OVERHEAD) × tx.gasprice` from the subscription, netting `GAS_OVERHEAD × tx.gasprice` in pure profit per call, plus the full reimbursement of their actual gas spend.

---

### Impact Explanation

A malicious keeper can drain the entire deposited balance of any active subscription in a single `updatePriceFeeds` call by submitting the transaction with a sufficiently high `tx.gasprice`. The subscription manager suffers direct loss of all deposited ETH. After the balance is exhausted, future legitimate updates revert with `InsufficientBalance`, causing service disruption for all readers of that subscription. The minimum-balance invariant enforced on `addFunds`/`withdrawFunds` provides no protection here because `_processFeesAndPayKeeper` is allowed to reduce the balance to zero. [7](#0-6) 

---

### Likelihood Explanation

- `updatePriceFeeds` is fully permissionless; no whitelist, role, or stake is required.
- Valid update data is freely obtainable from the public Hermes API.
- Setting `tx.gasprice` to an arbitrary value is a standard EVM transaction parameter — no special tooling is needed.
- The attacker only needs to satisfy the subscription's update criteria (heartbeat elapsed or price deviation exceeded), both of which are observable on-chain.
- The attacker is always profitable: they recover their inflated gas spend from the subscription and additionally pocket `GAS_OVERHEAD × tx.gasprice`.

---

### Recommendation

Cap the effective gas price used in the fee calculation to a protocol-defined maximum or to a recent on-chain gas price oracle value. For example:

```solidity
uint256 effectiveGasPrice = Math.min(tx.gasprice, maxGasPriceInWei);
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Alternatively, store a `maxGasPriceInWei` parameter (settable by admin) and revert if `tx.gasprice` exceeds it. This mirrors the pattern already used in the off-chain keeper (`max_fee_wei` in `adjust_fee_if_necessary`): [8](#0-7) 

---

### Proof of Concept

1. Subscription S has `balanceInWei = 10 ETH`, `priceIds.length = 2`.
2. Attacker observes that the heartbeat interval has elapsed (update condition met).
3. Attacker fetches valid `updateData` from Hermes.
4. Attacker submits `updatePriceFeeds(S, updateData)` with `tx.gasprice = 1_000_000 gwei` (1 mwei).
5. Assume actual gas used = 250 000. Then:
   - `gasCost = (250 000 + 30 000) × 1 000 000 gwei = 280 000 000 000 gwei = 280 ETH`
   - `keeperSpecificFee = singleUpdateKeeperFeeInWei × 2` (small)
   - `totalKeeperFee ≈ 280 ETH > 10 ETH` → transaction reverts with `InsufficientBalance`.
6. Attacker lowers `tx.gasprice` to exactly drain the balance: `tx.gasprice = 10 ETH / 280 000 ≈ 35 714 gwei`.
   - `gasCost = 280 000 × 35 714 gwei ≈ 10 ETH`
   - Attacker pays network `250 000 × 35 714 gwei ≈ 8.93 ETH`, receives `10 ETH` from subscription.
   - **Net profit ≈ 1.07 ETH** on a single call; subscription balance reduced to ~0.
7. Subscription can no longer fund future updates; service is disrupted.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L345-345)
```text
        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-849)
```text
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L852-854)
```text
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L856-857)
```text
        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L860-860)
```text
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
```

**File:** apps/fortuna/src/keeper/fee.rs (L339-344)
```rust
    if let Some(max_fee) = max_fee_wei {
        let effective_max = std::cmp::max(max_fee, min_fee_wei);
        target_fee_min = std::cmp::min(target_fee_min, effective_max);
        target_fee = std::cmp::min(target_fee, effective_max);
        target_fee_max = std::cmp::min(target_fee_max, effective_max);
    }
```
