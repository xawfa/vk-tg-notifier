"""VK (личные сообщения) -> Telegram через Email-уведомления.
Работает без VK_TOKEN: ВК шлёт письмо о новом сообщении, скрипт ловит его по IMAP и шлёт в Telegram.
"""
import os
import time
import html
import imaplib
import email
import re
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

IMAP_HOST = os.getenv("EMAIL_IMAP", "imap.gmail.com").strip()
EMAIL_LOGIN = os.getenv("EMAIL_LOGIN", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip().replace(" ", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

if not TG_BOT_TOKEN or not TG_CHAT_ID:
    print("[FATAL] Нет TG_BOT_TOKEN / TG_CHAT_ID в .env", flush=True)
    raise SystemExit(1)
if not EMAIL_LOGIN or not EMAIL_APP_PASSWORD:
    print("[FATAL] Нет EMAIL_LOGIN / EMAIL_APP_PASSWORD в .env", flush=True)
    print("Как получить: Google-аккаунт -> Безопасность -> 2FA -> Пароли приложений -> Создай для 'Почта'", flush=True)
    raise SystemExit(1)


def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    if len(text) > 4000:
        text = text[:4000] + "…"
    r = requests.post(url, json={
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    try:
        r.raise_for_status()
    except Exception as e:
        # показываем ответ Telegram (там точная причина: chat not found / blocked / parse)
        try:
            print(f"[TG-ERR] {r.status_code} {r.text[:300]}", flush=True)
        except Exception:
            pass
        raise
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram: {j}")


def decode_mime(s: str) -> str:
    try:
        parts = email.header.decode_header(s)
        out = []
        for t, enc in parts:
            if isinstance(t, bytes):
                out.append(t.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(str(t))
        return "".join(out)
    except Exception:
        return s


def get_body(msg) -> str:
    plain = ""
    html_t = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if not isinstance(payload, (bytes, bytearray)):
                    continue
                charset = part.get_content_charset() or "utf-8"
                txt = bytes(payload).decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                plain += txt + "\n"
            elif ctype == "text/html":
                h = re.sub(r"<[^>]+>", " ", txt)
                h = html.unescape(h)
                html_t += h + "\n"
        # карточки сообщений лежат в HTML-части — клеим её тоже, парсер сам найдёт
        body = plain + "\n" + html_t
        if not body.strip():
            body = plain or html_t
    else:
        try:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, (bytes, bytearray)):
                charset = msg.get_content_charset() or "utf-8"
                body = bytes(payload).decode(charset, errors="replace")
            else:
                body = str(msg.get_payload())
        except Exception:
            try:
                body = str(msg.get_payload())
            except Exception:
                body = ""
    # чистим лишние пробелы/переносы
    body = re.sub(r"\r", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return body.strip()[:2000]


def is_vk_message(from_: str, subject: str, body: str) -> bool:
    f = from_.lower()
    subj = subject.lower()
    b = body.lower()[:2000]
    # 1) отправитель точно от ВК
    from_ok = any(k in f for k in ["notify.vk", "notify@vk", "no-reply@vk", "donotreply@vk", "vk.com", "vk.ru", "vkontakte", "вконтакте", "vkmessenger", "admin@notify"])
    if not from_ok:
        return False
    # 2) тема про сообщение ...
    subj_ok = any(k in subj for k in [
        "сообщ", "message", "написал", "написала", "написали",
        "wrote", "pm", "диалог", "личн", "reply", "ответил",
    ])
    if subj_ok:
        return True
    # 3) ... или тело-дайджест вида "отправил вам сообщение" / "3 новых личных сообщения"
    body_ok = any(k in b for k in [
        "отправил вам сообщение", "отправила вам сообщение", "отправили вам сообщение",
        "написал вам", "написала вам",
        "новых личных сообщения", "новых личных сообщений", "новое личное сообщение",
        "непрочитанных сообщений", "sent you a message", "new message",
    ])
    return body_ok


def short_digest(subject: str, body: str) -> str:
    """Коротко: кто написал + кусок текста, без 'Здравствуйте/С уважением/настройки'."""
    # Склеиваем переносы: в HTML-письмах имя и текст часто на разных строках.
    # Мусор вытираем заменой (не обрезкой — иначе убьём HTML-часть после plain-футера).
    flat = re.sub(r"\s+", " ", body)
    flat = re.sub(r"показать все|отписаться от рассылки|поменять настройки|с уважением|администрация вконтакте|settings\?act=notify|здравствуйте", " ", flat, flags=re.IGNORECASE)

    # режем на куски по маркеру "отправил(а) вам сообщение" / "написал(а) вам"
    marker = re.compile(r"отправил[аи]?\s+вам\s+сообщение|написал[аи]?\s+вам", re.IGNORECASE)
    chunks = marker.split(flat)
    found = []
    if len(chunks) > 1:
        for i in range(1, len(chunks)):
            before = chunks[i - 1]
            after = chunks[i]
            # имя — последние 2-3 слова перед маркером (без дублей)
            words = before.strip().split()
            name = " ".join(words[-2:]) if len(words) >= 2 else (words[-1] if words else "")
            # snippet — до даты ("10 сен в 14:26") или до 200 символов
            snippet = re.split(r"\d{1,2}\s+\S+\s+в\s+\d{1,2}:\d{2}", after)[0].strip()
            snippet = snippet[:200]
            if name:
                found.append((name, snippet))

    if found:
        by_name: dict = {}
        for name, snippet in found:
            by_name.setdefault(name, [])
            if snippet and snippet not in by_name[name]:
                by_name[name].append(snippet)
        rows = []
        for name, snippets in list(by_name.items())[:5]:
            name_e = html.escape(name[:60])
            if snippets:
                rows.append(f"👤 {name_e}: {html.escape(snippets[-1][:200])}")
            else:
                rows.append(f"👤 {name_e}: новое сообщение")
        n = len(found)
        head = f"💬 <b>ВК: {n} нов. ({len(by_name)} чат.)</b>"
        return head + "\n" + "\n".join(rows)

    # запасной: только заголовок + ссылка (без дампа тела)
    m = re.search(r"(\d+)\s+новых?\s+личных?\s+сообщени", subject + " " + flat, re.IGNORECASE)
    n = m.group(0) if m else "новые"
    return f"💬 <b>ВК: {html.escape(n)}</b>"


def check_once(imap):
    try:
        status, data = imap.search(None, "UNSEEN")
    except Exception as e:
        print(f"[ERR] search: {e}", flush=True)
        raise
    if status != "OK":
        return
    ids = data[0].split()
    if not ids:
        return
    # защита от завала: если непрочитанных сотни (старый ящик) — старые молча помечаем, шлём только 10 свежих
    if len(ids) > 20:
        old = ids[:-10]
        print(f"[INFO] Непрочитанных {len(ids)}, старые {len(old)} помечаю прочитанными без отправки", flush=True)
        for num in old:
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except Exception:
                pass
        ids = ids[-10:]
    else:
        print(f"[INFO] Новых писем: {len(ids)}", flush=True)
    for num in ids:
        try:
            st, d = imap.fetch(num, "(RFC822)")
            if st != "OK" or not d or not isinstance(d[0], tuple) or len(d[0]) < 2:
                continue
            raw = d[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(bytes(raw))
            from_ = decode_mime(str(msg.get("From", "")))
            subject = decode_mime(str(msg.get("Subject", "")))
            body = get_body(msg)

            if not is_vk_message(from_, subject, body):
                # не личка (промо/купоны) — помечаем чтобы не проверять снова, но не шлём
                try:
                    imap.store(num, "+FLAGS", "\\Seen")
                except Exception:
                    pass
                continue

            print(f"[NEW] VK письмо: {subject[:80]}", flush=True)
            text = short_digest(subject, body) + "\n\n🔗 <a href=\"https://vk.ru/im?tab=unread\">Открыть сообщения</a>"
            tg_send(text)
            # помечаем прочитанным чтобы не слать дважды
            imap.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            print(f"[ERR] письмо: {e}", flush=True)


def run_once():
    """Одна проверка и выход — для бесплатных Cron (Render Cron / GitHub Actions)."""
    import socket
    # быстрая проверка: в адресе сервера не должно быть @ и пробелов
    if "@" in IMAP_HOST or " " in IMAP_HOST or "." not in IMAP_HOST:
        print(f"[FATAL] EMAIL_IMAP похож на ошибку (там должен быть хост вида imap.gmail.com). Проверь секрет.", flush=True)
        raise SystemExit(1)
    last_err = None
    for attempt in range(1, 4):
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(EMAIL_LOGIN, EMAIL_APP_PASSWORD)
            imap.select("INBOX")
            print(f"[OK] IMAP {IMAP_HOST} подключён как {EMAIL_LOGIN} (once)", flush=True)
            check_once(imap)
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass
            print("[OK] Once done.", flush=True)
            return
        except (socket.gaierror, socket.timeout, OSError) as e:
            last_err = e
            print(f"[WARN] Сеть ({e}), попытка {attempt}/3, жду 15 сек…", flush=True)
            time.sleep(15)
    print(f"[FATAL] Не смог подключиться к {IMAP_HOST}: {last_err}", flush=True)
    raise SystemExit(1)


def run():
    print("[OK] VK(email) → Telegram запущен. Проверка каждые", CHECK_INTERVAL, "сек", flush=True)
    # стартовое сообщение шлём один раз
    if "--no-hello" not in sys.argv:
        try:
            tg_send("✅ <b>VK → Telegram (через email) подключён.</b>\nНовые письма от ВК будут приходить сюда.")
        except Exception as e:
            print(f"[WARN] tg_send: {e}", flush=True)

    while True:
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(EMAIL_LOGIN, EMAIL_APP_PASSWORD)
            imap.select("INBOX")
            print(f"[OK] IMAP {IMAP_HOST} подключён как {EMAIL_LOGIN}", flush=True)
            while True:
                try:
                    check_once(imap)
                except Exception as e:
                    print(f"[ERR] check: {e}", flush=True)
                time.sleep(CHECK_INTERVAL)
                try:
                    imap.noop()
                except Exception:
                    break  # переподключиться
        except imaplib.IMAP4.error as e:
            print(f"[ERR] IMAP login: {e}. Проверь EMAIL_LOGIN и пароль приложений (не обычный пароль!). Пауза 60 сек", flush=True)
            time.sleep(60)
        except Exception as e:
            print(f"[ERR] {e}. Пауза 15 сек", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run()
