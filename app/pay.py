"""Платёжный шлюз Robokassa: ссылка на оплату пополнения и проверка подписи уведомлений.

Настройка (переменные окружения):
  ROBOKASSA_LOGIN       — идентификатор магазина (MerchantLogin)
  ROBOKASSA_PASS1       — пароль #1 (подпись ссылки на оплату и возврата на SuccessURL)
  ROBOKASSA_PASS2       — пароль #2 (подпись уведомления на ResultURL)
  ROBOKASSA_TEST=1      — тестовый режим (IsTest=1); пароли тогда — тестовые из настроек магазина
  ROBOKASSA_HASH        — алгоритм подписи из настроек магазина: md5 (по умолчанию), sha1, sha256, sha384, sha512
  ROBOKASSA_LABEL_SBP   — IncCurrLabel способа «СБП» (необязательно; без него способ выбирается на странице Robokassa)
  ROBOKASSA_LABEL_CARD  — IncCurrLabel способа «Банковская карта» (необязательно)

Без LOGIN/PASS1/PASS2 шлюз выключен: пополнение остаётся заявкой, которую одобряет администратор.
"""

import hashlib
import hmac
import os
from urllib.parse import urlencode

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"

LOGIN = os.environ.get("ROBOKASSA_LOGIN", "").strip()
PASS1 = os.environ.get("ROBOKASSA_PASS1", "").strip()
PASS2 = os.environ.get("ROBOKASSA_PASS2", "").strip()
TEST = os.environ.get("ROBOKASSA_TEST", "") in ("1", "true", "yes")
HASH = os.environ.get("ROBOKASSA_HASH", "md5").strip().lower() or "md5"
LABELS = {
    "sbp": os.environ.get("ROBOKASSA_LABEL_SBP", "").strip(),
    "card": os.environ.get("ROBOKASSA_LABEL_CARD", "").strip(),
}


def enabled() -> bool:
    return bool(LOGIN and PASS1 and PASS2)


def _sign(*parts: str) -> str:
    return hashlib.new(HASH, ":".join(parts).encode()).hexdigest()


def _shp(params: dict[str, str]) -> list[str]:
    """Пользовательские параметры Shp_* входят в подпись в алфавитном порядке как «Shp_x=значение»."""
    return [f"{k}={params[k]}" for k in sorted(params, key=str.lower) if k.lower().startswith("shp_")]


def out_sum(amount: int) -> str:
    return f"{amount}.00"


def payment_url(inv_id: int, amount: int, description: str, method: str, email: str = "") -> str:
    s = out_sum(amount)
    q = {
        "MerchantLogin": LOGIN,
        "OutSum": s,
        "InvId": str(inv_id),
        "Description": description[:100],
        "SignatureValue": _sign(LOGIN, s, str(inv_id), PASS1),
        "Culture": "ru",
        "Encoding": "utf-8",
    }
    if LABELS.get(method):
        q["IncCurrLabel"] = LABELS[method]
    if email:
        q["Email"] = email
    if TEST:
        q["IsTest"] = "1"
    return PAY_URL + "?" + urlencode(q)


def _check(params: dict[str, str], password: str) -> bool:
    got = params.get("SignatureValue", "")
    s, inv = params.get("OutSum", ""), params.get("InvId", "")
    if not (got and s and inv):
        return False
    return hmac.compare_digest(_sign(s, inv, password, *_shp(params)).lower(), got.lower())


def check_result(params: dict[str, str]) -> bool:
    """Подпись уведомления об оплате (ResultURL) — паролем #2."""
    return _check(params, PASS2)


def check_success(params: dict[str, str]) -> bool:
    """Подпись возврата покупателя на SuccessURL — паролем #1."""
    return _check(params, PASS1)
