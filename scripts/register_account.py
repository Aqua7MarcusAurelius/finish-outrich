"""CLI-обёртка над `/auth/start` + `/auth/code` для одношагового добавления
аккаунта без UI. Аналог мастера «Новый аккаунт», но через терминал.

Использование:

    python scripts/register_account.py \
        --base-url http://localhost:8000 \
        --profile-name "SAD_11_RU" \
        --phone "+79..." \
        --proxy-primary  "158.46.145.227:63795:user:pass" \
        --proxy-fallback "172.121.119.159:64561:user:pass" \
        --api-id 12345678 \
        --api-hash "abcd...32 hex..." \
        --device-model "Samsung Galaxy S23" \
        --system-version "Android 14" \
        --app-version "Telegram Android 10.5.4" \
        --lang-code ru \
        --system-lang-code ru \
        --password "опц-2fa"
    Enter SMS code: 12345
    ✓ Account SAD_11_RU registered (id=23)

Все 8 fingerprint-полей обязательны. `--password` опционален: если 2FA
запросится после кода, бэк подставит этот пароль автоматически
(тот же путь что в UI). Если опустить и 2FA включена — скрипт
дополнительно попросит пароль на отдельном prompt.

Прокси-форматы: `host:port:user:pass` или полная URL-форма
`socks5://user:pass@host:port` (та же, что в UI-модалке).
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from typing import Any

import httpx


PROXY_COLON_RX = re.compile(r"^([^:\s]+):(\d+):([^:\s]+):(.+)$")


def proxy_to_url(value: str) -> str:
    v = value.strip()
    if not v:
        raise ValueError("прокси не задан")
    if v.startswith("socks5://"):
        return v
    m = PROXY_COLON_RX.match(v)
    if not m:
        raise ValueError(
            f"неверный формат прокси: {value!r} — "
            "ожидается host:port:user:pass или socks5://user:pass@host:port"
        )
    host, port, user, pwd = m.groups()
    return f"socks5://{user}:{pwd}@{host}:{port}"


def die(message: str, code: int = 1) -> None:
    print(f"✗ {message}", file=sys.stderr)
    sys.exit(code)


def parse_response(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        die(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return {}  # unreachable
    if resp.status_code >= 400:
        err = (data or {}).get("error", {})
        code = err.get("code", "UNKNOWN")
        msg = err.get("message", "")
        die(f"{code}: {msg}  (HTTP {resp.status_code})")
    return data


def main() -> None:
    p = argparse.ArgumentParser(
        description="Зарегистрировать Telegram-аккаунт через /auth/* за один проход.",
    )
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="URL FastAPI-приложения (default: %(default)s)")
    p.add_argument("--profile-name", required=True,
                   help="Имя аккаунта в нашей системе (accounts.name)")
    p.add_argument("--phone", required=True, help="Телефон в формате +7...")
    p.add_argument("--proxy-primary", required=True,
                   help="Primary SOCKS5 (host:port:user:pass или URL)")
    p.add_argument("--proxy-fallback", required=True,
                   help="Fallback SOCKS5 (host:port:user:pass или URL)")
    p.add_argument("--api-id", type=int, required=True,
                   help="api_id, полученный на my.telegram.org")
    p.add_argument("--api-hash", required=True,
                   help="api_hash, полученный на my.telegram.org")
    p.add_argument("--device-model", required=True,
                   help='напр. "Samsung Galaxy S23"')
    p.add_argument("--system-version", required=True,
                   help='напр. "Android 14"')
    p.add_argument("--app-version", required=True,
                   help='напр. "Telegram Android 10.5.4"')
    p.add_argument("--lang-code", default="ru",
                   help="lang_code (default: %(default)s)")
    p.add_argument("--system-lang-code", default="ru",
                   help="system_lang_code (default: %(default)s)")
    p.add_argument("--lang-pack", default="",
                   help="lang_pack — обычно пусто")
    p.add_argument("--password", default=None,
                   help="2FA-пароль (опц.); если 2FA включён и не задан — спросим интерактивно")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="HTTP-таймаут к нашему API в секундах")
    args = p.parse_args()

    try:
        primary_url = proxy_to_url(args.proxy_primary)
        fallback_url = proxy_to_url(args.proxy_fallback)
    except ValueError as e:
        die(str(e))
        return  # unreachable

    payload = {
        "phone": args.phone.strip(),
        "name": args.profile_name.strip(),
        "proxy_primary": primary_url,
        "proxy_fallback": fallback_url,
        "api_id": int(args.api_id),
        "api_hash": args.api_hash.strip(),
        "device_model": args.device_model.strip(),
        "system_version": args.system_version.strip(),
        "app_version": args.app_version.strip(),
        "lang_code": args.lang_code.strip() or "ru",
        "system_lang_code": args.system_lang_code.strip() or "ru",
        "lang_pack": (args.lang_pack or "").strip(),
        "password": args.password if args.password else None,
    }

    base = args.base_url.rstrip("/")
    timeout = args.timeout

    with httpx.Client(base_url=base, timeout=timeout) as client:
        print(f"→ POST /auth/start  (phone={payload['phone']}, name={payload['name']})")
        resp = client.post("/auth/start", json=payload)
        data = parse_response(resp)
        session_id = data["session_id"]
        print(f"  session_id={session_id}, phase={data['status']}")
        print(f"  Telegram прислал код на {payload['phone']}.")

        code = input("Enter SMS code: ").strip()
        if not code:
            die("пустой код — отмена")

        print(f"→ POST /auth/code")
        resp = client.post("/auth/code", json={"session_id": session_id, "code": code})
        data = parse_response(resp)

        if data.get("status") == "2fa_required":
            # Пароль не был передан в /auth/start (или backend всё равно
            # запросил 2FA) — спросим интерактивно.
            pwd = args.password or getpass.getpass("Enter 2FA password: ")
            if not pwd:
                die("пустой 2FA-пароль — отмена")
            print(f"→ POST /auth/2fa")
            resp = client.post("/auth/2fa", json={"session_id": session_id, "password": pwd})
            data = parse_response(resp)

        if data.get("status") != "done":
            die(f"непредвиденный статус: {data}")

        account_id = data.get("account_id")
        print(f"✓ Account {payload['name']} registered (id={account_id})")


if __name__ == "__main__":
    main()
