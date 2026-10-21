"""
اختبار الاتصال بمصادر الأخبار و Groq من داخل الحاوية.
التشغيل:
    docker cp test_news.py telegram_crypto_bot:/app/test_news.py
    docker exec telegram_crypto_bot python /app/test_news.py
"""
import os
import json
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip().strip('"').strip("'")
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()


class _FollowRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, 307, msg, headers)


_opener = urllib.request.build_opener(_FollowRedirects)

NON_CHAT = ("whisper", "tts", "guard", "embed", "orpheus", "playai", "transcribe", "moderation", "safeguard")
PREFERRED = ("llama-3.3-70b", "gpt-oss-120b", "llama-4", "gpt-oss-20b", "qwen", "llama-3.1-70b", "llama-3.1-8b")


def check_feed(name, url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (crypto-bot)"})
        data = _opener.open(req, timeout=15).read()
        root = ET.fromstring(data)
        titles = [(it.findtext("title") or "").strip() for it in root.iter("item")]
        titles = [t for t in titles if t]
        print("OK   {}: {} عنوان. أحدثها: {}".format(name, len(titles), titles[0] if titles else "-"))
        return True
    except urllib.error.HTTPError as e:
        print("FAIL {}: HTTP {} ({})".format(name, e.code, "الموقع يحجب الطلب" if e.code in (403, 429) else e.reason))
    except Exception as e:
        print("FAIL {}: {}".format(name, e))
    return False


def groq_call(path, body=None):
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1" + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + GROQ_API_KEY,
                 "User-Agent": "crypto-bot/1.0"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))


def pick_model(ids):
    chat = [i for i in ids if i and not any(h in i.lower() for h in NON_CHAT)]
    for pref in PREFERRED:
        for i in chat:
            if pref in i.lower():
                return i
    return chat[0] if chat else None


def check_groq():
    if not GROQ_API_KEY:
        print("FAIL Groq: المتغير GROQ_API_KEY غير موجود داخل الحاوية (تحقق من .env ثم أعد: docker compose up -d)")
        return False
    print("     مفتاح Groq موجود، يبدأ بـ {}، طوله {}".format(GROQ_API_KEY[:4], len(GROQ_API_KEY)))
    try:
        ids = [m.get("id", "") for m in groq_call("/models").get("data", []) if m.get("active", True)]
        print("     النماذج المتاحة لحسابك ({}): {}".format(len(ids), ", ".join(ids)))
        model = GROQ_MODEL or pick_model(ids)
        if not model:
            print("FAIL Groq: لا يوجد نموذج دردشة متاح")
            return False
        print("     النموذج المستخدم في الاختبار: {}".format(model))
        body = {
            "model": model, "temperature": 0,
            "messages": [
                {"role": "system", "content": 'Reply ONLY with JSON: {"sentiment":"positive|neutral|negative","severity":1-5,"reason":"short Arabic sentence"}'},
                {"role": "user", "content": "Headlines:\n1. Major exchange hacked for $500M\n2. Bitcoin falls 2%"},
            ],
        }
        resp = groq_call("/chat/completions", body)
        print("OK   Groq: {}".format(resp["choices"][0]["message"]["content"]))
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        hints = {
            401: "المفتاح خاطئ أو ملغى، أنشئ مفتاحاً جديداً",
            403: "الطلب محجوب (تحقق من المفتاح أو الشبكة)",
            404: "النموذج غير متاح، اترك GROQ_MODEL فارغاً في .env ليختار البوت تلقائياً",
            429: "تجاوزت حد الطلبات، انتظر قليلاً",
        }
        print("FAIL Groq: HTTP {} - {}".format(e.code, hints.get(e.code, e.reason)))
        print("     التفاصيل:", detail)
    except Exception as e:
        print("FAIL Groq:", e)
    return False


if __name__ == "__main__":
    print("--- مصادر الأخبار (RSS) ---")
    feeds_ok = [check_feed(n, u) for n, u in FEEDS]
    print("--- Groq ---")
    groq_ok = check_groq()
    print("--- الخلاصة ---")
    print("RSS: {}/{} يعمل | Groq: {}".format(sum(feeds_ok), len(FEEDS), "يعمل" if groq_ok else "لا يعمل (سيُستخدم فحص الكلمات المفتاحية)"))
