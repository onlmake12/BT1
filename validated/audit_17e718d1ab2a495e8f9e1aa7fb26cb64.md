### Title
ETH Sent Directly to `Executor` Contract Is Permanently Stuck — (File: `target_chains/ethereum/contracts/contracts/executor/Executor.sol`)

---

### Summary

The `Executor` contract defines `receive() external payable {}`, silently accepting ETH sent directly to it. The contract has no withdrawal function. Any ETH deposited outside of a governance `execute()` call — or any excess `msg.value` beyond `gi.value` in an `execute()` call — is locked in the contract with no self-service recovery path.

---

### Finding Description

`Executor.sol` at line 113 defines:

```solidity
/// @dev Called when `msg.value` is not zero and the call data is empty.
receive() external payable {}
``` [1](#0-0) 

This silently accepts any ETH sent directly to the contract address. The contract has no `withdraw()` or rescue function. The only mechanism to move ETH out is the `execute()` function, which forwards exactly `gi.value` wei to `callAddress`:

```solidity
(success, response) = address(callAddress).call{value: gi.value}(gi.callData);
``` [2](#0-1) 

`execute()` is itself `public payable`: [3](#0-2) 

Two distinct ETH-locking paths exist:

1. **Direct transfer via `receive()`**: Any address can send ETH to the `Executor` with empty calldata. The ETH is accepted silently and credited to the contract's balance with no accounting entry and no withdrawal path.

2. **Excess `msg.value` in `execute()`**: A caller invoking `execute()` with `msg.value > gi.value` has the surplus permanently retained by the contract. There is no refund of the difference.

Recovery in both cases requires governance to issue a new Wormhole VAA instructing the `Executor` to forward the balance to a recovery address — a privileged, multi-step process that may never occur for small amounts.

---

### Impact Explanation

ETH sent to the `Executor` contract — either via direct transfer or as excess `msg.value` in `execute()` — is permanently locked unless the Pyth governance emitter issues a dedicated recovery instruction. There is no permissionless withdrawal path. For any user or relayer who accidentally sends ETH to this address, the funds are unrecoverable without governance intervention.

---

### Likelihood Explanation

The `Executor` contract address is publicly known and callable by anyone. Accidental ETH transfers to contract addresses are a common user error. Additionally, `execute()` is `public payable`, meaning any relayer submitting a governance VAA could accidentally include a non-zero `msg.value` exceeding `gi.value`, locking the surplus. The likelihood is low but non-negligible given the contract's public accessibility.

---

### Recommendation

1. **Remove or revert `receive()`**: If the contract is not intended to hold ETH outside of active `execute()` calls, change `receive()` to revert:
   ```solidity
   receive() external payable { revert("Executor does not accept ETH"); }
   ```

2. **Refund excess `msg.value` in `execute()`**: After the forwarding call, return any surplus to `msg.sender`:
   ```solidity
   uint256 excess = msg.value - gi.value;
   if (excess > 0) {
       (bool refunded, ) = msg.sender.call{value: excess}("");
       require(refunded, "refund failed");
   }
   ```

3. **Add a governance-gated rescue function** analogous to the `rescueETH()` pattern recommended in the reference report, callable only by the owner emitter.

---

### Proof of Concept

```solidity
// Step 1: Anyone sends ETH directly to the Executor
(bool success, ) = address(executor).call{value: 1 ether}("");
// success == true; ETH is now in executor.balance

// Step 2: No withdrawal function exists
// executor.withdraw() → does not exist
// executor.rescueETH() → does not exist

// Step 3: ETH is permanently locked unless governance issues a recovery VAA
// address(executor).balance == 1 ether, unrecoverable without governance action
``` [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-113)
```text
    function execute(
        bytes memory encodedVm
    ) public payable returns (bytes memory response) {
        IWormhole.VM memory vm = verifyGovernanceVM(encodedVm);

        GovernanceInstruction memory gi = parseGovernanceInstruction(
            vm.payload
        );

        if (gi.targetChainId != chainId && gi.targetChainId != 0)
            revert ExecutorErrors.InvalidGovernanceTarget();

        if (
            gi.action != ExecutorAction.Execute ||
            gi.executorAddress != address(this)
        ) revert ExecutorErrors.DeserializationError();

        // Check if the gi.callAddress is a contract account.
        uint len;
        address callAddress = address(gi.callAddress);
        assembly {
            len := extcodesize(callAddress)
        }
        if (len == 0) revert ExecutorErrors.InvalidContractTarget();

        bool success;
        (success, response) = address(callAddress).call{value: gi.value}(
            gi.callData
        );

        // Check if the call was successful or not.
        if (!success) {
            // If there is return data, the delegate call reverted with a reason or a custom error, which we bubble up.
            if (response.length > 0) {
                // The first word of response is the length, so when we call revert we add 1 word (32 bytes)
                // to give the pointer to the beginning of the revert data and pass the size as the second argument.
                assembly {
                    let returndata_size := mload(response)
                    revert(add(32, response), returndata_size)
                }
            } else {
                revert ExecutorErrors.ExecutionReverted();
            }
        }
    }

    /// @dev Called when `msg.value` is not zero and the call data is empty.
    receive() external payable {}
```
