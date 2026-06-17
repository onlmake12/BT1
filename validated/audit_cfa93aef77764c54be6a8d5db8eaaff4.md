### Title
Lazer `verifyUpdate()` Signed Payload Lacks Domain Separation — Cross-Chain and Temporal Replay of Price Updates - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signature hash as `keccak256(payload)` over raw payload bytes that contain no chain ID and no contract address. Because the same trusted signer keys are registered on every EVM deployment of `PythLazer`, a valid signed update captured on one chain is cryptographically indistinguishable from a valid update on any other EVM chain. An unprivileged relayer can replay a captured update — either across chains or temporally on the same chain — and `verifyUpdate` will accept it, returning the payload and signer as valid.

---

### Finding Description

In `PythLazer.verifyUpdate()`, the signed digest is constructed as:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` bytes contain only: a `PAYLOAD_MAGIC` constant (`2479346549`), a timestamp, a channel byte, and feed data. None of these fields are chain-specific. There is no `block.chainid`, no `address(this)`, and no nonce or sequence number bound to a specific contract instance. [2](#0-1) 

The EVM-format magic (`706910618`) checked at the outer envelope level is also a static constant, not chain-specific: [3](#0-2) 

Because Pyth registers the same trusted signer addresses across all EVM deployments of `PythLazer` (Ethereum, Arbitrum, Optimism, Base, etc.), a signature that passes `isValidSigner` on one chain passes on all chains. [4](#0-3) 

The Sui Lazer contract has the same structural issue — `secp256k1_ecrecover(signature, payload, 0)` is called directly on the raw payload with no chain-specific domain: [5](#0-4) 

However, the Sui `UPDATE_MESSAGE_MAGIC` (`1296547300`) differs from the EVM magic (`706910618`), so EVM↔Sui cross-chain replay is blocked by the outer magic check. The vulnerability is confined to EVM-to-EVM replay.

---

### Impact Explanation

**Cross-chain replay**: An attacker observes a valid signed Lazer update on Ethereum at time T (e.g., BTC/USD = $100,000). At time T+N (when the real price has moved to $90,000), the attacker submits the captured update to `PythLazer.verifyUpdate()` on Arbitrum. The function returns `(payload, signer)` as valid. Any consumer contract on Arbitrum that does not independently enforce timestamp freshness will accept the stale price and act on $100,000 instead of $90,000.

**Temporal replay on the same chain**: The same mechanism allows replaying any previously valid update on the same chain. `verifyUpdate` is stateless — it stores no record of consumed updates — so the same bytes can be submitted repeatedly.

Consumer contracts that do enforce a staleness window are protected, but the `PythLazer` contract itself provides no such guarantee, and the Lazer EVM integration guide does not mandate it as a security requirement. [6](#0-5) 

---

### Likelihood Explanation

- The attack requires no privileged access: any address can call `verifyUpdate` with a fee of 1 wei.
- Valid signed Lazer updates are publicly observable on-chain (submitted by any relayer).
- `PythLazer` is deployed on multiple EVM chains with the same signer set, making cross-chain replay immediately actionable.
- Consumer contracts that omit a timestamp staleness check are the direct victims; such omissions are common in integrations that rely on the oracle contract to enforce freshness.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed digest, analogous to EIP-712 domain separation:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

Additionally, consider adding a monotonic sequence number or nonce per signer to prevent temporal replay of individual updates.

---

### Proof of Concept

1. Deploy `PythLazer` on two EVM testnets (chain A and chain B) with the same trusted signer registered on both.
2. On chain A, call `verifyUpdate{value: fee}(update)` with a freshly signed Lazer update. Record the `update` bytes.
3. On chain B, call `verifyUpdate{value: fee}(update)` with the identical `update` bytes captured from step 2.
4. Observe that chain B returns `(payload, signer)` successfully — the signature is accepted despite being produced for chain A.
5. Parse the payload on chain B: the timestamp is from step 2, not the current time on chain B, confirming stale data injection.

The root cause is confirmed at: [7](#0-6) 

where `hash = keccak256(payload)` contains no chain-binding context.

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-106)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }

        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L110-136)
```text
    function parsePayloadHeader(
        bytes memory update
    )
        public
        pure
        returns (
            uint64 timestamp,
            PythLazerStructs.Channel channel,
            uint8 feedsLen,
            uint16 pos
        )
    {
        uint32 FORMAT_MAGIC = 2479346549;

        pos = 0;
        uint32 magic = _readBytes4(update, pos);
        pos += 4;
        if (magic != FORMAT_MAGIC) {
            revert("invalid magic");
        }
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
    }
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L49-63)
```text
    let current_cap = state.current_cap();

    // 0 stands for keccak256 hash
    let pubkey = secp256k1_ecrecover(signature, payload, 0);

    // Check if the recovered pubkey is in the trusted signers list
    let trusted_signers = state.trusted_signers(&current_cap);
    let mut maybe_idx = trusted_signers.find_index!(|signer|
        signer.public_key() == &pubkey
    );

    assert!(maybe_idx.is_some(), ESignerNotTrusted);
    let idx = maybe_idx.extract();
    let expires_at_ms = trusted_signers[idx].expires_at_ms();
    assert!(clock.timestamp_ms() < expires_at_ms, ESignerExpired);
```
