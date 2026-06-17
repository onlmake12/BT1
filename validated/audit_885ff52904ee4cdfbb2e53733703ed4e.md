### Title
Missing Access Control on `set_publisher_stake_account` Allows Any Signer to Manipulate Publisher Stake Account Associations — (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `set_publisher_stake_account` instruction in the Integrity Pool program accepts a generic `signer` account with no authority constraint. Unlike every other privileged instruction in the same program (e.g., `create_slash_event`, `update_delegation_fee`, `update_reward_program_authority`), the `signer` field carries no `relations` binding to `pool_config`. Any unprivileged user can invoke this instruction to associate or disassociate any publisher from their stake account, directly corrupting the self-delegation reward accounting stored in `pool_data`.

---

### Finding Description

The Integrity Pool program's `set_publisher_stake_account` instruction is defined in the IDL as:

```json
{
  "name": "set_publisher_stake_account",
  "accounts": [
    { "name": "signer", "signer": true },
    { "name": "publisher", "docs": ["CHECK : The publisher will be checked against data in the pool_data"] },
    { "name": "pool_data", "relations": ["pool_config"], "writable": true },
    { "name": "pool_config", "pda": { "seeds": [{ "kind": "const", "value": [112,111,111,108,...] }] } },
    { "name": "new_stake_account_positions_option", "optional": true },
    { "name": "current_stake_account_positions_option", "optional": true }
  ]
}
``` [1](#0-0) 

The `signer` field is declared only as `"signer": true` with no `relations` constraint. In Anchor, `relations` is the mechanism that enforces a signer must match a specific authority field stored in another account. Without it, the runtime imposes no restriction on who the signer is.

Contrast this with every other privileged instruction in the same program. For example, `create_slash_event`, `update_delegation_fee`, and `update_reward_program_authority` all declare:

```json
{
  "name": "reward_program_authority",
  "relations": ["pool_config"],
  "signer": true
}
``` [2](#0-1) 

This `relations` binding enforces `pool_config.reward_program_authority == signer.key()` at the Anchor constraint layer. The `set_publisher_stake_account` instruction has no equivalent check.

The xc_admin CLI confirms the intended caller is the governance vault authority:

```typescript
signer: await vault.getVaultAuthorityPDA(targetCluster),
``` [3](#0-2) 

This intent is not enforced on-chain. The `publisher` account comment ("The publisher will be checked against data in the pool_data") refers only to validating the publisher's existence in the pool, not to restricting who the signer is.

The `pool_data` account stores a `publisher_stake_accounts` array of size 1024 that maps each publisher to their associated stake account: [4](#0-3) 

This array is what `set_publisher_stake_account` modifies.

---

### Impact Explanation

An unprivileged attacker can:

1. **Disassociation attack**: Call `set_publisher_stake_account` with `new_stake_account_positions_option = None` and `current_stake_account_positions_option = <publisher's legitimate stake account>`. This removes the publisher's self-delegation entry from `pool_data.publisher_stake_accounts`, causing the publisher to lose all self-delegation reward accounting. The `advance_delegation_record` instruction distributes rewards based on this mapping; a zeroed entry means the publisher's self-staked PYTH earns no rewards.

2. **Reassociation attack**: Call `set_publisher_stake_account` with an attacker-controlled stake account as `new_stake_account_positions_option`. This corrupts the self-delegation reward ratio calculations for that publisher slot, potentially redirecting self-delegation rewards.

The `pool_data` account is the central state for all reward distribution across the OIS protocol. Corrupting `publisher_stake_accounts` entries directly affects the `advance` and `advance_delegation_record` reward flows for all stakers delegated to the affected publisher. [5](#0-4) 

---

### Likelihood Explanation

The instruction is permissionlessly callable by any funded Solana account. No special knowledge, leaked key, or privileged role is required — only the ability to submit a transaction. The publisher's public key is on-chain and discoverable from `pool_data`. The attack requires a single transaction and costs only the Solana transaction fee. Likelihood is **high**.

---

### Recommendation

Add a `relations` constraint to the `signer` account in `set_publisher_stake_account` binding it to `pool_config.reward_program_authority` (or a dedicated `pool_config.publisher_authority` field if publishers should self-manage their stake accounts):

```rust
#[account(
    constraint = signer.key() == pool_config.reward_program_authority
        @ IntegrityPoolError::Unauthorized
)]
pub signer: Signer<'info>,
```

This mirrors the pattern already used in `create_slash_event`, `update_delegation_fee`, and `update_reward_program_authority`. [6](#0-5) 

---

### Proof of Concept

```typescript
// Attacker removes a publisher's stake account association
const attackerKeypair = Keypair.generate(); // any funded keypair
const targetPublisher = new PublicKey("<known publisher pubkey from pool_data>");
const publisherCurrentStakeAccount = new PublicKey("<publisher's current stake account>");

await integrityPoolProgram.methods
  .setPublisherStakeAccount()
  .accounts({
    signer: attackerKeypair.publicKey,       // ← any signer, no authority check
    publisher: targetPublisher,
    poolData: POOL_DATA_ADDRESS,
    poolConfig: POOL_CONFIG_PDA,
    newStakeAccountPositionsOption: null,    // disassociate
    currentStakeAccountPositionsOption: publisherCurrentStakeAccount,
  })
  .signers([attackerKeypair])
  .rpc();
// Result: pool_data.publisher_stake_accounts[publisherIndex] = PublicKey::default()
// Publisher's self-delegation rewards are now zeroed in all future advance() calls
``` [1](#0-0) [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-760)
```json
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "new_stake_account_positions_option",
          "optional": true
        },
        {
          "name": "current_stake_account_positions_option",
          "optional": true
        }
      ],
      "args": [],
      "discriminator": [99, 46, 72, 132, 100, 235, 211, 117],
      "name": "set_publisher_stake_account"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1083-1088)
```json
      "accounts": [
        {
          "name": "reward_program_authority",
          "relations": ["pool_config"],
          "signer": true
        },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1120-1152)
```json
    {
      "accounts": [
        {
          "name": "reward_program_authority",
          "relations": ["pool_config"],
          "signer": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          },
          "writable": true
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "reward_program_authority",
          "type": "pubkey"
        }
      ],
      "discriminator": [105, 58, 166, 4, 99, 253, 115, 225],
      "name": "update_reward_program_authority"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1380-1416)
```json
          {
            "name": "publishers",
            "type": {
              "array": ["pubkey", 1024]
            }
          },
          {
            "name": "del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "self_del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "publisher_stake_accounts",
            "type": {
              "array": ["pubkey", 1024]
            }
```

**File:** governance/xc_admin/packages/xc_admin_cli/src/index.ts (L1099-1099)
```typescript
          signer: await vault.getVaultAuthorityPDA(targetCluster),
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```
