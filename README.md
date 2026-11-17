# PDF Time Parser (ATOSS)

## Install

- Option A: Nix + direnv (recommended)
  - Install `nix` and `direnv`, then in this repo:
    - `direnv allow` (or `nix develop` without direnv)
    - A Python venv `.venv` is created and `requirements.txt` installed automatically.

- Option B: Plain Python
  - Python 3.10+
  - `pip install -r requirements.txt`

## Usage

Create a fresh review report directly from the two PDFs:

```
python cli.py merge atoss_aug.pdf Time-Recorder2.1_Timeproof_08.2025.pdf --issues august_25_issues.csv
```

This parses both exports, merges and validates the entries, and writes `august_25.txt` (month and year inferred as `<month>_<yy>` in lowercase).

After editing the report text file, re-validate it and rebuild the summary table:

```
python cli.py verify august_25.txt --issues august_25_verified_issues.csv
```

This overwrites `august_25.txt` with the condensed verified view and optionally writes an updated issues CSV. Re-run `merge` if you ever need to regenerate the editable version.

Pass `-o/--out` to either command if you want to override the default `<month>_<yy>.txt` location.

### Generate Printable PDF Overview

Generate an ink-saving, clean printable PDF overview (for easy ATOSS manual entry):

```bash
# Single month overview
python cli.py pdf export_may_26.txt

# Multiple months compiled into one PDF
python cli.py pdf export_may_26.txt export_june_26.txt export_july_26.txt -o export_may_june_july_2026.pdf
```

Notes:
- Location: maps token `ma` (case-insensitive, whole word) to `remote`; otherwise `office`.
- App PDF: defaults to `remote`; lines containing `Norm` switch to `office` and `Mobil` confirm `remote`.
- Times are detected per line with the date; multiple start/end pairs per day are supported.
- Reports include an issue summary ahead of the daily sections for quick scanning.
- Weekdays in the month without any entries are listed so you can fill in holidays or missed logs explicitly.

## Webapp (NixOS + Caddy + Authelia)

`webapp.py` is a small stdlib-only web UI around the CLI pipeline, meant to run on a
small VPS (1 vCPU / 1 GB is plenty). It binds to `127.0.0.1` only and rejects requests
without a `Remote-User` header, so it must sit behind Caddy with Authelia forward auth.

Since the TimeRecorder app export is no longer available on Android, the app PDF is
optional everywhere: upload just the ATOSS PDF and type your entries directly into the
report in the browser, then Merge → edit → Verify → download TXT/PDF.

### Server setup

Clone this repo to a stable path on the VPS (e.g. `/srv/atoss`) and import the module:

```nix
# configuration.nix
{
  imports = [ (import /srv/atoss/module.nix { src = /srv/atoss; }) ];

  services.atoss-web = {
    enable = true;
    # defaults: port 8088, dataDir /var/lib/atoss-web
    caddy = {
      enable = true;                  # only if Caddy itself is managed by NixOS
      domain = "times.example.com";
      autheliaAddress = "127.0.0.1:9091";
      autheliaLoginDomain = "auth.example.com";
    };
  };
}
```

If Caddy runs outside NixOS (or you manage its config by hand), add this site block instead:

```caddy
times.example.com {
    forward_auth 127.0.0.1:9091 {
        uri /api/verify?rd=https://auth.example.com/
        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }
    reverse_proxy 127.0.0.1:8088
}
```

### Notes

- Server runtime deps come from nixpkgs: python3 + pdfminer-six, plus a `typst` binary and
  the Inter font for PDF rendering. The service runs as a dedicated user with
  `ProtectSystem=strict`; only `/var/lib/atoss-web` is writable (back that up).
- PDF rendering calls `typst` directly via `ATOSS_TYPST_BIN` / `ATOSS_TYPST_FONT_PATH`
  (set by the module). Without them it falls back to `nix shell nixpkgs#typst` for
  local use; the dev shell now also puts `typst` and Inter on PATH.
- Per-user data lives in `/var/lib/atoss-web/<Remote-User>/` (`work/` + `reports/`).
- Upload limit is 25 MB per request (`ATOSS_MAX_BODY`).
- Only port 443 should be exposed; the app port is loopback-only.
