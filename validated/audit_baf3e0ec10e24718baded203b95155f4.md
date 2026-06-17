### Title
Permissionless `initialize_pool` Accepts Arbitrary `pool_data` and `slash_custody` Accounts Without Validation — (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The integrity pool program's `initialize_pool` instruction accepts the `pool_data` and `slash_custody` accounts without any on-chain constraint or ownership/type validation. Because `initialize_pool` is permissionless (only requires a `payer` signer with no authority check), any unprivileged transaction sender can race to call it first and permanently bind the singleton `pool_config` PDA to attacker-controlled accounts.

---

### Finding Description

The `initialize_pool` instruction in the integrity pool program initializes the singleton `pool_config` PDA (seed: `"pool_config"`). Inspecting the IDL:

```json
{
  "name": "initialize_pool",
  "accounts": [
    { "name": "payer",         "signer": true, "writable": true },
    { "name": "config_account","pda": { ... } },
    { "name": "pool_data",     "writable": true },   // ← NO constraint, no PDA, no type check
    { "name": "pool_config",   "pda": { "seeds": ["pool_config"] }, "writable": true },
    { "name": "slash_custody" },                     // ← NO constraint whatsoever
    { "name": "system_program" }
  ]
}
```

Two critical accounts are accepted without any validation:

1. **`pool_data`** — stored as `pool_config.pool_data`. Every subsequent instruction that uses `pool_data` validates it only via `"relations": ["pool_config"]`, meaning it must equal whatever pubkey was stored at initialization. If an attacker supplies a fake account they control, all reward accounting (publisher caps, delegation state, claimable rewards) operates on attacker-controlled data.

2. **`slash_custody`** — stored as `pool_config.slash_custody`. The `slash` instruction transfers slashed PYTH tokens directly to `slash_custody` (validated only via `"relations": ["slash_event"]`, which itself derives from `pool_config.slash_custody`). If an attacker sets this to their own token account, they receive all future slashed tokens.

The `pool_config` PDA uses `init` (not `init_if_needed`), so it can only be created once. There is no authority constraint on `payer` — any signer can call `initialize_pool`. This creates a front-running window between program deployment and the legitimate operator's initialization call.

This is the direct analog to the CVGT report's `initialize_handler` accepting any CVGT mint without legitimacy checks, and the `config_pool_state_handler` allowing `cvgt_staking_state` to be set to a spoofed account.

---

### Impact Explanation

- **Corrupted reward accounting**: A fake `pool_data` account with attacker-controlled `del_state`, `self_del_state`, and `publishers` arrays causes `advance_delegation_record` to compute rewards from garbage or malicious data, enabling reward inflation or denial of rewards to legitimate stakers.
- **Theft of slashed PYTH tokens**: A fake `slash_custody` pointing to an attacker-controlled token account causes all tokens slashed from misbehaving publishers to be transferred to the attacker rather than the protocol treasury.
- **Permanent**: Because `pool_config` is a singleton PDA initialized exactly once, a successful front-run permanently corrupts the pool state for all future stakers.

---

### Likelihood Explanation

The attack requires the attacker to submit `initialize_pool` before the legitimate deployer. On Solana, this is achievable by:
- Monitoring the mempool or deployment scripts for the program deployment transaction
- Submitting `initialize_pool` with higher priority fees immediately after the program is deployed but before the operator's initialization call

The window is narrow but real, especially since program deployments are publicly observable on-chain. No privileged key, oracle compromise, or Sybil attack is required — only a standard transaction from any funded wallet.

---

### Recommendation

Add an authority constraint to `initialize_pool` so only a designated deployer/governance key can call it:

```rust
#[account(
    constraint = payer.key() == EXPECTED_DEPLOYER_PUBKEY @ ErrorCode::Unauthorized
)]
pub payer: Signer<'info>,
```

Additionally, add explicit type and ownership constraints on `pool_data` and `slash_custody`:

```rust
// pool_data: enforce it is owned by this program and has the correct discriminator
#[account(
    constraint = pool_data.to_account_info().owner == &crate::ID @ ErrorCode::InvalidPoolDataAccount
)]
pub pool_data: Account<'info, PoolData>,

// slash_custody: enforce it is a token account owned by the expected custody authority PDA
#[account(
    constraint = slash_custody.owner == expected_custody_authority @ ErrorCode::InvalidSlashCustodyAccount
)]
pub slash_custody: Account<'info, TokenAccount>,
```

---

### Proof of Concept

1. Deploy the integrity pool program to a test environment.
2. Before the legitimate operator calls `initialize_pool`, submit the following transaction as an unprivileged attacker:
   - Create a fake `PoolData`-shaped account (`pool_data_fake`) owned by the integrity pool program ID, with attacker-controlled publisher/delegation data.
   - Create an attacker-controlled SPL token account (`slash_custody_fake`) for PYTH.
   - Call `initialize_pool(reward_program_authority=attacker, y=<any>)` with `pool_data = pool_data_fake` and `slash_custody = slash_custody_fake`.
3. Observe that `pool_config.pool_data == pool_data_fake` and `pool_config.slash_custody == slash_custody_fake` are now permanently stored.
4. Call `create_slash_event` followed by `slash` on any staker — observe that slashed PYTH tokens are transferred to `slash_custody_fake` (attacker's account).
5. Call `advance_delegation_record` — observe that reward calculations use the attacker's fake `pool_data`, enabling reward manipulation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L545-606)
```json
    {
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "config_account",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                12, 74, 158, 192, 43, 86, 104, 29, 164, 155, 4, 186, 155, 36,
                207, 137, 253, 128, 249, 44, 241, 145, 227, 125, 189, 51, 111,
                70, 231, 183, 19, 217
              ]
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pool_data",
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
          },
          "writable": true
        },
        {
          "name": "slash_custody"
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
        },
        {
          "name": "y",
          "type": "u64"
        }
      ],
      "discriminator": [95, 180, 10, 172, 84, 174, 232, 40],
      "name": "initialize_pool"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L871-960)
```json
              "value": [
                12, 74, 158, 192, 43, 86, 104, 29, 164, 155, 4, 186, 155, 36,
                207, 137, 253, 128, 249, 44, 241, 145, 227, 125, 189, 51, 111,
                70, 231, 183, 19, 217
              ]
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "config_account",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "governance_target_account",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [116, 97, 114, 103, 101, 116]
              },
              {
                "kind": "const",
                "value": [118, 111, 116, 105, 110, 103]
              }
            ]
          },
          "writable": true
        },
        {
          "name": "slash_custody",
          "relations": ["slash_event"],
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "custody_authority",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [97, 117, 116, 104, 111, 114, 105, 116, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "address": "pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ",
          "name": "staking_program"
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        }
      ],
      "args": [
        {
          "name": "index",
          "type": "u64"
        }
      ],
      "discriminator": [204, 141, 18, 161, 8, 177, 92, 142],
      "name": "slash"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1337-1362)
```json
      "name": "PoolConfig",
      "type": {
        "fields": [
          {
            "name": "pool_data",
            "type": "pubkey"
          },
          {
            "name": "reward_program_authority",
            "type": "pubkey"
          },
          {
            "name": "pyth_token_mint",
            "type": "pubkey"
          },
          {
            "name": "y",
            "type": "u64"
          },
          {
            "name": "slash_custody",
            "type": "pubkey"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L186-205)
```typescript
  public async initializePool({
    rewardProgramAuthority,
    poolData,
    y,
  }: {
    rewardProgramAuthority: PublicKey;
    poolData: PublicKey;
    y: bigint;
  }) {
    const yAnchor = convertBigIntToBN(y);
    const instruction = await this.integrityPoolProgram.methods
      .initializePool(rewardProgramAuthority, yAnchor)
      .accounts({
        poolData,
        slashCustody: getStakeAccountCustodyAddress(poolData),
      })
      .instruction();

    return sendTransaction([instruction], this.connection, this.wallet);
  }
```
