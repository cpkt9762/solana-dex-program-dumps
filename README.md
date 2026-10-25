# solana-dex-program-dumps

Daily snapshots of the on-chain program binaries (`.so`) of the major Solana DEXs, collected
from mainnet with the official Solana CLI by GitHub Actions. Every deployed version that was
observed is kept, named by its `last_deployed_slot`, for reverse engineering, auditing and
version checks.

- Binaries: [GitHub releases](https://github.com/cpkt9762/solana-dex-program-dumps/releases), one per day with changes (tag `snapshot-YYYY-MM-DD`),
  each file named `<name>-<last_deployed_slot>.so`
- Metadata of the latest version: `dumps/<name>/metadata.json`
- Metadata of superseded versions: `archives/<name>/<last_deployed_slot>/metadata.json`
- Every version of every program in one file: [`index.json`](index.json)
- Programs tracked: see [Tracked programs](#tracked-programs) and [`registry.json`](registry.json)

## Where things are

Git only holds text. The binaries are release assets, so the repository stays small, downloads
have no quota, and a single version can be removed without rewriting git history.

```text
registry.json                               programs to track (name, program_id, type, description)
index.json                                  all known versions per program, newest first, plus links
dumps/<name>/metadata.json                  latest version
archives/<name>/<slot>/metadata.json        superseded versions
scripts/collect.py                          collector (Python 3.11+, standard library only)
tests/test_collect.py                       unit tests (no network needed)
.github/workflows/collect.yml               daily job: collect, publish releases, commit metadata
release snapshot-<date> -> <name>-<slot>.so  binaries first collected that day (UTC)
```

## Downloading binaries

```bash
# One version, from its metadata
curl -LO "$(jq -r .download_url dumps/pump-fun/metadata.json)"

# With the GitHub CLI
gh release download snapshot-2026-10-24 --pattern 'pump-fun-*.so' --repo cpkt9762/solana-dex-program-dumps

# The latest version of every program
jq -r '.programs[].versions[0].download_url // empty' index.json | xargs -n1 curl -LO
```

## How it works

For every program in `registry.json`, `scripts/collect.py` does the following:

1. `solana program show` reads `last_deployed_slot`, the upgrade authority and the ProgramData address.
2. It compares the slot with `dumps/<name>/metadata.json`. If the slot is unchanged, the program is
   skipped without downloading anything.
3. If the slot changed, or no slot exists (see below), `solana program dump` downloads the binary,
   which is then compared by sha256.
4. On a real change the binary is uploaded as `<name>-<slot>.so` to the release of the day,
   `snapshot-YYYY-MM-DD` (UTC; the first change of the day creates it). Only after that succeeds,
   the old `metadata.json` is moved to `archives/<name>/<old slot>/` and the new one is written.
   `index.json` is regenerated at the end of every run.

Change detection uses `last_deployed_slot` first and sha256 second. Programs owned by the
non-upgradeable loader (for example `orca-v1`) report no slot, so they are compared by sha256 only and
named `sha256-<first 12 hex chars>` instead of a slot (file `orca-v1-sha256-4f316b5d1eb4.so`).
At the end of a run with changes, the release notes are rewritten to list every program in that
snapshot, so a second run on the same day only adds to it.

A failure for one program (RPC error, timeout, malformed binary, GitHub API error) is logged and
retried with backoff and never stops the run. The script exits with 1 if any program failed, so the
workflow turns red while still committing the programs that did update. If a program is redeployed
while it is being downloaded, it is downloaded again, so the slot always matches the bytes.
Publishing is idempotent: if a run uploaded a file but failed before pushing the metadata, the
next run finds the file in the release and reuses it.

### Version rules

| Where | What |
|---|---|
| `dumps/<name>/metadata.json` | always the newest version seen |
| `archives/<name>/<slot>/metadata.json` | the version that was live at `last_deployed_slot = <slot>` before it was replaced |
| `<name>-<slot>.so` in release `snapshot-<date>` | the binary of that version, kept after it is superseded |
| `previous_slot` | slot of the version that this one replaced (null for the first one) |
| `collected_at` | UTC time at which this version was first observed, not the deploy time |

The deploy time itself is not stored: it can be looked up from the slot (`solana block-time <slot>`),
as long as the RPC node still has that block.

### `metadata.json`

```json
{
  "name": "raydium-amm-v4",
  "program_id": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
  "loader": "BPFLoaderUpgradeab1e11111111111111111111111",
  "program_data_address": "A7ZG7ByDi8DpzT9Ab7CiXhvgYTJQmaDPJkMDoPitaCQV",
  "sha256": "3849aece6fee86df0090d2824396f29220b5948da34d2ab5c7167655684a6467",
  "size": 308616,
  "onchain_sha256": "3bcf0828cf38e5f703adbf31dc1bf81f0a1254dcb176ca950ae52231c20e6d14",
  "onchain_size": 1406384,
  "last_deployed_slot": 445763204,
  "upgrade_authority": "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK",
  "collected_at": "2026-10-24T18:05:25Z",
  "previous_slot": null,
  "release_tag": "snapshot-2026-10-24",
  "download_url": "https://github.com/cpkt9762/solana-dex-program-dumps/releases/download/snapshot-2026-10-24/raydium-amm-v4-445763204.so"
}
```

`program_data_address`, `last_deployed_slot` and `upgrade_authority` are `null` for non-upgradeable
programs; `upgrade_authority` is also `null` once a program has been made immutable.

### Padding is stripped from the stored file

A ProgramData account is usually allocated larger than the program (Pump.fun: 10.5 MB on chain,
1.9 MB of real ELF), and the rest is zeros. The published `.so` is the on-chain data without that
padding: everything up to the last non-zero byte or the end of the ELF headers' extent (program
headers, section headers, segments, sections), whichever is larger. Only zero bytes are ever removed,
and the result is still a complete, valid ELF file. (Plain trailing-zero stripping is not enough:
the last section header usually ends in zero bytes.)

Both hashes are recorded so either form can be verified:

| Field | Covers |
|---|---|
| `sha256`, `size` | the published `.so` (padding stripped) |
| `onchain_sha256`, `onchain_size` | the raw bytes exactly as `solana program dump` writes them |

### Verifying a dump

```bash
solana program dump <program_id> /tmp/raw.so --url https://api.mainnet-beta.solana.com
sha256sum /tmp/raw.so            # equals onchain_sha256 while the program is unchanged
sha256sum <name>-<slot>.so       # equals sha256
```

## Run locally

Requirements: Python 3.11+ and the [Solana CLI from Anza](https://docs.anza.xyz/cli/install)
(`solana` and `solana-keygen` in `PATH`). No third-party Python packages are needed.

```bash
# Try it without touching the repository or GitHub: metadata and binaries go to ./dry-run
python3 scripts/collect.py --dry-run dry-run --only pump-fun phoenix

# Publish for real (needs the GitHub CLI logged in with write access to the repository)
python3 scripts/collect.py --release-repo cpkt9762/solana-dex-program-dumps

python3 -m unittest discover -s tests            # unit tests
```

`--dry-run DIR` copies the current metadata into `DIR` (which must be empty or not exist), runs the
full collection there and saves the binaries under `DIR/releases/<tag>/`. Other options:
`--verify-hash` (always download and compare sha256), `--retries N`, `--delay SECONDS` (pause between
programs, default 0.5), `--root PATH`, `-v`.

The Solana CLI refuses to run without a signer even for read-only commands, so the script creates a
throwaway keypair in a temporary directory for each run. It holds no funds, and nothing is ever
signed or sent.

## Configure the RPC endpoint

The collector reads the `RPC_URL` environment variable and falls back to the public
`https://api.mainnet-beta.solana.com`. The public endpoint is rate limited; for reliable daily runs
use your own provider.

- Locally: `RPC_URL=https://your-rpc.example python3 scripts/collect.py ...`
- GitHub Actions: add a repository secret named `SOLANA_RPC_URL`
  (Settings > Secrets and variables > Actions). If it is not set, the public RPC is used.
- Optional repository variable `SOLANA_VERSION` (for example `v3.1.14`) pins the Solana CLI release
  that the workflow installs; the default is `stable`.

The RPC URL is never logged, and URLs in CLI error output are redacted.

## Add a program

1. Append an entry to `registry.json`:

   ```json
   {"name": "my-dex", "program_id": "<base58 program id>", "type": "amm", "description": "My DEX",
    "github": "https://github.com/owner/repo"}
   ```

   `name` becomes a directory name and part of the file names, so it must be lowercase letters,
   digits and single hyphens, and both `name` and `program_id` must be unique. `github`
   (`https://github.com/...`) and `website` (`https://...`) are optional; give `website` only when
   there is no GitHub link.
2. Try it: `python3 scripts/collect.py --dry-run dry-run --only my-dex`
3. Refresh the table below with `python3 scripts/collect.py --print-registry-table`.
4. Commit. The next scheduled run (or a manual run of the workflow) publishes it.

Do not rename an existing `name`: its history lives under that name.

## Remove a program or a version

For example after a takedown request:

```bash
jq -r '.programs[] | select(.name == "<name>") | .versions[] | "\(.release_tag) \(.download_url)"' index.json   # its files
gh release delete-asset snapshot-<date> <name>-<slot>.so --yes                                          # delete one version
```

To stop tracking a program, also remove it from `registry.json` and delete `dumps/<name>/` and
`archives/<name>/` (or keep them as a record without binaries). None of this needs a history
rewrite, because git never contained the binaries.

## GitHub Actions

`.github/workflows/collect.yml` runs daily at 02:00 UTC and can be started manually from the
Actions tab (optionally for a subset of programs, or with sha256 verification). It installs the
Solana CLI, runs the unit tests, runs the collector (which creates the releases with the workflow's
`GITHUB_TOKEN`), then commits and pushes the metadata as `github-actions[bot]` with a message that
lists every program that changed. The workflow requests `contents: write`, which covers both pushes
and releases.

Things worth knowing:

- **Why releases and not Git LFS.** LFS downloads by anyone (clones, forks) count against the
  repository owner's quota, and once it is exhausted cloners only get pointer files. Release assets
  have no bandwidth or total size limit (each file must be under 2 GiB; the largest program here is
  about 2.6 MB), and git history only grows by small JSON files.
- **First run.** The first run publishes one release with every program (about 52 MB in total). It
  takes a few minutes because programs are fetched one after another.
- **"Source code" archives.** GitHub attaches them to every release and offers no way to hide them;
  they only contain this repository's scripts and metadata. One release per day keeps them rare.
- **Idle repositories.** GitHub disables scheduled workflows after 60 days without repository
  activity. If nothing was committed for 45 days the job adds an empty keepalive commit.
- **No overlapping runs.** A concurrency group serialises runs.
- **Failures.** If some programs cannot be collected, the others are still committed and the job
  is marked as failed at the end.

## Tracked programs

Program IDs come from the [Jupiter program-id-to-label list](https://lite-api.jup.ag/swap/v1/program-id-to-label)
plus manual additions. Every ID below was dumped successfully from mainnet; names, types and
descriptions are best effort. The `type` column is a rough classification: `amm` constant
product or SPL-token-swap style, `clmm` / `dlmm` concentrated or bin liquidity, `clob` order book,
`stable-swap`, `pmm` oracle-priced or proprietary market makers (mostly closed source),
`bonding-curve` launchpads, `perp` perpetuals, `lst-swap` liquid staking token swaps.

The `Link` column is the project's GitHub when one is known, otherwise its official website; it is
empty when neither could be confirmed (several closed-source prop AMMs have no public site or
repository). The link text shows what the GitHub link points to: `owner/repo` is a repository, which
is the program source when that is public and otherwise an SDK, IDL, docs or audit repository, and a
bare name is the developer's GitHub organisation or account. Links were checked on 2026-10-25 and can go stale;
they are also available as `github` / `website` per program in [`index.json`](index.json).

| Name | Program ID | Type | Description | Link |
|---|---|---|---|---|
| `raydium-amm-v4` | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | amm | Raydium AMM v4, constant-product AMM | [raydium-io/raydium-amm](https://github.com/raydium-io/raydium-amm) |
| `raydium-cpmm` | `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | amm | Raydium CPMM, constant-product market maker (new architecture) | [raydium-io/raydium-cp-swap](https://github.com/raydium-io/raydium-cp-swap) |
| `raydium-clmm` | `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | clmm | Raydium CLMM, tick-based concentrated liquidity | [raydium-io/raydium-clmm](https://github.com/raydium-io/raydium-clmm) |
| `raydium-launchlab` | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | bonding-curve | Raydium LaunchLab, token launchpad with bonding curve | [raydium-io/raydium-idl](https://github.com/raydium-io/raydium-idl) |
| `orca-whirlpool` | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | clmm | Orca Whirlpools, concentrated liquidity AMM | [orca-so/whirlpools](https://github.com/orca-so/whirlpools) |
| `meteora-dlmm` | `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | dlmm | Meteora DLMM, bin-based dynamic liquidity market maker | [MeteoraAg/dlmm-sdk](https://github.com/MeteoraAg/dlmm-sdk) |
| `meteora-damm-v2` | `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG` | amm | Meteora DAMM v2 (Dynamic AMM v2), constant product with optional price range and dynamic fees | [MeteoraAg/damm-v2](https://github.com/MeteoraAg/damm-v2) |
| `meteora-damm-v1` | `Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB` | amm | Meteora Dynamic AMM v1 (legacy) | [MeteoraAg/damm-v1-sdk](https://github.com/MeteoraAg/damm-v1-sdk) |
| `meteora-dbc` | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | bonding-curve | Meteora Dynamic Bonding Curve | [MeteoraAg/dynamic-bonding-curve](https://github.com/MeteoraAg/dynamic-bonding-curve) |
| `phoenix` | `PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY` | clob | Phoenix, on-chain central limit order book | [Ellipsis-Labs/phoenix-v1](https://github.com/Ellipsis-Labs/phoenix-v1) |
| `openbook-v2` | `opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb` | clob | OpenBook v2, on-chain central limit order book | [openbook-dex/openbook-v2](https://github.com/openbook-dex/openbook-v2) |
| `manifest` | `MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms` | clob | Manifest, on-chain order book | [Bonasa-Tech/manifest](https://github.com/Bonasa-Tech/manifest) |
| `serum-dex-v3` | `srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX` | clob | Serum DEX v3 (legacy order book, still referenced by Raydium AMM v4 pools) | [project-serum/serum-dex](https://github.com/project-serum/serum-dex) |
| `saber` | `SSwpkEEcbUqx4vtoEByFjSkhKdCT862DNVb52nZg1UZ` | stable-swap | Saber StableSwap | [saber-hq/stable-swap](https://github.com/saber-hq/stable-swap) |
| `saber-decimals` | `DecZY86MU5Gj7kppfUCEmd4LbXXuyZH1yHaP2NTqdiZB` | stable-swap | Saber Decimals, decimal-wrapper pools on top of StableSwap | [saber-hq/saber-periphery](https://github.com/saber-hq/saber-periphery) |
| `byreal` | `REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2` | clmm | Byreal CLMM (Raydium CLMM fork) | [byreal-git/byreal-clmm](https://github.com/byreal-git/byreal-clmm) |
| `crema` | `CLMM9tUoggJu2wagPkkqs9eFG4BWhVBZWkP1qv3Sp7tR` | clmm | Crema Finance CLMM | [CremaFinance](https://github.com/CremaFinance) |
| `invariant` | `HyaB3W9q6XdA5xwpU4XnSZV94htfmbmqJXZcEbRaJutt` | clmm | Invariant concentrated liquidity AMM | [invariant-labs/protocol](https://github.com/invariant-labs/protocol) |
| `saros-dlmm` | `1qbkdrr3z4ryLA7pZykqxvxWPoeifcVKo6ZG9CfkvVE` | dlmm | Saros DLMM, bin-based liquidity | [saros-xyz/saros-dlmm-sdk-rs](https://github.com/saros-xyz/saros-dlmm-sdk-rs) |
| `saros-amm` | `SSwapUtytfBdBn1b9NUGG6foMVPtcWgpRU32HToDUZr` | amm | Saros AMM (SPL token-swap fork) | [saros-xyz/saros-swap](https://github.com/saros-xyz/saros-swap) |
| `perena` | `NUMERUNsFCP3kuNmWZuXtm1AaQCPj9uw6Guv2Ekoi5P` | stable-swap | Perena Numeraire, stable-swap AMM | [quineco/numeraire-sdk](https://github.com/quineco/numeraire-sdk) |
| `metadao-futarchy` | `FUTARELBfJfQ8RDGhg1wdhddq1odMAJUePHFuBYfUxKq` | amm | MetaDAO futarchy, conditional-market AMM | [metaDAOproject/programs](https://github.com/metaDAOproject/programs) |
| `moonit` | `MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG` | bonding-curve | Moonit bonding-curve launchpad | [gomoonit/moonit-sdk](https://github.com/gomoonit/moonit-sdk) |
| `omnipair` | `omnixgS8fnqHfCcTGKWj6JtKjzpJZ1Y5y9pyFkQDkYE` | amm | Omnipair, constant-product AMM combined with lending | [omnipair/omnipair-rs](https://github.com/omnipair/omnipair-rs) |
| `sanctum-lst-swap` | `stkitrT1Uoy18Dk1fTrgPw8W6MVzoCfYoAFT4MLsmhq` | lst-swap | Sanctum Router, cross-LST swap router (CPI wrapper over Sanctum pools) | [igneous-labs/sanctum-router-sdk](https://github.com/igneous-labs/sanctum-router-sdk) |
| `sanctum-infinity` | `5ocnV1qiCgaQR8Jb8xWnVbApfaygJ8tNoZfgPwsgx9kx` | lst-swap | Sanctum Infinity, multi-LST liquidity pool | [igneous-labs/inf-1.5](https://github.com/igneous-labs/inf-1.5) |
| `pump-fun` | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | bonding-curve | Pump.fun bonding-curve launchpad | [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) |
| `pump-swap` | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | amm | PumpSwap, constant-product AMM for graduated Pump.fun tokens | [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) |
| `boop-fun` | `boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4` | bonding-curve | Boop.fun bonding-curve launchpad | [Boop-fun/idls](https://github.com/Boop-fun/idls) |
| `virtuals` | `5U3EU2ubXtK84QcRjWVmYt9RaDyA8gKxdUrPFXmZyaki` | bonding-curve | Virtuals buy/sell program | [Virtual-Protocol](https://github.com/Virtual-Protocol) |
| `lifinity-v2` | `2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c` | pmm | Lifinity v2, oracle-driven proactive market maker | [Lifinity-Labs](https://github.com/Lifinity-Labs) |
| `lifinity-v1` | `EewxydAPCCVuNEyrVN68PuSYdQ7wKn27V9Gjeoi8dy3S` | pmm | Lifinity v1 (legacy, superseded by v2) | [Lifinity-Labs](https://github.com/Lifinity-Labs) |
| `woofi` | `WooFif76YGRNjk1pA8wCsN67aQsD9f9iLsz4NcJ1AVb` | pmm | WOOFi, oracle-priced PMM | [woonetwork/woofi-jupiter-integration](https://github.com/woonetwork/woofi-jupiter-integration) |
| `humidifi` | `9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp` | pmm | HumidiFi, closed-source oracle-priced prop AMM | [humidifi.xyz](https://www.humidifi.xyz/) |
| `spl-token-swap` | `SwaPpA9LAaLfeLi3a68M4DjnLqgtticKg6CnyNwgAC8` | amm | SPL Token Swap reference program | [solana-labs/solana-program-library](https://github.com/solana-labs/solana-program-library/tree/master/token-swap) |
| `orca-v1` | `DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1` | amm | Orca v1 (SPL token-swap based) | [orca-so](https://github.com/orca-so) |
| `orca-v2` | `9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP` | amm | Orca v2 (SPL token-swap based) | [orca-so](https://github.com/orca-so) |
| `stepn` | `Dooar9JkhdZ7J3LHN3A7YCuoGRUggXhQaG4kijfLGU2j` | amm | STEPN swap (SPL token-swap fork) | [stepn.com](https://www.stepn.com/) |
| `penguin` | `PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP` | amm | Penguin Finance swap (SPL token-swap fork) | [pngfi/typescript-sdk](https://github.com/pngfi/typescript-sdk) |
| `guacswap` | `Gswppe6ERWKpUTXvRPfXdzHhiCyJvLadVvXGfdpBqcE1` | amm | Guacswap (SPL token-swap fork) | [Guacgg](https://github.com/Guacgg) |
| `fluxbeam` | `FLUXubRmkEi2q6K3Y9kBPg9248ggaZVsoSFhtJHSrm1X` | amm | FluxBeam (SPL token-swap fork) | [fluxbeam.xyz](https://fluxbeam.xyz/) |
| `dexlab` | `DSwpgjMvXhtGn6BsbqmacdBZyfLj6jSWf3HJpdJtmg6N` | amm | DexLab swap (SPL token-swap fork) | [dexlab-protocol/dexlab-token-swap](https://github.com/dexlab-protocol/dexlab-token-swap) |
| `aldrin-v1` | `AMM55ShdkoGRB5jVYPjWziwk8m5MpwyDgsMWHaMSQWH6` | amm | Aldrin AMM v1 (SPL token-swap fork) | [aldrin-labs](https://github.com/aldrin-labs) |
| `aldrin-v2` | `CURVGoZn8zycx6FXwwevgBTB2gVvdbGTEpvMJDbgs2t4` | amm | Aldrin AMM v2 (SPL token-swap fork) | [aldrin-labs](https://github.com/aldrin-labs) |
| `mercurial` | `MERLuDFBMmsHnsBPZw2sDQZHvXFMwp8EdjudcU2HKky` | stable-swap | Mercurial multi-token stable-swap (merged into Meteora) | [mercurial-finance/stable-swap-n-pool-instructions](https://github.com/mercurial-finance/stable-swap-n-pool-instructions) |
| `bonkswap` | `BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p` | amm | BonkSwap (SPL token-swap fork) | [bonkswap.io](https://www.bonkswap.io/) |
| `cropper` | `H8W3ctz92svYg6mkn1UtGfu2aQr2fnUFHM1RhScEtQDt` | amm | Cropper Finance (SPL token-swap fork) | [CropperFinance/cropper_instructions](https://github.com/CropperFinance/cropper_instructions) |
| `pancakeswap` | `HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq` | clmm | PancakeSwap on Solana (CLMM fork) | [pancakeswap](https://github.com/pancakeswap) |
| `goosefx-gamma` | `GAMMA7meSFWaBXF25oSUgmGRwaW6sCMFLmBNiMSdbHVT` | amm | GooseFX GAMMA AMM | [GooseFX1/gamma-swap](https://github.com/GooseFX1/gamma-swap) |
| `stabble-stable` | `swapNyd8XiQwJ6ianp9snpu4brUqFxadzvHebnAXjJZ` | stable-swap | Stabble Stable Swap | [stabbleorg/amm_v1](https://github.com/stabbleorg/amm_v1) |
| `stabble-weighted` | `swapFpHZwjELNnjvThjajtiVmkz3yPQEHjLtka2fwHW` | amm | Stabble Weighted Swap, Balancer-style weighted pools | [stabbleorg/amm_v1](https://github.com/stabbleorg/amm_v1) |
| `stabble-clmm` | `6dMXqGZ3ga2dikrYS9ovDXgHGh5RUsb2RTUj6hrQXhk6` | clmm | Stabble CLMM | [stabbleorg/clmm](https://github.com/stabbleorg/clmm) |
| `drift-v2` | `dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH` | perp | Drift v2, perpetuals and spot DEX with AMM-backed liquidity (GitHub and docs now under the Velocity Exchange name) | [velocity-exchange/protocol-v2](https://github.com/velocity-exchange/protocol-v2) |
| `jupiter-perps` | `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu` | perp | Jupiter Perpetuals, LP-pool backed perps | [jup-ag](https://github.com/jup-ag) |
| `solfi` | `SoLFiHG9TfgtdUXUjWAxi3LtvYuFyDLVhBWxdMZxyCe` | pmm | SolFi v1, proprietary oracle-priced AMM by Ellipsis Labs | [Ellipsis-Labs](https://github.com/Ellipsis-Labs) |
| `solfi-v2` | `SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF` | pmm | SolFi v2, proprietary oracle-priced AMM by Ellipsis Labs | [Ellipsis-Labs](https://github.com/Ellipsis-Labs) |
| `tessera-v` | `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | pmm | TesseraV, proprietary AMM by Wintermute |  |
| `goonfi` | `goonERTdGsjnkZqWuVjs73BZ3Pb9qoCUdBUL17BnS5j` | pmm | GoonFi v1, proprietary AMM |  |
| `goonfi-v2` | `goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE` | pmm | GoonFi v2, proprietary AMM |  |
| `zerofi` | `ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY` | pmm | ZeroFi, proprietary AMM |  |
| `bisonfi` | `BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi` | pmm | BisonFi, proprietary AMM |  |
| `obric-v2` | `obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y` | pmm | Obric v2, proprietary AMM | [obric.xyz](https://obric.xyz/) |
| `aquifer` | `AQU1FRd7papthgdrwPTTq5JacJh8YtwEXaBfKU3bTz45` | pmm | Aquifer, proprietary AMM | [aquiferdex.io](https://aquiferdex.io/) |
| `alphaq` | `ALPHAQmeA7bjrVuccPsYPiCvsi428SNwte66Srvs4pHA` | pmm | AlphaQ, proprietary AMM | [alphaq.xyz](https://www.alphaq.xyz/) |
| `quantum` | `QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv` | pmm | Quantum, proprietary AMM |  |
| `scorch` | `ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` | pmm | Scorch, proprietary AMM |  |
| `obsidian` | `HBVw6bZtcCaezhcBrmfyXBSBRWCdv72271xQ4GPvms2z` | pmm | Obsidian, proprietary AMM |  |
| `heaven` | `HEAVENoP2qxoeuF8Dj2oT1GHEnu49U5mJYkdeC8BAX2o` | pmm | Heaven, proprietary AMM | [heavenxyz](https://github.com/heavenxyz) |
| `taurusfi` | `9VX8EKBg6vM6tA68xaDsPkbrx26XConZjkQmhVApUptc` | pmm | TaurusFi, proprietary AMM |  |
| `gavel` | `srAMMzfVHVAtgSJc8iH6CfKzuWuUTzLHVCE81QU1rgi` | amm | Gavel (Plasma), sandwich-resistant AMM by Ellipsis Labs, source available (BUSL-1.1) | [Ellipsis-Labs/plasma](https://github.com/Ellipsis-Labs/plasma) |
| `whalestreet` | `FW6zUqn4iKRaeopwwhwsquTY6ABWLLgjxtrC3VPnaWBf` | pmm | WhaleStreet, proprietary AMM |  |
| `marco-polo` | `9tKE7Mbmj4mxDjWatikzGAtkoWosiiZX9y6J4Hfm2R8H` | pmm | Marco Polo, proprietary AMM |  |

## Notes and limitations

- The binaries are public on-chain data and are provided for research, auditing and version
  checking. Their copyright stays with the respective authors. If you are the author of a program and
  want it removed, open an issue.
- Nothing here is a statement about who controls a program; that is only what the chain says
  (`upgrade_authority`).
- Programs can be closed or become immutable; a program that disappears shows up as a failure in
  the logs, and its published versions stay available.
- Only programs owned by the upgradeable loader and the non-upgradeable `BPFLoader2` have been
  exercised so far. Programs owned by other loaders (for example loader v4) are untested.
- The collector sees one snapshot per day. If a program is upgraded twice between two runs, only the
  last deployment is recorded. History starts with the first run of this repository.
