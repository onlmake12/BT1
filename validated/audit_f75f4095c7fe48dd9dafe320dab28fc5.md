### Title
ERC-20 Fee Tokens Permanently Locked in Starknet Pyth Contract — No Withdrawal Mechanism Exists - (File: `target_chains/starknet/contracts/src/pyth.cairo`)

### Summary
The Starknet Pyth contract collects fees exclusively in ERC-20 tokens (ETH, STRK, or any configured token) via `transferFrom` on every `update_price_feeds` call. These tokens accumulate inside the contract. However, unlike the EVM deployment which has a `WithdrawFee` governance action that sends native ETH, the Starknet contract implements no analogous withdrawal path for ERC-20 tokens. The `WithdrawFee` governance payload is also structurally incompatible with Starknet (it encodes a 20-byte Ethereum address, not a 252-bit Starknet felt address). All accumulated fee tokens are permanently locked.

### Finding Description
The Starknet Pyth contract is initialized with two ERC-20 fee token addresses (`fee_token_address1`, `fee_token_address2`) and corresponding per-update fees. [1](#0-0) 

Every call to `update_price_feeds` pulls ERC-20 tokens from the caller into the contract via `transferFrom`. Fees accumulate in the contract's own token balance. [2](#0-1) 

The `execute_governance_instruction` dispatcher handles `SetFee`, `SetFeeInToken`, `SetDataSources`, `SetWormholeAddress`, `AuthorizeGovernanceDataSourceTransfer`, and `RequestGovernanceDataSourceTransfer` — but **no `WithdrawFee` case exists** in the Starknet contract. A grep across all Starknet Cairo files for `withdraw_fee`, `WithdrawFee`, or `withdraw.*fee` returns zero matches. [3](#0-2) 

By contrast, the EVM `PythGovernance.sol` does implement `WithdrawFee`, but it only transfers native ETH via `call{value: amount}("")` — irrelevant to Starknet's token-based fee model. [4](#0-3) 

Furthermore, the cross-chain `WithdrawFee` governance payload format encodes a 20-byte Ethereum address as the recipient, which is structurally incompatible with Starknet's 252-bit felt address space. [5](#0-4) 

### Impact Explanation
All ERC-20 fee tokens (ETH, STRK, or any configured token) paid by every Starknet user calling `update_price_feeds` accumulate in the Pyth contract with no recovery path. As Starknet usage grows, the locked value grows proportionally. The Pyth governance body has no on-chain mechanism to reclaim these funds. This is a permanent, irreversible loss of protocol revenue.

### Likelihood Explanation
This is certain to occur — it is already occurring on every mainnet `update_price_feeds` call on Starknet. No attacker action is required; the normal operation of the protocol causes fees to accumulate. The only uncertainty is the total dollar value locked, which increases over time.

### Recommendation
Add a `WithdrawFee` governance action handler to the Starknet Pyth contract that:
1. Accepts a Starknet-compatible recipient address (felt/ContractAddress) and a token address.
2. Calls `transfer(recipient, amount)` on the specified ERC-20 token contract.
3. Defines a new Starknet-specific governance payload format (e.g., `WithdrawFeeStarknet`) that encodes a 252-bit Starknet address and a token contract address, since the existing 20-byte EVM payload format is incompatible.

### Proof of Concept
1. Deploy the Starknet Pyth contract with `fee_token_address1 = STRK_TOKEN`.
2. Any user calls `update_price_feeds(data)` — the contract calls `STRK.transferFrom(user, pyth_contract, fee)`. STRK balance of `pyth_contract` increases.
3. Governance attempts to issue a `WithdrawFee` VAA targeting Starknet. The Starknet `execute_governance_instruction` dispatcher has no `WithdrawFee` match arm — the instruction is silently ignored or panics as unrecognized.
4. The STRK tokens remain locked in the contract indefinitely with no recovery path. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/starknet/contracts/src/pyth.cairo (L124-130)
```text
    struct Storage {
        wormhole_address: ContractAddress,
        fee_token_address1: ContractAddress,
        fee_token_address2: ContractAddress,
        single_update_fee1: u256,
        single_update_fee2: u256,
        data_sources: Map<usize, DataSource>,
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L289-291)
```text
        fn update_price_feeds(ref self: ContractState, data: ByteBuffer) {
            self.update_price_feeds_internal(data, array![], 0, 0, false);
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L293-308)
```text
        fn get_update_fee(self: @ContractState, data: ByteBuffer, token: ContractAddress) -> u256 {
            let single_update_fee = if token == self.fee_token_address1.read() {
                self.single_update_fee1.read()
            } else if token == self.fee_token_address2.read() {
                self.single_update_fee2.read()
            } else {
                panic_with_felt252(GetSingleUpdateFeeError::UnsupportedToken.into())
            };

            let mut reader = ReaderImpl::new(data);
            read_and_verify_header(ref reader);
            let wormhole_proof_size = reader.read_u16();
            reader.skip(wormhole_proof_size.into());
            let num_updates = reader.read_u8();
            single_update_fee * num_updates.into()
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L412-450)
```text
        fn execute_governance_instruction(ref self: ContractState, data: ByteBuffer) {
            let wormhole = IWormholeDispatcher { contract_address: self.wormhole_address.read() };
            let vm = wormhole.parse_and_verify_vm(data.clone());
            self.verify_governance_vm(@vm);
            let instruction = governance::parse_instruction(vm.payload);
            if instruction.target_chain_id != 0
                && instruction.target_chain_id != wormhole.chain_id() {
                panic_with_felt252(GovernanceActionError::InvalidGovernanceTarget.into());
            }
            match instruction.payload {
                GovernancePayload::SetFee(payload) => {
                    self.set_fee(payload.value, payload.expo, self.fee_token_address1.read());
                },
                GovernancePayload::SetFeeInToken(payload) => {
                    self.set_fee(payload.value, payload.expo, payload.token);
                },
                GovernancePayload::SetDataSources(payload) => {
                    let new_data_sources = payload.sources;
                    let old_data_sources = self.write_data_sources(@new_data_sources);
                    let event = DataSourcesSet { old_data_sources, new_data_sources };
                    self.emit(event);
                },
                GovernancePayload::SetWormholeAddress(payload) => {
                    if instruction.target_chain_id == 0 {
                        panic_with_felt252(GovernanceActionError::InvalidGovernanceTarget.into());
                    }
                    self.check_new_wormhole(payload.address, data);
                    self.wormhole_address.write(payload.address);
                    let event = WormholeAddressSet {
                        old_address: wormhole.contract_address, new_address: payload.address,
                    };
                    self.emit(event);
                },
                GovernancePayload::RequestGovernanceDataSourceTransfer(_) => {
                    // RequestGovernanceDataSourceTransfer can be only part of
                    // AuthorizeGovernanceDataSourceTransfer message
                    panic_with_felt252(GovernanceActionError::InvalidGovernanceMessage.into());
                },
                GovernancePayload::AuthorizeGovernanceDataSourceTransfer(payload) => {
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L264-272)
```text
    function withdrawFee(WithdrawFeePayload memory payload) internal {
        if (payload.fee > address(this).balance)
            revert PythErrors.InsufficientFee();

        (bool success, ) = payload.targetAddress.call{value: payload.fee}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(payload.targetAddress, payload.fee);
    }
```

**File:** governance/xc_admin/packages/xc_admin_common/src/governance_payload/WithdrawFee.ts (L8-14)
```typescript
  static layout: BufferLayout.Structure<
    Readonly<{ targetAddress: string; value: bigint; expo: bigint }>
  > = BufferLayout.struct([
    BufferLayoutExt.hexBytes(20, "targetAddress"), // Ethereum address as hex string
    BufferLayoutExt.u64be("value"), // uint64 for value
    BufferLayoutExt.u64be("expo"), // uint64 for exponent
  ]);
```
