# subbox-slskd

Standalone [slskd](https://github.com/slskd/slskd) (Soulseek) tooling for
[Subbox](https://github.com/laker-93). These scripts let you run a local slskd
instance and auto-download the tracks on your Subbox **wishlist** that you don't
already own — filling gaps in your library over Soulseek.

They were extracted from the `pymix` backend repo so they can be handed to end
users without the rest of the platform. They talk to Subbox/pymix, Navidrome and
slskd purely over their HTTP APIs.

## Scripts

| Script | What it does |
|---|---|
| `scripts/run-slskd-macos.sh` | Install (if needed) and run slskd on macOS. Detects Apple Silicon vs Intel from `uname -m` (override with `SLSKD_ARCH_TAG`). Prompts once for Soulseek credentials + download/share dirs, caches them, verifies the login, and prints the exact `download_wishlist.py` command to run. |
| `scripts/run-slskd-windows.ps1` | The same install-and-run flow for Windows (x64). |
| `scripts/download_wishlist.py` | Reads your Subbox wishlist, checks what's already in your Navidrome library, and downloads the missing tracks through slskd. Standard-library only — runs with a bare `python3`, no `pip install`. |

## Quick start

```bash
# 1. Start slskd (first run downloads it; caches your creds afterwards)
./scripts/run-slskd-macos.sh

# 2. In another terminal, download your missing wishlist tracks.
#    The run script above prints this command pre-filled with --slskd-url /
#    --slskd-username. Add your Subbox --username / --password:
python3 scripts/download_wishlist.py \
    --slskd-url http://127.0.0.1:5030 \
    --slskd-username <soulseek-user> --slskd-password <soulseek-pass> \
    --username <subbox-user> --password <subbox-pass>
```

Run `python3 scripts/download_wishlist.py --help` for the full flag set (pymix
API URL, Navidrome URL, dry-run, match thresholds, etc.).

## How it fits together

1. **pymix wishlist API** — what you *want*.
2. **Navidrome** (Subsonic API) — what you already *have*.
3. **slskd** — the Soulseek client used to fetch the gap.

`download_wishlist.py` searches Navidrome per wishlist item, downloads only the
missing ones via slskd (into your Subbox watch dir, so Subbox's importer ingests
them), and flips each item to `downloaded` via the pymix API. pymix's own
reconcile loop later promotes `downloaded` → `available` once the file is
imported.

## Credentials

The run scripts cache your Soulseek/slskd login in
`scripts/slskd-credentials.env` (mode `600`, git-ignored). Delete that file to
re-enter; it's auto-removed if a login is rejected. **Never commit it.**
