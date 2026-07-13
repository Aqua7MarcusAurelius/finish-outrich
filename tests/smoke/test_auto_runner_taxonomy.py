"""Smoke-тесты таксономии ошибок auto_runner (Fix #2).

Юнит-тесты чистой функции ``WorkerAutoRunnerService._classify_send_error``:
проверяем что классификация правильно раскладывает известные коды по
трём вердиктам и не даёт неизвестному коду проскочить как транзиент.

Запуск (внутри контейнера app):
    docker compose exec -T app python -m tests.smoke.test_auto_runner_taxonomy
"""
from __future__ import annotations

import sys
from modules.worker_auto_runner.service import WorkerAutoRunnerService


def test_classification_matrix() -> list[tuple[str, str, bool]]:
    """Возвращает список (name, expected_verdict, actual_ok)."""
    cases: list[tuple[str, str]] = [
        # INVALID_LEAD — лид объективно мёртвый
        ("CANNOT_WRITE", "invalid_lead"),
        ("USERNAME_NOT_FOUND", "invalid_lead"),
        ("USERNAME_UNAVAILABLE", "invalid_lead"),
        # TRANSIENT — временная проблема, продолжаем
        ("SEND_FAILED_TRANSIENT", "transient"),      # PEER_FLOOD
        ("FLOOD_WAIT", "transient"),
        ("FloodWaitError", "transient"),
        ("GENERATION_FAILED", "transient"),
        # NEEDS_OPERATOR — нужна реакция человека
        ("SESSION_EXPIRED", "needs_operator"),
        ("WORKER_NOT_RUNNING", "needs_operator"),
        # Неизвестные коды → безопасный дефолт needs_operator
        ("SOMETHING_WEIRD", "needs_operator"),
        ("", "needs_operator"),
        ("ProxyConnectionError", "needs_operator"),
    ]
    results: list[tuple[str, str, bool]] = []
    for code, expected in cases:
        got = WorkerAutoRunnerService._classify_send_error(code)
        results.append((code, expected, got == expected))
    return results


def test_no_verdict_collision() -> bool:
    """У кода не должно быть более одного вердикта одновременно."""
    codes = (
        WorkerAutoRunnerService._INVALID_LEAD_CODES
        | WorkerAutoRunnerService._TRANSIENT_BACKOFF_CODES
        | WorkerAutoRunnerService._NEEDS_OPERATOR_CODES
    )
    for c in codes:
        buckets = [
            c in WorkerAutoRunnerService._INVALID_LEAD_CODES,
            c in WorkerAutoRunnerService._TRANSIENT_BACKOFF_CODES,
            c in WorkerAutoRunnerService._NEEDS_OPERATOR_CODES,
        ]
        if sum(buckets) > 1:
            return False
    return True


def test_peer_flood_no_self_stop_semantics() -> bool:
    """Regression-guard: PEER_FLOOD (aka SEND_FAILED_TRANSIENT) НЕ должен
    считаться needs_operator. Это была главная жалоба 2026-07-11:
    88% self-stop'ов были на PEER_FLOOD, оператор просто ждал 30-60 мин
    и жал Play заново.
    """
    return (
        WorkerAutoRunnerService._classify_send_error("SEND_FAILED_TRANSIENT")
        == "transient"
    )


def main() -> int:
    failures: list[str] = []

    for code, expected, ok in test_classification_matrix():
        line = f"  code={code!r:30} expected={expected:15} → " + (
            "PASS" if ok else "FAIL"
        )
        print(line)
        if not ok:
            failures.append(f"classification: {code!r} != {expected}")

    if test_no_verdict_collision():
        print("  no verdict collision  → PASS")
    else:
        failures.append("verdict collision detected across bucket sets")

    if test_peer_flood_no_self_stop_semantics():
        print("  PEER_FLOOD → transient (not needs_operator)  → PASS")
    else:
        failures.append("PEER_FLOOD wrongly classified as needs_operator")

    print()
    if failures:
        print(f"FAILED: {len(failures)} test(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
