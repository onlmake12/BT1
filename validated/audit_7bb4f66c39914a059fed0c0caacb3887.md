### Title
Governance VAA Execution Has No Timestamp Expiration — Stale Actions Can Be Applied Indefinitely - (File: `target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol`)

---

### Summary

Pyth governance VAAs carry no on-chain expiration check. Any governance VAA that was signed by the Wormhole guardian set but never submitted to a target chain can be submitted by any unprivileged caller at an arbitrarily later time, as long as no VAA with a higher sequence number has already been executed on that chain.

---

### Finding Description

Every Pyth governance execution path — EVM `PythGovernance.sol`, `Executor.sol`, Aptos `governance.move`, Sui `governance.move`, CosmWasm `contract.rs`, and NEAR `governance.rs` — validates a governance VAA using only two checks:

1. The emitter chain/address matches the configured governance source.
2. The VAA sequence number is strictly greater than the last executed sequence number.

There is no check that compares the VAA's embedded `timestamp` field against the current block time.

In `PythGovernance.sol::verifyGovernanceVM`:

```solidity
if (vm.sequence <= lastExecutedGovernanceSequence())
    revert PythErrors.OldGovernanceMessage();
setLastExecutedGovernanceSequence(vm.sequence);
```

No `block.timestamp - vm.timestamp > MAX_AGE` guard exists. [1](#0-0) 

The same pattern is present in `Executor.sol::verifyGovernanceVM`:

```solidity
if (vm.sequence <= lastExecutedSequence)
    revert ExecutorErrors.MessageOutOfOrder();
lastExecutedSequence = vm.sequence;
``` [2](#0-1) 

And in Aptos `governance.move::parse_and_verify_governance_vaa`:

```move
assert!(sequence > state::get_last_executed_governance_sequence(), ...);
state::set_last_executed_governance_sequence(sequence);
``` [3](#0-2) 

And in Sui `governance.move::execute_governance_instruction`:

```move
assert!(sequence > state::get_last_executed_governance_sequence(pyth_state), ...);
state::set_last_executed_governance_sequence(&latest_only, pyth_state, sequence);
``` [4](#0-3) 

And in CosmWasm `contract.rs::execute_governance_instruction`:

```rust
if vaa.sequence <= state.governance_sequence_number {
    Err(PythContractError::OldGovernanceMessage)?;
}
``` [5](#0-4) 

The `executeGovernanceInstruction` function is publicly callable by any address on all chains. [6](#0-5) 

---

### Impact Explanation

A governance VAA that was created (and signed by Wormhole guardians) but never submitted to a specific target chain remains permanently valid. An unprivileged caller can submit it at any future time. Concrete harmful scenarios:

- **`UpgradeContract`**: A VAA pointing to an implementation address that was later found to be vulnerable can be submitted to a chain where it was never applied, forcing a downgrade to a known-vulnerable version.
- **`WithdrawFee`**: A VAA withdrawing accumulated fees to a specific address can be submitted long after that address has been compromised or is no longer controlled by the intended recipient.
- **`SetDataSources` / `SetFee`**: Outdated configuration parameters can be applied to chains that missed the original submission window, silently reverting to stale settings.

The `Executor.sol` `execute()` function is particularly sensitive because it executes arbitrary calldata against any contract address, and a stale payload may reference state or addresses that have changed. [7](#0-6) 

---

### Likelihood Explanation

The scenario requires a governance VAA to exist that was never submitted to a specific chain. This is realistic in several situations:

- A new chain is added to Pyth's deployment after several governance actions have already been taken; the deployer must replay all prior VAAs in sequence, and a VAA may be skipped.
- A chain was temporarily unavailable (RPC outage, bridge downtime) during a governance execution campaign, leaving a gap in the sequence.
- The `sync_governance_vaas.ts` script iterates VAAs sequentially and skips those not targeting the current chain, but a VAA targeting `chain_id = 0` (all chains) that was skipped due to an error would remain submittable indefinitely. [8](#0-7) 

---

### Recommendation

Add a maximum age check in each governance execution path. For EVM:

```solidity
uint32 constant MAX_GOVERNANCE_VAA_AGE = 7 days;

function verifyGovernanceVM(bytes memory encodedVM) internal returns (...) {
    ...
    if (block.timestamp > vm.timestamp + MAX_GOVERNANCE_VAA_AGE)
        revert PythErrors.GovernanceVaaExpired();
    ...
}
```

Apply the equivalent check in Aptos, Sui, CosmWasm, NEAR, and Fuel governance handlers using their respective clock/time APIs.

---

### Proof of Concept

1. Governance multisig creates and executes a Squads proposal that emits a Wormhole VAA with sequence N targeting `UpgradeContract` on chain A, pointing to implementation `ImplV2`.
2. The VAA is submitted to all chains except chain A (e.g., due to an RPC failure).
3. Governance later discovers `ImplV2` has a critical bug and creates VAA sequence N+1 pointing to `ImplV3`. This is submitted to all chains including chain A (last executed on chain A becomes N+1 after this step — but only if N+1 > N, which it is, so chain A now has N+1 as last executed).

Wait — in this exact scenario the stale VAA (N) cannot be executed on chain A after N+1 is applied. The more precise scenario is:

1. Governance creates VAA sequence 50 (`UpgradeContract` to `ImplV2`) and submits it to chains B, C, D but NOT chain A (chain A last executed = 49).
2. `ImplV2` is found vulnerable. Governance creates VAA sequence 51 (`UpgradeContract` to `ImplV3`) and submits it to chains B, C, D but again NOT chain A (chain A last executed still = 49).
3. Any unprivileged caller now submits VAA sequence 50 to chain A. It passes the sequence check (50 > 49) and upgrades chain A to the vulnerable `ImplV2`.
4. The caller then submits VAA sequence 51 to chain A, upgrading to `ImplV3` — but the window between steps 3 and 4 exposes chain A to the vulnerable implementation, and any funds or state changes made during that window are at risk.

The root cause is that VAA sequence 50 carries no expiration and remains valid indefinitely on any chain where `lastExecutedGovernanceSequence < 50`.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L56-59)
```text
        if (vm.sequence <= lastExecutedGovernanceSequence())
            revert PythErrors.OldGovernanceMessage();

        setLastExecutedGovernanceSequence(vm.sequence);
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L64-65)
```text
    function executeGovernanceInstruction(bytes calldata encodedVM) public {
        IWormhole.VM memory vm = verifyGovernanceVM(encodedVM);
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L66-110)
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
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L131-134)
```text
        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;
```

**File:** target_chains/aptos/contracts/sources/governance/governance.move (L51-53)
```text
        let sequence = vaa::get_sequence(&parsed_vaa);
        assert!(sequence > state::get_last_executed_governance_sequence(), error::invalid_governance_sequence_number());
        state::set_last_executed_governance_sequence(sequence);
```

**File:** target_chains/sui/contracts/sources/governance/governance.move (L83-87)
```text
        assert!(sequence > state::get_last_executed_governance_sequence(pyth_state),
            E_CANNOT_EXECUTE_GOVERNANCE_ACTION_WITH_OBSOLETE_SEQUENCE_NUMBER);

        // Update latest executed sequence number to current one.
        state::set_last_executed_governance_sequence(&latest_only, pyth_state, sequence);
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L258-262)
```rust
    if vaa.sequence <= state.governance_sequence_number {
        Err(PythContractError::OldGovernanceMessage)?;
    } else {
        updated_config.governance_sequence_number = vaa.sequence;
    }
```

**File:** contract_manager/scripts/sync_governance_vaas.ts (L76-112)
```typescript
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
  while (true) {
    const submittedWormholeMessage = new SubmittedWormholeMessage(
      await matchedVault.getEmitter(),
      lastExecuted + 1,
      matchedVault.cluster,
    );
    let vaa: Buffer;
    try {
      vaa = await submittedWormholeMessage.fetchVaa();
    } catch (error) {
      console.log(error);
      console.log("no vaa found for sequence", lastExecuted + 1);
      break;
    }
    const parsedVaa = parseVaa(vaa);
    const action = decodeGovernancePayload(parsedVaa.payload);
    if (!action) {
      console.log("can not decode vaa, skipping");
    } else if (
      action.targetChainId === "unset" ||
      contract.getChain().wormholeChainName === action.targetChainId
    ) {
      console.log("executing vaa", lastExecuted + 1);
      await contract.executeGovernanceInstruction(
        toPrivateKey(argv["private-key"]),
        vaa,
      );
    } else {
      console.log(
        `vaa is not for this chain (${
          contract.getChain().wormholeChainName
        } != ${action.targetChainId}, skipping`,
      );
    }
    lastExecuted++;
  }
```
