"""
BIST Saatlik Mail Raporu
─────────────────────────
Watchlist'teki hisseler + genel BIST 100 / BIST 100 Dışı taramasını
yapar, sonucu Gmail üzerinden mail olarak gönderir.

Bulutta (PythonAnywhere, Render vb.) saatlik zamanlanmış görev (cron) olarak çalıştırılmalıdır.

Gerekli ortam değişkenleri:
  ANTHROPIC_API_KEY   → Claude API anahtarı
  GMAIL_ADRES         → Gönderen Gmail adresi (örn: seninmail@gmail.com)
  GMAIL_UYGULAMA_SIFRE→ Gmail "Uygulama Şifresi" (normal şifre DEĞİL)
  ALICI_MAIL          → Raporun gönderileceği mail adresi (kendi adresin olabilir)

Kurulum: pip install yfinance pandas anthropic
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import yfinance as yf
import pandas as pd
import anthropic

# ════════════════════════════════════════════════════════════════
#  AYARLAR
# ════════════════════════════════════════════════════════════════

KLASOR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_DOSYA = os.path.join(KLASOR, "watchlist.json")

BIST100 = [
    "GARAN.IS","ISCTR.IS","AKBNK.IS","YKBNK.IS","HALKB.IS","VAKBN.IS","TSKB.IS",
    "EREGL.IS","TOASO.IS","FROTO.IS","TUPRS.IS","KCHOL.IS","SAHOL.IS","TKFEN.IS",
    "THYAO.IS","PGSUS.IS","TAVHL.IS",
    "AKSEN.IS","ZOREN.IS","ODAS.IS","ENKAI.IS",
    "BIMAS.IS","MGROS.IS","SOKM.IS","ULKER.IS","TTKOM.IS",
    "ASELS.IS","LOGO.IS","NETAS.IS","ARCLK.IS",
    "EKGYO.IS","ISGYO.IS",
    "SASA.IS","KOZAL.IS","KORDS.IS","DOHOL.IS","OTOKAR.IS","BRISA.IS",
]

BIST_DISI = [
    "INDES.IS","CIMSA.IS","MAVI.IS","GOZDE.IS",
    "PLTKM.IS","FONET.IS","KRONT.IS",
    "AEFES.IS","CCOLA.IS","TATGD.IS","BANVT.IS",
    "ECILC.IS","GUBRF.IS","NTTUR.IS","MARTI.IS","MPARK.IS",
]


# ════════════════════════════════════════════════════════════════
#  WATCHLIST
# ════════════════════════════════════════════════════════════════

def watchlist_yukle():
    if os.path.exists(WATCHLIST_DOSYA):
        with open(WATCHLIST_DOSYA, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ════════════════════════════════════════════════════════════════
#  VERİ + TEKNİK ANALİZ (önceki sürümle aynı mantık)
# ════════════════════════════════════════════════════════════════

def hisse_verisi_al(ticker, gun=120):
    try:
        df = yf.Ticker(ticker).history(period=f"{gun}d")
        return df if not df.empty else None
    except Exception:
        return None


def temel_veriler_al(ticker):
    try:
        bilgi = yf.Ticker(ticker).info or {}
        return {
            "f/k_orani":  bilgi.get("trailingPE"),
            "pd_dd":      bilgi.get("priceToBook"),
            "ozkaynak_getirisi": bilgi.get("returnOnEquity"),
            "beta":       bilgi.get("beta"),
            "sirket_adi": bilgi.get("longName", ticker.replace(".IS", "")),
            "sektor":     bilgi.get("sector", "—"),
        }
    except Exception:
        return {}


def teknik_gostergeler(df):
    k = df["Close"]
    ema20 = k.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = k.ewm(span=50, adjust=False).mean().iloc[-1]

    delta = k.diff()
    kazanc = delta.clip(lower=0).rolling(14).mean()
    kayip = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = (100 - 100 / (1 + kazanc / kayip)).iloc[-1]

    ema12 = k.ewm(span=12, adjust=False).mean()
    ema26 = k.ewm(span=26, adjust=False).mean()
    macd_hat = ema12 - ema26
    sinyal_hat = macd_hat.ewm(span=9, adjust=False).mean()

    hacim_10 = df["Volume"].tail(10).mean()
    hacim_30 = df["Volume"].tail(30).mean()
    hacim_or = hacim_10 / hacim_30 if hacim_30 > 0 else 1

    def deg(n):
        return (k.iloc[-1] - k.iloc[-n]) / k.iloc[-n] * 100 if len(k) > n else 0

    return {
        "son_fiyat": round(k.iloc[-1], 2),
        "rsi": round(rsi, 1),
        "macd_bullish": bool(macd_hat.iloc[-1] > sinyal_hat.iloc[-1]),
        "hacim_orani": round(hacim_or, 2),
        "degisim_1g": round(deg(2), 2),
        "degisim_1ay": round(deg(22), 2),
        "trend": "yukselis" if ema20 > ema50 else "dusus",
    }


def puan_hesapla(g, t):
    puan = 50
    rsi = g.get("rsi", 50)
    if 30 <= rsi <= 50:
        puan += 12
    elif 50 < rsi <= 65:
        puan += 6
    elif rsi > 70:
        puan -= 8

    puan += 10 if g.get("trend") == "yukselis" else -5
    puan += 8 if g.get("macd_bullish") else -4

    h = g.get("hacim_orani", 1)
    if h > 1.3:
        puan += 8
    elif h > 1.1:
        puan += 4

    fk = t.get("f/k_orani")
    if fk and 3 <= fk <= 8:
        puan += 9
    elif fk and fk > 20:
        puan -= 5

    return max(0, min(100, puan))


def hisse_analiz_et(ticker):
    df = hisse_verisi_al(ticker)
    if df is None or len(df) < 30:
        return None
    g = teknik_gostergeler(df)
    t = temel_veriler_al(ticker)
    puan = puan_hesapla(g, t)
    return {
        "ticker": ticker.replace(".IS", ""),
        "sirket": t.get("sirket_adi", ticker),
        "fiyat": g["son_fiyat"],
        "puan": puan,
        "rsi": g["rsi"],
        "trend": g["trend"],
        "degisim_1g": g["degisim_1g"],
        "degisim_1ay": g["degisim_1ay"],
        "fk": t.get("f/k_orani"),
    }


# ════════════════════════════════════════════════════════════════
#  AI YORUMU (sadece watchlist hisseleri için detaylı)
# ════════════════════════════════════════════════════════════════

def ai_watchlist_yorumu(watchlist_sonuclari):
    anahtar = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anahtar or not watchlist_sonuclari:
        return ""

    istemci = anthropic.Anthropic(api_key=anahtar)
    ozet = json.dumps(watchlist_sonuclari, ensure_ascii=False, indent=2)

    try:
        yanit = istemci.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system="Sen kisa ve net yorum yapan bir Turk borsa analistisin.",
            messages=[{"role": "user", "content": f"""
Asagidaki watchlist hisseleri icin HER BIRI icin 2-3 cumlelik kisa yorum yaz.
Su an alinir mi, izlenmeli mi, riskli mi - net belirt.

{ozet}
"""}]
        )
        return yanit.content[0].text
    except Exception as e:
        return f"[AI yorumu alinamadi: {e}]"


# ════════════════════════════════════════════════════════════════
#  MAIL OLUŞTUR + GÖNDER
# ════════════════════════════════════════════════════════════════

def mail_govdesi_olustur(watchlist_sonuclari, watchlist_ai_yorum, bist100_top, bistdisi_top):
    zaman = datetime.now().strftime("%d.%m.%Y %H:%M")

    govde = f"BIST SAATLIK RAPOR — {zaman}\n"
    govde += "=" * 60 + "\n\n"

    # Watchlist bölümü
    if watchlist_sonuclari:
        govde += "★ TAKIP LISTENIZ (Watchlist)\n"
        govde += "-" * 60 + "\n"
        for s in watchlist_sonuclari:
            sinyal = "AL" if s["puan"] >= 70 else "IZLE" if s["puan"] >= 55 else "DIKKAT"
            govde += (
                f"{s['ticker']:<8} {s['sirket'][:25]:<25} "
                f"{s['fiyat']:>8.2f}TL  Puan:{s['puan']:>3}  "
                f"[{sinyal}]  Gunluk:{s['degisim_1g']:+.1f}%  1Ay:{s['degisim_1ay']:+.1f}%\n"
            )
        govde += "\n"
        if watchlist_ai_yorum:
            govde += "AI YORUMU:\n" + watchlist_ai_yorum + "\n\n"
    else:
        govde += "★ TAKIP LISTENIZ BOS — watchlist_yonetici.py ile hisse ekleyebilirsiniz\n\n"

    # Genel piyasa - en iyi 5
    govde += "★ BIST 100 — EN YUKSEK PUANLI 5 HISSE\n"
    govde += "-" * 60 + "\n"
    for s in bist100_top[:5]:
        sinyal = "AL" if s["puan"] >= 70 else "IZLE"
        govde += f"{s['ticker']:<8} {s['fiyat']:>8.2f}TL  Puan:{s['puan']:>3}  [{sinyal}]\n"

    govde += "\n★ BIST 100 DISI — EN YUKSEK PUANLI 5 HISSE\n"
    govde += "-" * 60 + "\n"
    for s in bistdisi_top[:5]:
        sinyal = "AL" if s["puan"] >= 70 else "IZLE"
        govde += f"{s['ticker']:<8} {s['fiyat']:>8.2f}TL  Puan:{s['puan']:>3}  [{sinyal}]\n"

    govde += "\n" + "=" * 60 + "\n"
    govde += "ONEMLI: Bu rapor otomatik bir analiz aracidir, yatirim tavsiyesi DEGILDIR.\n"
    govde += "Karar vermeden once kendi arastirmanizi yapin.\n"
    govde += "Watchlist'i degistirmek icin: watchlist_yonetici.py\n"

    return govde


def mail_gonder(govde):
    gonderen = os.environ.get("GMAIL_ADRES", "")
    sifre = os.environ.get("GMAIL_UYGULAMA_SIFRE", "")
    alici = os.environ.get("ALICI_MAIL", gonderen)

    if not gonderen or not sifre:
        print("[!] GMAIL_ADRES veya GMAIL_UYGULAMA_SIFRE ayarlanmamis. Mail gonderilemedi.")
        print("\n--- RAPOR ICERIGI (mail gonderilemedi, ekrana yaziliyor) ---\n")
        print(govde)
        return False

    msg = MIMEMultipart()
    msg["From"] = gonderen
    msg["To"] = alici
    msg["Subject"] = f"BIST Saatlik Rapor — {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    msg.attach(MIMEText(govde, "plain", "utf-8"))

    try:
        sunucu = smtplib.SMTP("smtp.gmail.com", 587)
        sunucu.starttls()
        sunucu.login(gonderen, sifre)
        sunucu.send_message(msg)
        sunucu.quit()
        print(f"[✓] Mail gonderildi: {alici}")
        return True
    except Exception as e:
        print(f"[!] Mail gonderme hatasi: {e}")
        return False


# ════════════════════════════════════════════════════════════════
#  ANA AKIŞ
# ════════════════════════════════════════════════════════════════

def calistir():
    print(f"[{datetime.now()}] Saatlik analiz basliyor...")

    # Watchlist analizi
    watchlist = watchlist_yukle()
    watchlist_sonuclari = []
    for kod in watchlist:
        ticker = kod if kod.endswith(".IS") else kod + ".IS"
        sonuc = hisse_analiz_et(ticker)
        if sonuc:
            watchlist_sonuclari.append(sonuc)
        time.sleep(0.3)

    watchlist_ai_yorum = ai_watchlist_yorumu(watchlist_sonuclari) if watchlist_sonuclari else ""

    # Genel tarama (BIST100 + BIST_DISI) - hızlı versiyon
    bist100_sonuc = []
    for t in BIST100:
        s = hisse_analiz_et(t)
        if s:
            bist100_sonuc.append(s)
        time.sleep(0.2)
    bist100_sonuc.sort(key=lambda x: x["puan"], reverse=True)

    bistdisi_sonuc = []
    for t in BIST_DISI:
        s = hisse_analiz_et(t)
        if s:
            bistdisi_sonuc.append(s)
        time.sleep(0.2)
    bistdisi_sonuc.sort(key=lambda x: x["puan"], reverse=True)

    # Mail oluştur ve gönder
    govde = mail_govdesi_olustur(watchlist_sonuclari, watchlist_ai_yorum, bist100_sonuc, bistdisi_sonuc)
    mail_gonder(govde)

    print(f"[{datetime.now()}] Analiz tamamlandi.\n")


if __name__ == "__main__":
    calistir()
