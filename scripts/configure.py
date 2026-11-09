"""Generate local secrets without printing them or overwriting an existing .env."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env уже существует: оставлен без изменений.")
password = secrets.token_hex(24)
content = (root / ".env.example").read_text()
content = content.replace("replace-with-a-random-password", password)
content = content.replace("replace-with-a-random-api-key", secrets.token_hex(32))
with target.open("x") as stream:
    stream.write(content)
target.chmod(0o600)
print(
    "Создан .env со случайными ключами. Добавьте BOT_TOKEN и запустите docker compose up -d --build"
)
