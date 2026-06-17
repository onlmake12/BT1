### Title
Cross-Chain Replay of Lazer Price Updates Due to Missing Chain ID in Signed Payload - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

The `PythLazer.sol` `verifyUpdate` function verifies a Lazer price update by recovering the signer from `keccak256(payload)`. The signed payload contains no chain ID and no contract address. Because the same Pyth Lazer trusted signer key is registered across all EVM deployments, a validly signed update captured on one chain passes signature verification on any other EVM chain where PythLazer is deployed.

---

### Finding Description

In `verifyUpdate`, the hash committed to by the signer is computed as:

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

The `payload` bytes contain only `[PAYLOAD_FORMAT_MAGIC][timestamp_u64][channel_u8][feedsLen_u8][feed data...]`: [2](#0-1) 

No `block.chainid`, no contract address, and no nonce are included in the signed data. The `isValidSigner` check only verifies that the recovered address has not expired: [3](#0-2) 

Because the Pyth Lazer infrastructure registers the same trusted signer key across all EVM deployments (the key is a Pyth-controlled infrastructure key, not chain-specific), the identical `update` bytes submitted on Ethereum will pass `verifyUpdate` on Arbitrum, Base, BNB Chain, or any other EVM chain where PythLazer is deployed with the same signer.

The Sui contract has the same structural gap — the signed payload is `keccak256(payload)` with no chain-domain binding: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker who observes the public Lazer WebSocket stream can:

1. Capture a validly signed Lazer update at time `T` with price `P` on chain A.
2. Submit those exact bytes to `PythLazer.verifyUpdate` on chain B.
3. `verifyUpdate` returns `(payload, signer)` with no error — the signature is cryptographically valid.
4. Any consumer contract on chain B that does not independently enforce timestamp freshness will accept price `P` (which may be stale relative to chain B's current state).

Consumer contracts that rely solely on `verifyUpdate` returning a non-zero signer — without checking `timestamp` against a recency window — will accept the replayed stale price. DeFi protocols (lending, perpetuals, options) using Lazer prices without their own staleness guard are directly exposed to price manipulation via stale-price injection.

The `verifyUpdate` function itself performs no timestamp check: [5](#0-4) 

---

### Likelihood Explanation

- **Attacker capability**: Anyone can subscribe to the Lazer WebSocket and capture signed updates. No privileged access is required.
- **Precondition**: PythLazer must be deployed on at least two EVM chains with the same trusted signer key (the standard production setup).
- **Consumer exposure**: Consumer contracts that do not enforce a freshness window on the returned `timestamp` are directly vulnerable. The Cardano integration docs explicitly warn that the contract "does not enforce freshness of the update, nor does it disallow verifying the same update multiple times," confirming this is a known gap left to integrators — many of whom may not implement it correctly. [6](#0-5) 

---

### Recommendation

Include `block.chainid` and the contract address in the signed message, analogous to EIP-712 domain separation:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

This ensures a signature produced for chain A is cryptographically invalid on chain B, eliminating cross-chain replay. Additionally, consider enforcing a maximum age on the payload `timestamp` inside `verifyUpdate` to prevent same-chain replay of stale updates.

---

### Proof of Concept

```python
# 1. Subscribe to Lazer WebSocket and capture a signed update on Ethereum
update_bytes = capture_lazer_update(chain="ethereum")  # public WebSocket

# 2. Submit the identical bytes to PythLazer on Arbitrum
# verifyUpdate accepts it — same signer key, same payload bytes, no chain binding
tx = arbitrum_pyth_lazer.verifyUpdate(update_bytes, value=fee)

# 3. Consumer contract on Arbitrum receives (payload, signer) with no revert
# If consumer does not check payload timestamp vs block.timestamp, stale price P is accepted
```

The `verifyUpdate` call on Arbitrum succeeds because `keccak256(payload)` is identical to what was signed for Ethereum, and the same trusted signer address is registered on both chains. [5](#0-4)

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

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L43-63)
```text
public(package) fun verify_le_ecdsa_message(
    state: &State,
    clock: &Clock,
    signature: &vector<u8>,
    payload: &vector<u8>,
) {
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
