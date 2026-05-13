#!/usr/bin/env python3
"""
Песочница-разведчик для CRM API коллеги
(см. docs/INT_CRM_API_TOKEN_GUIDE.md).

Не часть основного приложения — одноразовый инструмент, чтобы понять
как устроена их API: какие поля у лида, что возвращает request-new,
что есть в preferences. Все запросы и ответы пишутся в
scripts/crm_probe.log одной строкой JSON на событие — чтобы можно
было вернуться и посмотреть «что мы делали» через час.

Запуск (внутри контейнера app, чтобы был httpx из requirements.txt):
    docker compose exec app python scripts/crm_probe.py <command>

Конфиг — через .env:
    CRM_BASE_URL=https://crm.example.com
    CRM_API_TOKEN=crm_a1b2c3d4...

Команды:
    preferences            — GET /api/l1/preferences
    chats                  — GET /api/l1/chats
    leads [--status X]     — GET /api/l1/leads (что уже привязано к нам)
    messages <username>    — GET /api/l1/chats/<username>/messages
    request-new [--count N]— POST /api/l1/leads/request-new ⚠ реально забирает!
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "crm_probe.log"
ENV_PATH = SCRIPT_DIR.parent / ".env"


def _load_env() -> None:
    """
    Очень простой .env loader: KEY=VALUE по строкам, без секций.
    Используется чтобы не тащить python-dotenv ради одного файла.
    Уже выставленные переменные среды не перезаписываем.
    """
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env()
BASE_URL = os.environ.get("CRM_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("CRM_API_TOKEN", "")


def _log(kind: str, **fields: Any) -> None:
    """Запись события в JSON-Lines файл. Токен сюда НЕ пишем."""
    payload = {"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _need_config() -> None:
    missing: list[str] = []
    if not BASE_URL:
        missing.append("CRM_BASE_URL")
    if not TOKEN:
        missing.append("CRM_API_TOKEN")
    if missing:
        print(f"В .env не заданы: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _call(method: str, path: str, **kwargs: Any) -> Any:
    """Один HTTP-вызов: вывод в stdout + запись в log файл."""
    _need_config()
    url = f"{BASE_URL}{path}"
    _log("request", method=method, url=url, body=kwargs.get("json"), params=kwargs.get("params"))
    print(f"\n>>> {method} {path}")
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(method, url, headers=_headers(), **kwargs)
    except httpx.HTTPError as e:
        print(f"!!! HTTP error: {e}", file=sys.stderr)
        _log("error", error=str(e))
        return None
    print(f"<<< HTTP {resp.status_code}")
    body: Any
    try:
        body = resp.json()
        print(_pretty(body))
    except Exception:
        body = resp.text
        print(body[:1000])
    _log("response", status=resp.status_code, body=body)
    return body


# ─── Команды ─────────────────────────────────────────────────────────

def cmd_preferences(_: argparse.Namespace) -> None:
    _call("GET", "/api/l1/preferences")


def cmd_chats(_: argparse.Namespace) -> None:
    _call("GET", "/api/l1/chats")


def cmd_leads(args: argparse.Namespace) -> None:
    params: dict[str, str] = {}
    if args.status:
        params["status"] = args.status
    if args.search:
        params["search"] = args.search
    _call("GET", "/api/l1/leads", params=params)


def cmd_messages(args: argparse.Namespace) -> None:
    _call("GET", f"/api/l1/chats/{args.username}/messages")


def cmd_request_new(args: argparse.Namespace) -> None:
    n = int(args.count)
    print(f"⚠  Сейчас будет реально забрано {n} лид(ов) из пула CRM.")
    print("   Они уйдут из общего пула и окажутся привязанными к нашему")
    print("   ai-l1 пользователю. Подтверди ввод командой 'yes'.")
    if input("> ").strip().lower() != "yes":
        print("Отменено.")
        sys.exit(0)
    _call("POST", "/api/l1/leads/request-new", json={"count": n})


# ─── Аргументы CLI ───────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CRM API probe — разведка перед интеграцией.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preferences", help="GET /api/l1/preferences")
    sub.add_parser("chats", help="GET /api/l1/chats")

    p_leads = sub.add_parser("leads", help="GET /api/l1/leads")
    p_leads.add_argument("--status", help="фильтр по статусу лида")
    p_leads.add_argument("--search", help="поиск по username")

    p_msg = sub.add_parser("messages", help="GET /api/l1/chats/<user>/messages")
    p_msg.add_argument("username", help="username чата (без @)")

    p_rn = sub.add_parser("request-new", help="POST /api/l1/leads/request-new ⚠ забирает лидов!")
    p_rn.add_argument("--count", "-n", default="1", help="сколько лидов забрать (default 1)")

    args = parser.parse_args()
    handlers = {
        "preferences": cmd_preferences,
        "chats": cmd_chats,
        "leads": cmd_leads,
        "messages": cmd_messages,
        "request-new": cmd_request_new,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
