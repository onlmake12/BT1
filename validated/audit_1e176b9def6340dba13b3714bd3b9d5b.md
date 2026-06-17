### Title
Keeper Fee Drain via Attacker-Controlled `tx.gasprice` in `_processFeesAndPayKeeper` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `_processFeesAndPayKeeper` function computes the keeper reimbursement using the live `tx.gasprice` opcode with no upper bound. Because `tx.gasprice` is set entirely by the transaction originator (the keeper), any unprivileged keeper can inflate it to extract the maximum possible amount from a subscription's balance in a single call to `updatePriceFeeds`.

---

### Finding Description

`_processFeesAndPayKeeper` at line 846 of `Scheduler.sol` calculates the keeper fee as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

`tx.gasprice` is the gas price of the current transaction, which is chosen freely by the caller. There is no cap, no oracle-based reference price, and no time-weighted average applied to it. The only guard is:

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [2](#0-1) 

This means the keeper can set `tx.gasprice` to any value up to `subscriptionBalance / (gasUsed + GAS_OVERHEAD)` and the check will pass, transferring the entire subscription balance to `msg.sender`.

The `GAS_OVERHEAD` constant is 30,000 gas — a fixed estimate with no dynamic bound:

```solidity
uint256 public constant GAS_OVERHEAD = 30000;
``` [3](#0-2) 

The keeper network is explicitly permissionless — no registration is required:

> "Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`." [4](#0-3) 

The entry point is:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
    uint256 startGas = gasleft();
    ...
    _processFeesAndPayKeeper(status, startGas, params.priceIds.length);
``` [5](#0-4) 

---

### Impact Explanation

**Subscription balance drain (theft of funds).**

A malicious keeper calls `updatePriceFeeds` with a valid price update (satisfying all on-chain checks) but sets `tx.gasprice` to:

```
P_attack = subscriptionBalance / (gasUsed + GAS_OVERHEAD)
```

The contract then charges the subscription `(gasUsed + GAS_OVERHEAD) × P_attack = subscriptionBalance`, transferring the entire balance to the keeper. The subscription manager's deposited ETH is stolen.

For a keeper who is also a block proposer (validator), the gas fees paid to the network are reclaimed, making the net profit equal to the full subscription balance. For a non-validator keeper, the net wallet gain is `GAS_OVERHEAD × tx.gasprice + keeperSpecificFee` per call — always positive — while the upfront gas cost is `gasUsed × tx.gasprice`. The attack is profitable for validators and can be used as a griefing vector by non-validators.

---

### Likelihood Explanation

- `updatePriceFeeds` is permissionless; any address can call it.
- Setting `tx.gasprice` to an arbitrary value requires no special access — it is a standard transaction parameter.
- On PoS chains (Ethereum, Arbitrum, Base, etc.), block proposers routinely include their own transactions. MEV infrastructure (Flashbots, private mempools) makes this accessible to non-validators as well.
- The attack requires only a valid price update blob (freely obtainable from Hermes) and a subscription with a non-trivial balance.

---

### Recommendation

1. **Cap `tx.gasprice` in the fee calculation** using a governance-set or EIP-1559 base-fee-derived maximum:

```solidity
uint256 effectiveGasPrice = tx.gasprice < MAX_GAS_PRICE_WEI
    ? tx.gasprice
    : MAX_GAS_PRICE_WEI;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

2. **Use `block.basefee` as a reference** (EIP-1559 chains) and cap the effective gas price at a small multiple of `block.basefee` (e.g., `2 × block.basefee`), preventing a keeper from inflating the price beyond what the network actually charges.

3. **Separate the gas reimbursement from the keeper incentive**: reimburse actual gas at a capped rate and pay the keeper incentive as a fixed fee (`singleUpdateKeeperFeeInWei`), removing the variable `tx.gasprice` component entirely.

---

### Proof of Concept

```solidity
// Attacker contract
contract MaliciousKeeper {
    IScheduler scheduler;

    constructor(address _scheduler) {
        scheduler = IScheduler(_scheduler);
    }

    // Called with a very high tx.gasprice
    function drain(uint256 subscriptionId, bytes[] calldata updateData) external {
        // tx.gasprice is set by the caller to:
        //   subscriptionBalance / (estimatedGasUsed + GAS_OVERHEAD)
        // The subscription balance is fully transferred to address(this).
        scheduler.updatePriceFeeds(subscriptionId, updateData);
    }
}
```

**Steps:**
1. Attacker fetches a valid Pyth price update from Hermes for the target subscription's price IDs.
2. Attacker estimates `gasUsed` for `updatePriceFeeds` (e.g., ~300,000 gas for a 2-feed subscription).
3. Attacker reads `subscriptionBalance` from `getSubscription(subscriptionId)`.
4. Attacker submits `drain(subscriptionId, updateData)` with:
   `tx.gasprice = subscriptionBalance / (300000 + 30000) ≈ subscriptionBalance / 330000`
5. `_processFeesAndPayKeeper` computes `totalKeeperFee ≈ subscriptionBalance`, passes the balance check, and transfers the full balance to the attacker.
6. For a validator attacker, the gas cost is reclaimed; net profit equals the subscription balance.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-849)
```text
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L851-854)
```text
        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L29-29)
```text
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-60)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.
```
