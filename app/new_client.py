"""Scaffold a new client bot: `python run.py new-client "Client Name"`.

Each client gets its own isolated config under clients/<slug>/:
  - <slug>.env      → its Telegram token, persona, Stripe key, paths
  - catalog.json    → its products
  - data/           → its own database (leads, orders, inventory)
  - salesbot-<slug>.service → ready-to-install systemd unit

All client bots share the ONE Ollama model on the droplet — personality comes
from each client's persona + catalog, not from retraining the model.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "client"


def _build_env(name: str, slug: str) -> str:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in example.splitlines():
        if line.startswith("CATALOG_PATH="):
            line = f"CATALOG_PATH=clients/{slug}/catalog.json"
        elif line.startswith("DATABASE_PATH="):
            line = f"DATABASE_PATH=clients/{slug}/data/leads.db"
        elif line.startswith("BUSINESS_NAME="):
            line = f"BUSINESS_NAME={name}"
        elif line.startswith("PERSONA_NAME="):
            line = "PERSONA_NAME="  # fill in this client's persona name
        lines.append(line)
    header = (
        f"# ══════════════════════════════════════════════════════════════\n"
        f"#  Client: {name}\n"
        f"#  Fill in: TELEGRAM_BOT_TOKEN, SELLER_CHAT_IDS, OLLAMA_MODEL,\n"
        f"#           PERSONA_NAME/PERSONA_STYLE, STRIPE_SECRET_KEY (optional)\n"
        f"# ══════════════════════════════════════════════════════════════\n"
    )
    return header + "\n".join(lines) + "\n"


def _service_unit(name: str, slug: str) -> str:
    py = ROOT / ".venv" / "bin" / "python"
    env = ROOT / "clients" / slug / f"{slug}.env"
    return f"""[Unit]
Description=Sales Bot — {name}
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=CHANGE_ME
WorkingDirectory={ROOT}
ExecStart={py} {ROOT}/run.py --env {env}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: python run.py new-client "Client Name"')
        return 2
    name = " ".join(argv).strip()
    slug = slugify(name)
    base = ROOT / "clients" / slug

    if base.exists():
        print(f"✋ clients/{slug} already exists — pick another name or edit it.")
        return 2

    (base / "data").mkdir(parents=True)

    # Per-client catalog (start from the template).
    template = ROOT / "catalog.json"
    catalog = template.read_text(encoding="utf-8") if template.exists() else "{}"
    (base / "catalog.json").write_text(catalog, encoding="utf-8")

    # Per-client env.
    (base / f"{slug}.env").write_text(_build_env(name, slug), encoding="utf-8")

    # Ready-to-install systemd unit.
    (base / f"salesbot-{slug}.service").write_text(
        _service_unit(name, slug), encoding="utf-8"
    )

    rel = f"clients/{slug}"
    print(f"""
✅ Created {rel}/ for "{name}"

   {rel}/{slug}.env               ← its token, persona, Stripe key, paths
   {rel}/catalog.json            ← its products
   {rel}/data/                   ← its own database
   {rel}/salesbot-{slug}.service ← its 24/7 service

Next:
  1. Edit its config:
       nano {rel}/{slug}.env        (Telegram token, OLLAMA_MODEL, persona…)
       nano {rel}/catalog.json      (this client's products)
  2. Load its inventory (if selling):
       python run.py --env {rel}/{slug}.env stock <product_id> keys.txt
  3. Check its connections:
       python run.py --env {rel}/{slug}.env --check
  4. Run it 24/7 (edit User= first):
       sudo cp {rel}/salesbot-{slug}.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now salesbot-{slug}
       journalctl -u salesbot-{slug} -f

Personality is set by PERSONA_NAME / PERSONA_STYLE + catalog.json in this
client's config — all clients share the same Ollama model. No retraining.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
