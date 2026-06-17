### Title
Zero-Amount Withdrawal Emits Misleading Events and Executes Zero-Value External Call — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `withdraw()` and `withdrawAsFeeManager()` functions lack a zero-amount guard. Any address (including unregistered ones for `withdraw`) can call these functions with `amount = 0`, causing the contract to emit two spurious `Withdrawal` events, execute a zero-value external call to `msg.sender`, and consume unnecessary gas — without any meaningful state change.

---

### Finding Description

`withdraw(uint128 amount)` in `Entropy.sol` performs only one check before proceeding:

```solidity
require(
    providerInfo.accruedFeesInWei >= amount,
    "Insufficient balance"
);
```

For an unregistered address, `providerInfo.accruedFeesInWei` defaults to `0`. Since `0 >= 0` is always true, the check passes. The function then:

1. Executes `providerInfo.accruedFeesInWei -= 0` (a no-op storage write that still costs gas).
2. Executes `msg.sender.call{value: 0}("")` — a zero-value external call that triggers the caller's `receive`/`fallback` function.
3. Emits `EntropyEvents.Withdrawal(msg.sender, msg.sender, 0)`.
4. Emits `EntropyEventsV2.Withdrawal(msg.sender, msg.sender, 0, bytes(""))`. [1](#0-0) 

`withdrawAsFeeManager(address provider, uint128 amount)` has the same flaw. It does check that the provider is registered (`sequenceNumber != 0`) and that `msg.sender` is the fee manager, but it does not check `amount > 0`. A legitimate fee manager can call it with `amount = 0` and trigger the same misleading event emission and zero-value call. [2](#0-1) 

Both events are defined in the SDK event interfaces: [3](#0-2) [4](#0-3) 

---

### Impact Explanation

- **Event log pollution**: Any address can spam the chain with `Withdrawal(addr, addr, 0)` events. Off-chain monitoring tools (e.g., Fortuna's `withdraw_fees_for_chain`, which reads `accruedFeesInWei` and acts on it) and indexers that track `Withdrawal` events to reconstruct provider fee balances will receive misleading signals.
- **Zero-value external call**: `msg.sender.call{value: 0}("")` executes the caller's `receive`/`fallback` function. A malicious contract can use this as a reentrancy probe or to trigger side effects in its own fallback at no cost.
- **Gas waste**: Unnecessary SSTORE and external call gas is consumed on every zero-amount call. [5](#0-4) 

---

### Likelihood Explanation

The entry path requires no privilege for `withdraw(0)`: any Ethereum address can call it. For `withdrawAsFeeManager(provider, 0)`, the caller must be the designated fee manager for a registered provider — still an unprivileged role that any provider can assign to any address. The call costs only gas, making repeated invocation trivially cheap. [6](#0-5) 

---

### Recommendation

Add a zero-amount guard at the top of both `withdraw` and `withdrawAsFeeManager`:

```solidity
// In withdraw():
if (amount == 0) revert EntropyErrors.AssertionFailure();

// In withdrawAsFeeManager():
if (amount == 0) revert EntropyErrors.AssertionFailure();
```

This mirrors the existing pattern used in `register()`: [7](#0-6) 

---

### Proof of Concept

```solidity
// Any EOA or contract can call this with no prior registration:
IEntropy entropy = IEntropy(ENTROPY_ADDRESS);
entropy.withdraw(0);
// Result:
//   - EntropyEvents.Withdrawal(msg.sender, msg.sender, 0) emitted
//   - EntropyEventsV2.Withdrawal(msg.sender, msg.sender, 0, "") emitted
//   - msg.sender.call{value: 0}("") executed
//   - No revert, no meaningful state change

// A registered provider's fee manager can do the same:
entropy.withdrawAsFeeManager(providerAddress, 0);
// Same two events emitted, same zero-value call executed
``` [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L118-118)
```text
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-173)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L175-209)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyEvents.sol (L65-69)
```text
    event Withdrawal(
        address provider,
        address recipient,
        uint128 withdrawnAmount
    );
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyEventsV2.sol (L144-149)
```text
    event Withdrawal(
        address indexed provider,
        address indexed recipient,
        uint128 withdrawnAmount,
        bytes extraArgs
    );
```
