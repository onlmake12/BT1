### Title
Excess `msg.value` Sent to `Executor.execute()` Is Permanently Trapped Until Governance Recovery - (File: target_chains/ethereum/contracts/contracts/executor/Executor.sol)

### Summary

The `execute()` function in `Executor.sol` is `public payable` but forwards only `gi.value` (from the VAA payload) to the target contract, not `msg.value`. There is no check that `msg.value == gi.value`. Any excess ETH sent by the caller remains locked in the Executor contract and can only be recovered via a new governance VAA.

### Finding Description

`Executor.execute()` is declared `public payable`: [1](#0-0) 

The forwarded value is taken from the governance instruction payload (`gi.value`), not from `msg.value`: [2](#0-1) 

`gi.value` is parsed from the VAA bytes: [3](#0-2) 

There is no assertion that `msg.value == gi.value` and no refund of the difference. The contract has a `receive()` fallback, so it silently accepts and retains any excess: [4](#0-3) 

When `gi.value == 0` (the common case for governance actions such as contract upgrades, fee changes, or data-source updates) but the relayer includes ETH in the call, the entire `msg.value` is trapped. Recovery requires governance to publish a new VAA that calls back into the Executor or another contract to transfer the ETH out.

### Impact Explanation

Any ETH sent as `msg.value` in excess of `gi.value` is permanently locked in the Executor until governance explicitly acts to recover it. For a relayer who accidentally attaches ETH to a governance relay transaction (e.g., `gi.value = 0` but `msg.value = 1 ETH`), the funds are unrecoverable without a new governance cycle. This is a direct, quantifiable financial loss for the caller.

### Likelihood Explanation

`execute()` is `public` — any address can relay a valid governance VAA. Relayers are often automated scripts or bots. A misconfigured relayer that always attaches a non-zero `msg.value` (e.g., to cover gas on other chains, or due to a scripting error) would silently lose ETH on every governance relay. The scenario is realistic and has no on-chain protection against it.

### Recommendation

Add a check that `msg.value` equals `gi.value` before executing the call:

```solidity
function execute(
    bytes memory encodedVm
) public payable returns (bytes memory response) {
    // ... existing VAA verification ...

    require(msg.value == gi.value, "Executor: msg.value != gi.value");

    (success, response) = address(callAddress).call{value: gi.value}(
        gi.callData
    );
    // ...
}
```

This mirrors the fix recommended in the reference report: reject the transaction when the sent value does not match what is actually needed.

### Proof of Concept

1. Governance publishes a VAA with `gi.value = 0` (e.g., a `SetFee` or `UpgradeContract` action).
2. A relayer calls `executor.execute{value: 1 ether}(vaa)`.
3. The call to `callAddress.call{value: 0}(callData)` succeeds.
4. `address(executor).balance` increases by `1 ether`.
5. The relayer has permanently lost `1 ether`; no function on the Executor allows them to reclaim it.
6. Only a subsequent governance VAA (e.g., one that calls `executor` itself with calldata to transfer ETH) can recover the funds.

The existing test suite confirms the Executor holds ETH and uses `gi.value` (not `msg.value`) for forwarding: [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-68)
```text
    function execute(
        bytes memory encodedVm
    ) public payable returns (bytes memory response) {
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L91-94)
```text
        bool success;
        (success, response) = address(callAddress).call{value: gi.value}(
            gi.callData
        );
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L112-113)
```text
    /// @dev Called when `msg.value` is not zero and the call data is empty.
    receive() external payable {}
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L171-172)
```text
        gi.value = encodedInstruction.toUint256(index);
        index += 32;
```

**File:** target_chains/ethereum/contracts/test/Executor.t.sol (L211-226)
```text
        uint value = 1;
        vm.deal(address(executor), value);

        testExecute(
            address(callable),
            abi.encodeWithSelector(ICallable.fooPayable.selector),
            1,
            value
        );
        assertEq(callable.fooCount(), c + 1);
        assertEq(callable.lastCaller(), address(executor));
        assertEq(address(executor).balance, 0);
        assertEq(address(callable).balance, value);
        // Sanity check to make sure the check above is meaningful.
        assert(address(executor) != address(this));
    }
```
