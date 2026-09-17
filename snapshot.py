#!/usr/bin/env python3
"""朝のマーケット・ブリーフ用スナップショット取得。
LLMや検索を使わず、機械可読なデータ元から数値を取り、決まった書式のMarkdownを出力する。
取得できない項目は数値を作らず「未取得」と書く。
"""
import csv, io, json, re, sys, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

JST = ZoneInfo("Asia/Tokyo")
NOW = dt.datetime.now(JST)
TODAY = NOW.date()
OUT = Path(__file__).parent / "data"

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
})


def get(url, **kw):
    last = None
    for i in range(4):
        try:
            r = S.get(url, timeout=25, **kw)
            if r.status_code == 200 and r.content:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{last}")


def dedupe(rows):
    d = {}
    for day, v in rows:
        if v is not None:
            d[day] = float(v)
    rows = sorted(d.items())
    if len(rows) < 2:
        raise RuntimeError("データ行が2行未満")
    return rows


# ---------------- データ元 ----------------
def yahoo(sym):
    """戻り値: {"rows":[(date, close)], "price", "time"}"""
    err = None
    for host in ("query1", "query2"):
        try:
            r = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}",
                    params={"range": "1mo", "interval": "1d"})
            res = r.json()["chart"]["result"][0]
            tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "UTC")
            closes = res["indicators"]["quote"][0]["close"]
            rows = dedupe((dt.datetime.fromtimestamp(t, tz).date(), c)
                          for t, c in zip(res["timestamp"], closes))
            m = res["meta"]
            return {"rows": rows, "price": m.get("regularMarketPrice"),
                    "time": m.get("regularMarketTime"), "tz": tz}
        except Exception as e:
            err = e
    raise RuntimeError(f"Yahoo {sym}: {err}")


def stooq(sym):
    r = get("https://stooq.com/q/d/l/", params={"s": sym, "i": "d"})
    rows = [(dt.date.fromisoformat(x["Date"]), x["Close"])
            for x in csv.DictReader(io.StringIO(r.text)) if x.get("Close")]
    return {"rows": dedupe(rows)[-30:]}


def nikkei_csv(name):
    r = get(f"https://indexes.nikkei.co.jp/nkave/historical/{name}")
    txt = r.content.decode("shift_jis", "ignore")
    rows = []
    for line in csv.reader(io.StringIO(txt)):
        if len(line) >= 2 and re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", line[0].strip()):
            try:
                rows.append((dt.datetime.strptime(line[0].strip(), "%Y/%m/%d").date(),
                             float(line[1].replace(",", ""))))
            except ValueError:
                pass
    return {"rows": dedupe(rows)}


def parse_jdate(s):
    s = s.strip()
    m = re.fullmatch(r"([RHS])(\d+)\.(\d+)\.(\d+)", s)
    if m:
        base = {"R": 2018, "H": 1988, "S": 1925}[m[1]]
        return dt.date(base + int(m[2]), int(m[3]), int(m[4]))
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


def mof_jgb10():
    """財務省 国債金利情報（当月分→足りなければ全期間）"""
    base = "https://www.mof.go.jp/jgbs/reference/interest_rate/"
    rows = []
    for name in ("jgbcm.csv", "data/jgbcm_all.csv"):
        txt = get(base + name).content.decode("shift_jis", "ignore")
        data = list(csv.reader(io.StringIO(txt)))
        hi = next(i for i, row in enumerate(data) if row and row[0].strip() == "基準日")
        col = [c.strip() for c in data[hi]].index("10年")
        for row in data[hi + 1:]:
            if len(row) > col:
                d = parse_jdate(row[0])
                try:
                    rows.append((d, float(row[col])))
                except ValueError:
                    pass
        rows = [x for x in rows if x[0]]
        if len({d for d, _ in rows}) >= 2:
            break
    return {"rows": dedupe(rows)}


def fred(series):
    r = get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series})
    rows = []
    for x in csv.reader(io.StringIO(r.text)):
        if len(x) == 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x[0]) and x[1] not in (".", ""):
            rows.append((dt.date.fromisoformat(x[0]), x[1]))
    return {"rows": dedupe(rows)[-30:]}


def matsui_index(code, etf, lo, hi):
    """松井証券の時系列ページ上部にある「現在値・前日比」を読む。
    読み違いを防ぐため、(1)値の範囲 (2)現在値・前日比・騰落率の整合 (3)連動ETFの騰落率との差 を確認する。"""
    import html as htmlmod
    r = get(f"https://finance.matsui.co.jp/stock/{code}/daily-bar/index")
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", r.text))
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}).{0,80}?現在値 ([\d,]+\.\d+) 前日比 "
                  r"([+\-−]?[\d,]+\.\d+) ?\(([+\-−]?[\d.]+)%\)", text)
    if not m:
        raise RuntimeError("ページ上に現在値が見つからない")
    y, mo, d, hh, mm = (int(x) for x in m.groups()[:5])
    v = float(m[6].replace(",", ""))
    ch = float(m[7].replace(",", "").replace("−", "-"))
    pct = float(m[8].replace("−", "-"))
    if not lo < v < hi:
        raise RuntimeError(f"値が想定範囲外: {v}")
    prev = v - ch
    if abs(ch / prev * 100 - pct) > 0.03:
        raise RuntimeError("現在値・前日比・騰落率が整合しない")
    day = dt.date(y, mo, d)
    try:  # ETFとの照合（ETFが取れないときは照合を省略）
        etfrows = dict(yahoo(etf)["rows"])
        days = sorted(k for k in etfrows if k <= day)
        if len(days) >= 2 and days[-1] == day:
            etf_pct = (etfrows[days[-1]] / etfrows[days[-2]] - 1) * 100
            if abs(etf_pct - pct) > 1.0:
                raise ValueError(f"ETF({etf})騰落率{etf_pct:.2f}%と不一致")
    except ValueError as e:
        raise RuntimeError(str(e))
    except Exception:
        pass
    when = f"{mo}/{d}終値" if hh >= 15 else f"{mo}/{d} {hh}:{mm:02d}時点"
    return f"{num(v, 2)}（{when}）{stale(day)}", f"前日比 {sg(ch, 2)}ポイント（{sg(pct, 2)}%）"


# ---------------- 騰落レシオ（JPX公式「商況プリント（後場）」の値上り・値下り銘柄数から計算） ----------------
JPX_PDF = "https://www.jpx.co.jp/markets/equities/volume-and-value/tvdivq000000derc-att/2_{}.pdf"


def pdf_text(content):
    import pypdf
    return "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(io.BytesIO(content)).pages)


def jpx_prime_counts(day):
    """指定日の東証プライム 値上り・値下り銘柄数。休場・未公開なら None"""
    ymd = day.strftime("%Y%m%d")
    r = None
    for i in range(3):
        try:
            r = S.get(JPX_PDF.format(ymd), timeout=25)
            if r.status_code in (200, 404):
                break
        except requests.RequestException:
            pass
        time.sleep(3)
    if r is None or r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"JPX {ymd}: HTTP {r.status_code}")
    text = pdf_text(r.content)
    if ymd not in text:
        raise RuntimeError(f"JPX {ymd}: PDF内に日付がない")
    for line in text.splitlines():
        nums = re.findall(r"\d[\d,]*(?:\.\d+)?", line)
        if "プライム" in line and len(nums) == 14:
            break
    else:  # 日本語が読めない場合に備え、数値14個の最初の行（表の1行目＝プライム）を使う
        rows = [re.findall(r"\d[\d,]*(?:\.\d+)?", ln) for ln in text.splitlines()]
        rows = [x for x in rows if len(x) == 14]
        if rows:
            nums = rows[0]
        else:  # 行が崩れて抽出された場合：見出しの日付の直後に並ぶ14個の数値を使う
            toks = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
            if ymd not in toks or len(toks) < toks.index(ymd) + 15:
                raise RuntimeError(f"JPX {ymd}: 騰落銘柄数の行が見つからない")
            i = toks.index(ymd)
            nums = toks[i + 1:i + 15]
    v = [float(x.replace(",", "")) for x in nums]
    b_, up, down, flat, nocmp = v[2], v[6], v[8], v[10], v[12]
    if up + down + flat + nocmp != b_ or not 1000 < b_ < 3000:
        raise RuntimeError(f"JPX {ymd}: 銘柄数の合計が合わない")
    if abs(up / b_ * 100 - v[7]) > 0.02:
        raise RuntimeError(f"JPX {ymd}: 値上り比率が合わない")
    return int(up), int(down)


def advdec_ratios():
    cache_file = OUT / "advdec.json"
    cache = json.loads(cache_file.read_text("utf-8")) if cache_file.exists() else {}
    sessions = []  # 新しい順 [(date, up, down)]
    day, fetched = TODAY, 0
    for _ in range(70):
        day -= dt.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        key = day.isoformat()
        if key not in cache:
            if fetched >= 45:
                break
            got = jpx_prime_counts(day)
            fetched += 1
            time.sleep(0.5)
            if got is None:
                if (TODAY - day).days <= 3:  # 直近の欠落は未公開の可能性があるので記録しない
                    continue
                cache[key] = None
            else:
                cache[key] = list(got)
        if cache[key]:
            sessions.append((day, *cache[key]))
        if len(sessions) >= 26:
            break
    OUT.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(dict(sorted(cache.items())[-120:]), ensure_ascii=False, indent=0), "utf-8")
    if len(sessions) < 26:
        raise RuntimeError(f"営業日データが{len(sessions)}日分しかない")

    def ratio(start, n):
        part = sessions[start:start + n]
        return sum(x[1] for x in part) / sum(x[2] for x in part) * 100

    latest = sessions[0][0]
    cur = {n: ratio(0, n) for n in (25, 10, 6)}
    prev = {n: ratio(1, n) for n in (25, 10, 6)}
    zone = "過熱" if cur[25] > 120 else "売られすぎ" if cur[25] < 70 else "中立圏"
    value = (f"25日 {cur[25]:.2f}%・10日 {cur[10]:.2f}%・6日 {cur[6]:.2f}%"
             f"（{md(latest)}時点、25日は{zone}）{stale(latest)}")
    change = "前日比 " + "・".join(f"{n}日 {sg(cur[n] - prev[n], 2)}ポイント" for n in (25, 10, 6))
    return value, change


def cnn_fg():
    r = get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"})
    return r.json()["fear_and_greed"]


# ---------------- 書式 ----------------
def num(x, nd):
    return f"{x:,.{nd}f}"


def sg(x, nd):
    return ("+" if x >= 0 else "-") + f"{abs(x):,.{nd}f}"


def md(d):
    return f"{d.month}/{d.day}"


def stale(d):
    return "※最新日付でない可能性" if (TODAY - d).days > 4 else ""


def fmt_close(rows, vunit, cunit, nd):
    (d1, v1), (d0, v0) = rows[-1], rows[-2]
    ch = v1 - v0
    value = f"{num(v1, nd)}{vunit}（{md(d1)}終値）{stale(d1)}"
    change = f"前日比 {sg(ch, nd)}{cunit}（{sg(ch / v0 * 100, 2)}%）"
    return value, change


def fmt_yield(rows):
    (d1, v1), (d0, v0) = rows[-1], rows[-2]
    if v1 > 20:  # 旧形式（利回り×10）への保険
        v1, v0 = v1 / 10, v0 / 10
    return f"{v1:.3f}%（{md(d1)}）{stale(d1)}", f"前日比 {sg((v1 - v0) * 100, 1)}bp"


def fmt_live(q, cunit, nd, label_time=True):
    """先物・為替: 取得時点の価格と、前営業日終値との差"""
    rows, price = q["rows"], q.get("price")
    if price is None or not q.get("time"):
        return fmt_close(rows, cunit, cunit, nd)
    t = dt.datetime.fromtimestamp(q["time"], JST)
    tday = dt.datetime.fromtimestamp(q["time"], q["tz"]).date()
    prev = [v for d, v in rows if d < tday] or [rows[-2][1]]
    ch = price - prev[-1]
    value = f"{num(price, nd)}{cunit}（{t.month}/{t.day} {t:%H:%M}時点）{stale(t.date())}"
    return value, f"前営業日終値比 {sg(ch, nd)}{cunit}（{sg(ch / prev[-1] * 100, 2)}%）"


FG_JA = {"extreme fear": "Extreme Fear（極度の恐怖）", "fear": "Fear（恐怖）", "neutral": "Neutral（中立）",
         "greed": "Greed（強欲）", "extreme greed": "Extreme Greed（極度の強欲）"}


def fmt_fg(j):
    t = dt.datetime.fromisoformat(str(j["timestamp"]).replace("Z", "+00:00")).astimezone(JST)
    value = f"{j['score']:.1f}（{t.month}/{t.day} {t:%H:%M}時点）{stale(t.date())}"
    change = f"前日 {float(j['previous_close']):.1f}、区分 {FG_JA.get(str(j['rating']).lower(), j['rating'])}"
    return value, change


# ---------------- 項目定義（上から順にデータ元を試す） ----------------
YH = ("Yahoo Finance", "https://finance.yahoo.com/")
ST = ("Stooq", "https://stooq.com/")
NK = ("日経の指数公式サイト", "https://indexes.nikkei.co.jp/nkave")
MOF = ("財務省 国債金利情報", "https://www.mof.go.jp/jgbs/reference/interest_rate/")
FR = ("FRED（セントルイス連銀）", "https://fred.stlouisfed.org/series/DGS10")
MT = ("松井証券 TOPIX時系列", "https://finance.matsui.co.jp/stock/.TOPX/daily-bar/index")
MT2 = ("松井証券 グロース250時系列", "https://finance.matsui.co.jp/stock/.MTHR/daily-bar/index")
JPXS = ("日本取引所グループ 商況プリント（値上り・値下り銘柄数から計算）", "https://www.jpx.co.jp/markets/equities/volume-and-value/")
CNN = ("CNN Fear & Greed Index", "https://edition.cnn.com/markets/fear-and-greed")

SKIP = object()  # 安定したデータ元がなく、自動取得の対象外とする項目

ITEMS = [
    ("日経225", [(NK, lambda: fmt_close(nikkei_csv("nikkei_stock_average_daily_jp.csv")["rows"], "円", "円", 2)),
                (YH, lambda: fmt_close(yahoo("^N225")["rows"], "円", "円", 2)),
                (ST, lambda: fmt_close(stooq("^nkx")["rows"], "円", "円", 2))]),
    ("日経225先物（シカゴCME円建てで代用）", [(YH, lambda: fmt_live(yahoo("NIY=F"), "円", 0))]),
    ("TOPIX", [(MT, lambda: matsui_index(".TOPX", "1306.T", 1000, 10000))]),
    ("グロース250", [(MT2, lambda: matsui_index(".MTHR", "2516.T", 200, 3000))]),
    ("NYダウ", [(YH, lambda: fmt_close(yahoo("^DJI")["rows"], "ドル", "ドル", 2)),
              (ST, lambda: fmt_close(stooq("^dji")["rows"], "ドル", "ドル", 2))]),
    ("NASDAQ", [(YH, lambda: fmt_close(yahoo("^IXIC")["rows"], "", "ポイント", 2)),
                (ST, lambda: fmt_close(stooq("^ndq")["rows"], "", "ポイント", 2))]),
    ("ドル円", [(YH, lambda: fmt_live(yahoo("JPY=X"), "円", 2)),
             (ST, lambda: fmt_close(stooq("usdjpy")["rows"], "円", "円", 2))]),
    ("日本国債10年利回り", [(MOF, lambda: fmt_yield(mof_jgb10()["rows"]))]),
    ("米国債10年利回り", [(YH, lambda: fmt_yield(yahoo("^TNX")["rows"])),
                  (FR, lambda: fmt_yield(fred("DGS10")["rows"]))]),
    ("SOX（PHL半導体指数）", [(YH, lambda: fmt_close(yahoo("^SOX")["rows"], "", "ポイント", 2))]),
    ("WTI原油先物", [(YH, lambda: fmt_close(yahoo("CL=F")["rows"], "ドル", "ドル", 2))]),
    ("VIX恐怖指数", [(YH, lambda: fmt_close(yahoo("^VIX")["rows"], "", "ポイント", 2))]),
    ("騰落レシオ（東証プライム）", [(JPXS, advdec_ratios)]),
    ("日経平均ボラティリティー・インデックス（日経VI）",
     [(NK, lambda: fmt_close(nikkei_csv("nikkei_stock_average_vi_daily_jp.csv")["rows"], "", "ポイント", 2)),
      (YH, lambda: fmt_close(yahoo("^JNIV")["rows"], "", "ポイント", 2))]),
    ("Fear & Greed指数（CNN）", [(CNN, lambda: fmt_fg(cnn_fg()))]),
]


def main():
    OUT.mkdir(exist_ok=True)
    day_file = OUT / f"{TODAY.isoformat()}.json"
    old = {}
    if day_file.exists():  # 同日の再実行で、前回成功した項目を失敗で上書きしない
        old = {i["label"]: i for i in json.loads(day_file.read_text("utf-8"))["items"]}

    items = []
    for label, sources in ITEMS:
        item = {"label": label, "ok": False, "errors": []}
        if sources is SKIP:
            item["skip"] = True
            items.append(item)
            print("--  " + label + "（対象外）")
            continue
        for (src, url), fn in sources:
            try:
                value, change = fn()
                item.update(ok=True, value=value, change=change, source=src, source_url=url)
                break
            except Exception as e:
                item["errors"].append(f"{src}: {str(e)[:160]}")
        if not item["ok"] and old.get(label, {}).get("ok"):
            item = old[label]
        items.append(item)
        print(("OK  " if item["ok"] else "NG  ") + label, item.get("value", ""), item["errors"])

    lines = ["## ■ マーケット・スナップショット", ""]
    for n, i in enumerate(items, 1):
        if i.get("skip"):
            lines.append(f"{n}. **{i['label']}**｜自動取得の対象外（安定したデータ元がないため）")
        elif i["ok"]:
            lines.append(f"{n}. **{i['label']}**｜{i['value']} ／ {i['change']}")
        else:
            lines.append(f"{n}. **{i['label']}**｜未取得（自動取得に失敗）")
    used = {}
    for i in items:
        if i["ok"]:
            used.setdefault(i["source"], i["source_url"])
    sources = ["出典:"] + [f"- [{k}]({v})" for k, v in used.items()]

    doc = {
        "date": TODAY.isoformat(),
        "generated_at": NOW.isoformat(timespec="seconds"),
        "ok_count": sum(i["ok"] for i in items),
        "total": sum(not i.get("skip") for i in items),
        "items": items,
        "snapshot_markdown": "\n".join(lines),
        "sources_markdown": "\n".join(sources),
    }
    body = json.dumps(doc, ensure_ascii=False, indent=2)
    day_file.write_text(body, "utf-8")
    (OUT / "latest.json").write_text(body, "utf-8")
    print(f"\n{doc['ok_count']}/{doc['total']} 項目取得")


if __name__ == "__main__":
    main()
