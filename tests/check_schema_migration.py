#!/usr/bin/env python3
"""Schema-Migrations-Guard für die HA-Add-ons.

Vergleicht das `schema:`/`options:` eines Add-ons gegen ein Baseline-Release
und schlägt fehl, wenn ein Update bestehender Installationen brechen würde.

Hintergrund: Der Supervisor validiert beim Add-on-Update die *gespeicherten*
Optionen gegen das *neue* Schema. Wird ein Pflichtfeld ohne Default ergänzt,
fehlt es in den Altdaten → Update/Start schlägt fehl (genau der Recovery-400-
Fehlertyp, nur auf der Schema-Seite).

Geprüfte Regeln (ERROR = Exit 1, WARN = nur Hinweis):
  * ERROR: neues Pflichtfeld (top-level) ohne Default in `options:`
  * ERROR: bestehendes optionales Feld wird zum Pflichtfeld (ohne Default)
  * ERROR: neues Pflicht-Feld in einem Listen-Element (z. B. backup_sources)
           ohne `?` — Altdaten enthalten es nicht
  * ERROR: Typwechsel eines bestehenden Felds (z. B. str → int)
  * WARN:  Option entfernt, war aber im alten Default → verwaiste Altdaten

Aufruf:
  # CI: gegen das letzte Release-Tag des Add-ons
  check_schema_migration.py --git

  # explizit zwei config.yaml vergleichen (Tests)
  check_schema_migration.py --base OLD/config.yaml --new NEW/config.yaml
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

# (Verzeichnis, Tag-Präfix) je Add-on
ADDONS = [
    ("offsite-backup", "offsite-v"),
    ("backuppc-recovery", "recovery-v"),
]


def _is_optional(schema_val) -> bool:
    """True, wenn das Schema-Feld optional ist (HA: trailing '?')."""
    return isinstance(schema_val, str) and schema_val.rstrip().endswith("?")


def _base_type(schema_val):
    """Normalisierter Typ-Bezeichner ohne '?' und ohne Range-Klammern."""
    if isinstance(schema_val, list):
        return "list"
    if isinstance(schema_val, dict):
        return "dict"
    s = str(schema_val).strip().rstrip("?")
    return s.split("(", 1)[0]  # 'str(0,255)' -> 'str'


def _list_element_schema(schema_val):
    """Das Element-Schema einer Listen-Option (erstes Listenelement)."""
    if isinstance(schema_val, list) and schema_val and isinstance(schema_val[0], dict):
        return schema_val[0]
    return {}


def _load(path_or_text) -> dict:
    return yaml.safe_load(path_or_text) or {}


def _git_show(ref: str, path: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", f"{ref}:{path}"], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return None


def _latest_tag(prefix: str) -> str | None:
    try:
        tags = subprocess.check_output(
            ["git", "tag", "--list", f"{prefix}*", "--sort=-version:refname"],
            text=True,
        ).split()
        return tags[0] if tags else None
    except subprocess.CalledProcessError:
        return None


def compare(old_cfg: dict, new_cfg: dict) -> tuple[list[str], list[str]]:
    """Gibt (errors, warnings) zurück."""
    errors: list[str] = []
    warnings: list[str] = []

    old_schema = old_cfg.get("schema", {}) or {}
    new_schema = new_cfg.get("schema", {}) or {}
    new_opts = new_cfg.get("options", {}) or {}
    old_opts = old_cfg.get("options", {}) or {}

    for key, new_val in new_schema.items():
        new_type = _base_type(new_val)
        new_optional = _is_optional(new_val)

        if key not in old_schema:
            # Neue Option
            if new_type == "list":
                # Listen-Default (leere Liste) erfüllt das Schema → ok auf
                # Top-Level. Element-Pflichtfelder werden unten geprüft.
                continue
            if not new_optional and key not in new_opts:
                errors.append(
                    f"Neues Pflichtfeld '{key}' ({new_val}) ohne Default in options: "
                    f"— bestehende Installs haben es nicht, Update bricht."
                )
            continue

        # Bestehende Option
        old_val = old_schema[key]
        old_type = _base_type(old_val)
        if old_type != new_type:
            errors.append(
                f"Typwechsel bei '{key}': '{old_val}' → '{new_val}' "
                f"— gespeicherte Altwerte validieren ggf. nicht mehr."
            )
        elif _is_optional(old_val) and not new_optional and key not in new_opts:
            errors.append(
                f"'{key}' war optional ({old_val}) und ist jetzt Pflicht ({new_val}) "
                f"ohne Default — bestehende leere Installs brechen."
            )

        # Listen-Element-Felder vergleichen (z. B. backup_sources)
        if new_type == "list":
            old_el = _list_element_schema(old_val)
            new_el = _list_element_schema(new_val)
            for ek, ev in new_el.items():
                if ek not in old_el and not _is_optional(ev):
                    errors.append(
                        f"Neues Pflicht-Element-Feld '{key}[].{ek}' ({ev}) ohne '?' "
                        f"— gespeicherte Listen-Einträge enthalten es nicht, Update bricht."
                    )

    # Entfernte Optionen
    for key in old_schema:
        if key not in new_schema and key in old_opts:
            warnings.append(
                f"Option '{key}' entfernt — gespeicherte Altdaten verwaisen (meist harmlos)."
            )

    return errors, warnings


def check_addon(addon_dir: str, base_ref: str | None, repo_root: Path) -> int:
    cfg_path = repo_root / addon_dir / "config.yaml"
    new_cfg = _load(cfg_path.read_text())

    if base_ref is None:
        print(f"  [{addon_dir}] kein Baseline-Tag gefunden — übersprungen "
              f"(erstes Release oder Tags fehlen).")
        return 0

    old_text = _git_show(base_ref, f"{addon_dir}/config.yaml")
    if old_text is None:
        print(f"  [{addon_dir}] config.yaml in {base_ref} nicht gefunden — übersprungen.")
        return 0

    old_cfg = _load(old_text)
    errors, warnings = compare(old_cfg, new_cfg)

    old_v = old_cfg.get("version", "?")
    new_v = new_cfg.get("version", "?")
    print(f"  [{addon_dir}] {old_v} ({base_ref}) → {new_v}")
    for w in warnings:
        print(f"    WARN: {w}")
    for e in errors:
        print(f"    ERROR: {e}")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--git", action="store_true",
                    help="Baseline aus dem letzten Release-Tag je Add-on ziehen.")
    ap.add_argument("--base", help="Pfad zu alter config.yaml (Test-Modus).")
    ap.add_argument("--new", help="Pfad zu neuer config.yaml (Test-Modus).")
    args = ap.parse_args()

    if args.base and args.new:
        errors, warnings = compare(_load(Path(args.base).read_text()),
                                   _load(Path(args.new).read_text()))
        for w in warnings:
            print(f"WARN: {w}")
        for e in errors:
            print(f"ERROR: {e}")
        if errors:
            print(f"\nFEHLGESCHLAGEN: {len(errors)} migrationsbrechende Änderung(en).")
            return 1
        print("OK: keine migrationsbrechenden Schema-Änderungen.")
        return 0

    repo_root = Path(__file__).resolve().parent.parent
    print("Schema-Migrations-Guard:")
    rc = 0
    for addon_dir, prefix in ADDONS:
        base_ref = _latest_tag(prefix) if args.git else None
        rc |= check_addon(addon_dir, base_ref, repo_root)

    if rc:
        print("\nFEHLGESCHLAGEN: migrationsbrechende Schema-Änderung(en) gefunden.")
    else:
        print("\nOK: keine migrationsbrechenden Schema-Änderungen.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
