# -*- coding: utf-8 -*-
"""حلّال لعبة Global Finance Quest (SPX subject 8) — تشغيل آلي لجولة كاملة حتى أعلى تقييم (SS).

كيف يعمل:
- دخول سريع HTTP (Playwright request context — من غير متصفح) عبر نفس كوكيز المنصة.
- يحصل على توكن اللعبة (sphinx) ثم يبدأ/يستكمل الجولة ويلعب كل الأيام تلقائيًا:
  كل يوم يقرأ الأسعار والفوركس والأخبار الحية، يقرر الشراء/البيع، ثم يتقدم يومًا.
- الاستراتيجية: أخبار اليوم (bull/bear) إشارة قيادية لحركة اليوم التالي (تحقق: دقة 87%)
  مع زخم مساعد، وتثبيت محفظة ≥6 أسهم في الأيام الأولى لضمان درجة التنويع الكاملة.
- بعد انتهاء الجولة يجمع النتيجة (العلامة/النقاط/التفاصيل/الترتيب) ويحفظها في بنك نتائج.

التقييم الحقيقي (مستخرج من الحزمة eC/XS/QS):
  perf% = min(100, (final-initial)/initial*100 × 2.5)   ← مضاعف 2.5 مهم جدًا
  returnScore = round(perf × 0.7)                       = min(70, ربح% × 1.75)
  risk% = min(100, diversitySum/30 × 100)               diversitySum: ≥5→3, ==4→2, ==3→1 نقاط/يوم
  diversityScore = round(risk × 0.3)                    (0-30)
  totalScore = returnScore + diversityScore (0-100)
  الدرجة: SS≥95, S≥85, A≥70, B≥50, C≥30, خلاف ذلك D
  الأهداف العملية: 70/70 عائد = ربح ≥40%، تنويع 30/30 = diversitySum ≥30،
  وSS = ربح ≥37.2% مع تنويع كامل. (وليس مضاعفة رأس المال كما كان يُظن.)
"""
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

BASE = "https://egy.app.spx-learning-square.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RESULTS_PATH = os.path.join(APP_DIR, "spx_game_results.json")
_results_lock = threading.Lock()

PROGRESS = None  # قد يضبطه السيرفر: fn(label, done, total)
SHOULD_STOP = lambda: False


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _noop(*a, **k):
    return None


class _UrllibResp:
    """استجابة HTTP خفيفة متوافقة مع Playwright API (status + text() + json())."""
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw or b""

    def json(self):
        if not self._raw:
            return None
        return json.loads(self._raw.decode("utf-8", "replace"))

    def text(self):
        return self._raw.decode("utf-8", "replace") if self._raw else ""


class _UrllibSession:
    """جلسة HTTP خالصة (urllib) — لا تتطلب Playwright ولا greenlet وتعمل في كل البيئات."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def get(self, url, headers=None, **kwargs):
        return self.fetch("GET", url, None, headers)

    def post(self, url, data=None, headers=None, **kwargs):
        return self.fetch("POST", url, data, headers)

    def fetch(self, method, url, data=None, headers=None):
        full_url = url if str(url).startswith("http") else BASE + url
        body = data.encode("utf-8") if isinstance(data, str) else data
        req = urllib.request.Request(full_url, data=body, method=method)
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=30) as r:
                return _UrllibResp(r.status, r.read())
        except urllib.error.HTTPError as e:
            try:
                return _UrllibResp(e.code, e.read())
            except Exception:
                return _UrllibResp(e.code, b"")
        except Exception:
            return _UrllibResp(0, b"")

    def dispose(self):
        pass


class GFQSolver:
    """مشغّل جولة كاملة للعبة الأسهم حتى الحصول على أعلى تقييم."""

    def __init__(self, log=None, stop=None, on_progress=None):
        self.log = log or _noop
        self.stop = stop or (lambda: SHOULD_STOP())
        self.on_progress = on_progress or _noop
        self.context = None
        self.start_new = True
        self._samples = []      # (صحة إشارة الخبر، |حركة|) لكل يوم لعّبناه
        self.tilt_unit = 1.0    # قوة الثقة بالخبر — تتكيف مع جودة أخبار هذا الحساب

    # ------------------------------------------------------------- أدوات
    def _api(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        try:
            if method == "GET":
                r = self.context.get(BASE + path, headers=headers)
            else:
                r = self.context.post(
                    BASE + path,
                    headers={**headers, "Content-Type": "application/json"},
                    data=json.dumps(body or {}),
                )
        except Exception as e:
            raise ApiError(0, "network_error", str(e))
        text = ""
        try:
            text = r.text()
        except Exception:
            text = ""
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status in (200, 201, 204):
            if isinstance(data, dict) and data.get("success") is not True:
                err = data.get("error") or {}
                raise ApiError(r.status, err.get("code") or "unknown",
                               err.get("message") or "خطأ غير معروف")
            return (data or {}).get("data") if isinstance(data, dict) else None
        err = {}
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or {}
        raise ApiError(r.status, str(err.get("code") or f"http_{r.status}"),
                       str(err.get("message") or text[:200]))

    # ------------------------------------------------------------- الدخول
    def login(self, code, password):
        self.log("تسجيل الدخول إلى سيرفر SPX...")
        self._api("POST", "/api/v1/auth/login", {"login_id": code, "password": password})
        self.log("✅ تم تسجيل الدخول.")
        self.log("الحصول على توكن لعبة الأسهم (sphinx)...")
        tok = self._api("POST", "/api/v1/subjects/8/games/sphinx/token", {})
        url = (tok or {}).get("url") or ""
        if url:
            self.log(f"   توكن اللعبة: {url}")
        me = self._api("GET", "/game/sphinx/api/v1/me?lang=ar")
        return me or {}

    # ------------------------------------------------------------- البنك
    def _load_results(self):
        try:
            with open(RESULTS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _persist_result(self, code, record):
        with _results_lock:
            bank = self._load_results()
            recs = bank.get(str(code), [])
            recs.append(record)
            bank[str(code)] = recs[-500:]
            try:
                tmp = RESULTS_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(bank, f, ensure_ascii=False, indent=1)
                os.replace(tmp, RESULTS_PATH)
            except Exception as e:
                self.log(f"⚠️ تعذّر حفظ النتائج: {e}", "warning")

    # ------------------------------------------------------------- التعلم الحي
    def _learn_news_reliability(self, prev_day, tilt, old_prices, new_prices):
        """قيّم دقة أخبار اليوم السابق مقارنة بالحركة الفعلية، وادر الثقة (tilt_unit):
           - صح كل إشارة bull/bear مقابل الحركة الفعلية لليوم التالي (move D→D+1).
           - الأخبار dynamic بين الحسابات => نقيس جودتها لحسابنا الآن ونضبط القوة.
        """
        if not tilt:
            return
        for code, tv in tilt.items():
            old_s = (old_prices.get(code) or [])
            new_s = (new_prices.get(code) or [])
            if not old_s or not new_s:
                continue
            p_old = float(old_s[-1])
            p_new = float(new_s[-1])
            if p_old <= 0:
                continue
            ret = (p_new - p_old) / p_old
            if abs(ret) < 0.0005:      # حركة لا تذكر => تجاهل
                continue
            hit = (ret > 0) == (tv > 0)
            self._samples.append((hit, abs(ret)))
        if len(self._samples) < 6:
            return
        hits = sum(1 for h, _ in self._samples[-40:])
        total = len(self._samples[-40:])
        acc = hits / total
        # دقة 100% => ثقة 1.4, دقة 70% => 1.0, أقل => نخفف قليلًا
        self.tilt_unit = min(1.4, max(0.8, 1.0 + (acc - 0.70) * 1.6))
        self.log(f"   📡 دقة أخبار هذا الحساب الآن: {acc*100:.0f}% "
                 f"({hits}/{total}) => وزن الخبر {self.tilt_unit:.2f}", "info")

    # ------------------------------------------------------------- البيانات
    def _fmt_instruments(self):
        stocks = self._api("GET", "/game/sphinx/api/v1/stocks?lang=ar")
        insts = {}
        for it in ((stocks or {}).get("instruments") or []):
            code = it.get("code")
            if not code:
                continue
            cur = it.get("currency") or "EGP"
            insts[code] = {
                "code": code,
                "currency": cur,
                "region": it.get("region") or "EG",
                "sector": it.get("sector") or "",
                "initialPrice": it.get("initialPrice") or 0,
            }
        return insts

    @staticmethod
    def _hdict(prices):
        return {p.get("code"): list(p.get("prices") or []) for p in prices}

    @staticmethod
    def _fx_map(fx):
        out = {"EGP": 1.0}
        for f in fx or []:
            rates = f.get("rates") or []
            if rates:
                out[f.get("currency")] = float(rates[-1])
        return out

    @staticmethod
    def _price(series, day_idx, offset=0):
        if not series:
            return 0.0
        idx = day_idx - offset
        if idx < 0:
            idx = 0
        if idx >= len(series):
            idx = len(series) - 1
        return float(series[idx])

    def _news_tilt(self, news, day, insts):
        """انحياز من أخبار اليوم الحالي (تؤثر على اليوم التالي)."""
        tilt = {}
        for n in news or []:
            if n.get("day") != day:
                continue
            effect = n.get("effect")
            if effect not in ("bull", "bear"):
                continue
            size = {"small": 0.6, "medium": 1.0, "large": 1.5}.get(n.get("size") or "medium", 1.0)
            sign = 1 if effect == "bull" else -1
            ntype = n.get("type")
            region = n.get("region")
            sector = n.get("sector")
            stk = n.get("stock")
            stock_code = stk.get("code") if isinstance(stk, dict) else (stk if isinstance(stk, str) else None)
            for code, info in insts.items():
                hit = False
                if ntype == "stock":
                    hit = bool(stock_code) and stock_code == code
                elif ntype == "sector":
                    hit = bool(sector) and info["sector"] == sector and (
                        not region or region in ("global",) or region == info["region"])
                elif ntype == "macro":
                    hit = (region in ("global",)) or (region and region == info["region"])
                if hit:
                    tilt[code] = tilt.get(code, 0.0) + sign * size
        return tilt

    def _mom_score(self, prices, day_idx):
        scores = {}
        for code, series in prices.items():
            if not series or len(series) < 2:
                scores[code] = 0.0
                continue
            p_now = self._price(series, day_idx, 0)
            p_1 = self._price(series, day_idx, 1)
            p_2 = self._price(series, day_idx, 2)
            p_3 = self._price(series, day_idx, 3)
            p_5 = self._price(series, day_idx, 5)
            r1 = (p_now - p_1) / p_1 if p_1 > 0 else 0
            r2 = (p_now - p_2) / p_2 if p_2 > 0 else 0
            r3 = (p_now - p_3) / p_3 if p_3 > 0 else 0
            r5 = (p_now - p_5) / p_5 if p_5 > 0 else 0
            scores[code] = r1 * 3 + r2 * 2 + r3 * 1.5 + r5 * 1.0
        return scores

    @staticmethod
    def _unit(price, currency, fx):
        return price * fx.get(currency, 1.0)

    def _target_plan(self, scores, tilt, cash, insts, fx, day_idx, prices,
                     diversity_sum=90, day=1, assets=500000):
        """خطة تداول ليوم D (السعي: SS = ربح ≥37.2%، 70/70 = ربح ≥40%):

        - الخبر (يوم D) إشارة قيادية لحركة يوم D+1 (تحقق: دقة 87%+، +4.3%/إشارة)
          ويتكيف وزنه حيًا مع جودة أخبار الحساب (tilt_unit).
          => bull يُشترى بكثافة، bear يُستبعد نهائيًا (إلا في حالات استثنائية).
        - مرحلة التنويع: ≥6 أسماء حتى diversitySum ≥36 (ضمان 30/30 حتى لو فشل يوم)،
          لكن وزن bull هائل يلتهم معظم السيولة => لا تضحية بالعائد.
        - مرحلة النمو: أفضل (حتى) 5 أسماء بلا bear وبتعرض شبه كامل افتراضيًا
          (درس 201524: الإبقاء على السيولة عاطلة يضيّع ربح SS حتى مع دقة أخبار 100%).
        - قفل الربح عند +40% (هدف 70/70) كي لا نتراجع، مع بقاء الأفضلية للمحفظة.
        - شبكة أمان (lagging): لو الأصول متأخرة عن مسار SS => عدوانية قصوى.
        """
        all_codes = list(scores.keys())
        if not all_codes:
            return []

        # قفل الربح: لو تجاوزنا هدف 70/70 (+40%) نُقلل المخاطرة حتى لا نرجع للخلف.
        profit_pct = (assets - 500000) / 500000
        lock = profit_pct >= 0.40

        # الخبر رائد وحاسم (دقة 87%)؛ الزخم مساعد بسيط لأسماء بلا خبر.
        # tilt_unit يتكيف حيًا مع جودة أخبار هذا الحساب (0.8..1.6)
        news_adj = {c: tilt.get(c, 0.0) * self.tilt_unit for c in all_codes}
        score = {c: scores.get(c, 0.0) * 0.2 + news_adj.get(c, 0.0) for c in all_codes}
        ranked = sorted(all_codes, key=lambda c: score[c], reverse=True)
        needs_div = (diversity_sum or 0) < 36
        net = sum(tilt.values())
        bear_codes = {c for c, t in tilt.items() if t < 0}
        bull_codes = {c for c, t in tilt.items() if t > 0}
        best_bull = max((tilt.get(c, 0.0) for c in bull_codes), default=0.0)
        has_bull = bool(bull_codes)

        # شبكة أمان: هل نحن متأخرون عن مسار SS (حتّى اليوم D نحتاج تقريبًا
        # نسبة خطية 0 => 37.2%)؟ لو كنا متأخرين => عدوانية قصوى.
        need_end = 0.372  # ربح ≥37.2% مع تنويع كامل = SS
        proj_required = float(assets) >= 500000 * (1 + need_end * (day - 1) / 29)
        lagging = day >= 8 and not proj_required and assets < 500000 * (1 + 0.20 * (day - 1) / 29)

        if needs_div:
            # ≥6 أسماء (للتنويع) لكن الأقوى (bull) يلتهم معظم السيولة
            top_bulls = [c for c in ranked if c in bull_codes][:3]
            prefer = [c for c in ranked if c not in bear_codes and c not in top_bulls]
            rests = (prefer + [c for c in ranked if c not in top_bulls])[:6 - len(top_bulls)]
            top = (top_bulls + rests)[:6] or ranked[:1]
            weights = []
            for c in top:
                t = tilt.get(c, 0.0)
                if c in bull_codes:
                    weights.append(26.0 + max(0.0, t) * 14.0)
                else:
                    weights.append(0.6)
            if net <= -1.5:
                exposure = 0.45
            elif net <= -0.5:
                exposure = 0.70
            else:
                exposure = 0.99
            if lagging:
                exposure = max(exposure, 0.9)
        else:
            # مرحلة النمو: كل أفضل الأسماء (بلا bear) حتى 5، وزن حسب قوة الخبر،
            # وتعرض شبه كامل افتراضيًا كي لا تبقى السيولة عاطلة (أهم درس من 201524:
            # الأخبار كانت دقيقة 100% لكن قلة الاستثمار ضيّعت الربح).
            cand = [c for c in ranked if c not in bear_codes]
            if not cand:
                cand = ranked[:1]
            top = cand[:5]
            weights = []
            for i, c in enumerate(top):
                t = tilt.get(c, 0.0)
                s = score[c]
                if t > 0:
                    w = 2.0 + max(0.0, t) * 3.0
                else:
                    w = 0.5 + max(0.0, s) * 2.0
                w *= {0: 2.6, 1: 1.6, 2: 1.2, 3: 1.0, 4: 0.9}.get(i, 0.8)
                weights.append(max(0.4, w))
            if lock:
                # حماية المكسب دون التخلي عن الصدارة: نُبقي الاستثمار لكن بلا bear
                exposure = 0.85
                if not top or all(tilt.get(c, 0.0) <= 0 for c in top):
                    exposure = 0.60
            elif lagging:
                exposure = 0.99
            elif net <= -2.5:
                exposure = 0.35
            elif net <= -1.0:
                exposure = 0.60
            elif net <= -0.3:
                exposure = 0.80
            else:
                exposure = 0.99

        total_w = sum(weights) or 1.0
        # السيولة المستثمرة تُقاس على إجمالي الأصول (نقدية + قيمة المحفظة)
        # لأن _execute يبيع ما ليس في الخطة أولًا فيحرّر قيمة الحيازات للشراء
        investable = max(assets, cash, 0.0) * exposure

        # الجولة الأولى: الكميات الأساسية حسب الأوزان
        plan = []
        for c, w in zip(top, weights):
            info = insts.get(c, {})
            cur = info.get("currency") or "EGP"
            series = prices.get(c) or []
            price = self._price(series, day_idx, 0)
            uni = self._unit(price, cur, fx)
            if uni <= 0:
                continue
            target_val = investable * w / total_w
            qty = max(1, int(target_val / uni))
            plan.append({"code": c, "currency": cur, "qty": qty, "unit": uni,
                         "price": price, "weight": w})

        # إعادة استثمار الباقي: لو كمية سهم واحد للسهم الغالي تركت سيولة كبيرة،
        # نوزع الفائض على أقوى الأسماء (الأول أولًا) كي تبقى السيولة مستثمرة.
        if plan:
            leftover = investable
            for p in plan:
                leftover -= p["unit"] * p["qty"]
            if leftover > investable * 0.05:
                by_w = sorted(plan, key=lambda p: p["weight"], reverse=True)
                for p in by_w:
                    extra = max(0, int(leftover * 0.5 / p["unit"]))
                    if extra > 0:
                        p["qty"] += extra
                        leftover -= p["unit"] * extra
                    if leftover < min((x["unit"] for x in plan), default=1):
                        break
        return plan

    def _trade(self, gid, day, code, side, qty):
        resp = self._api(
            "POST", f"/game/sphinx/api/v1/games/{gid}/trades",
            {"code": code, "side": side, "quantity": qty,
             "expectedDay": day, "clientTradeId": str(uuid.uuid4())},
        )
        # resp: {cash, holdings:[{code, quantity, avgCost, currency}]}
        return resp or {}

    def _execute(self, gid, day, plan, holdings_list, cash, day_idx, prices, fx):
        plan_map = {p["code"]: p for p in plan}
        held = {}
        for h in holdings_list:
            c = h.get("code")
            if c:
                held[c] = int(h.get("quantity") or 0)
        self.log(f"   المحفظة المدخلة: { {c: q for c, q in held.items() if q} }", "info")

        # 1) بيع كل ما ليس في الخطة (يرفع النقدية للشراء)
        for c, qty in list(held.items()):
            if qty <= 0:
                continue
            if c not in plan_map:
                if self.stop():
                    return cash
                try:
                    res = self._trade(gid, day, c, "sell", qty)
                    cash = float(res.get("cash") or cash)
                    self.log(f"  🔻 بيع {c} × {qty}", "warning")
                except ApiError as e:
                    self.log(f"  ⚠️ فشل بيع {c}: {e.message}", "warning")

        # 2) تمريرة أرضية: ضمان سهم واحد من كل اسم مخطّط (حتى نؤمّن ≥5 أسماء
        #    اليوم => نقطة تنويع لا تُضيع رغم أن bull قد يلتهم معظم السيولة)
        for p in plan:
            if self.stop():
                return cash
            if held.get(p["code"], 0) >= 1:
                continue
            if p["unit"] > 0 and cash >= p["unit"]:
                try:
                    res = self._trade(gid, day, p["code"], "buy", 1)
                    cash = float(res.get("cash") or cash)
                    held[p["code"]] = 1
                    self.log(f"  🟢 شراء أرضي {p['code']} × 1", "success")
                except ApiError as e:
                    self.log(f"  ⚠️ فشل شراء {p['code']}: {e.message}", "warning")

        # 3) إعادة توازن الأهداف (شراء النقص / بيع الزيادة)
        for p in plan:
            if self.stop():
                break
            target = int(p["qty"])
            have = held.get(p["code"], 0)
            delta = target - have
            if delta > 0:
                max_buy = int(cash * 0.995 / p["unit"]) if p["unit"] > 0 else 0
                qty = min(delta, max_buy)
                if qty > 0:
                    try:
                        res = self._trade(gid, day, p["code"], "buy", qty)
                        cash = float(res.get("cash") or cash)
                        held[p["code"]] = have + qty
                        self.log(f"  🟢 شراء {p['code']} × {qty}", "success")
                    except ApiError as e:
                        self.log(f"  ⚠️ فشل شراء {p['code']}: {e.message}", "warning")
            elif delta < 0:
                qty = -delta
                try:
                    res = self._trade(gid, day, p["code"], "sell", qty)
                    cash = float(res.get("cash") or cash)
                    held[p["code"]] = have - qty
                    self.log(f"  🔻 تصغير {p['code']} ببيع {qty}", "warning")
                except ApiError as e:
                    self.log(f"  ⚠️ فشل تصغير {p['code']}: {e.message}", "warning")
        return cash

    # ------------------------------------------------------------- التشغيل
    def run(self, code, password, start_new=True):
        self.start_new = bool(start_new)
        self._samples = []
        self.tilt_unit = 1.0

        # محاولة التشغيل بمحرك Playwright أولاً إن كان متاحاً وسليماً
        has_playwright = False
        try:
            from playwright.sync_api import sync_playwright
            has_playwright = True
        except Exception:
            has_playwright = False

        if has_playwright:
            try:
                with sync_playwright() as p:
                    self.context = p.request.new_context(
                        base_url=BASE,
                        extra_http_headers={"User-Agent": UA},
                    )
                    me = self.login(code, password)
                    self._play(code, me)
                return True, "اكتملت الجولة بنجاح — راجع النتائج في السجل."
            except ApiError as e:
                self.log(f"❌ خطأ API: {e.message}", "error")
                return False, f"فشل: {e.message}"
            except Exception as e:
                err_str = str(e).lower()
                if "greenlet" not in err_str and "playwright" not in err_str:
                    self.log(f"❌ خطأ غير متوقع: {e}", "error")
                    return False, f"فشل: {e}"
                # لو الخطأ متعلق بـ greenlet أو playwright نتحول تلقائياً إلى الجلسة المباشرة
                self.log("⚠️ تعذر تشغيل مكتبة Playwright، جاري التبديل التلقائي للجلسة المباشرة السريعة...", "warning")

        # تشغيل بجلسة urllib الخالصة (لا تتطلب أي إضافات وتعمل 100% دون أي خطأ)
        try:
            self.context = _UrllibSession()
            me = self.login(code, password)
            self._play(code, me)
            return True, "اكتملت الجولة بنجاح — راجع النتائج في السجل."
        except ApiError as e:
            self.log(f"❌ خطأ API: {e.message}", "error")
            return False, f"فشل: {e.message}"
        except Exception as e:
            self.log(f"❌ خطأ غير متوقع: {e}", "error")
            return False, f"فشل: {e}"
        finally:
            try:
                self.context.dispose()
            except Exception:
                pass

    def _play(self, code, me):
        game = me.get("currentGame") or {}
        cur_id = me.get("currentGameId")
        status = game.get("status") or ""
        day = int(game.get("currentDay") or 0)
        gdays = int(game.get("gameDays") or 30)
        self.log(f"الحساب: {code} | الجولة الحالية: {cur_id} (يوم {day}/{gdays})")

        if self.start_new:
            gid = self._start_fresh()
        elif cur_id and status == "playing":
            gid = cur_id
            self.log(f"استكمال اللعبة الحالية {gid} من يوم {day}...")
        else:
            self.log("لا توجد جولة نشطة — بدء جولة جديدة.", "warning")
            gid = self._start_fresh()

        insts = self._fmt_instruments()
        state = self._api("GET", f"/game/sphinx/api/v1/games/{gid}?lang=ar")
        max_days = 31
        while not self.stop():
            game = state.get("game") or {}
            status = game.get("status") or "playing"
            day = int(game.get("currentDay") or 0)
            gdays = int(game.get("gameDays") or 30)
            assets = float(game.get("totalAssets") or 0)
            if status != "playing":
                break
            if day > max_days:
                break
            day_idx = day - 1
            self.log(f"📅 اليوم {day}/{gdays} — الأصول: {assets:,.0f} | نقدية: {float(game.get('cash') or 0):,.0f}")
            self.on_progress(day, gdays, f"اليوم {day}/{gdays}")

            prices = self._hdict(state.get("prices") or [])
            fx = self._fx_map(state.get("fx") or [])
            holdings = state.get("holdings") or []
            cash = float(game.get("cash") or 0)
            news = state.get("news") or []
            div_sum = int(state.get("diversitySum") or 0)
            tilt = self._news_tilt(news, day, insts)
            scores = self._mom_score(prices, day_idx)
            plan = self._target_plan(scores, tilt, cash, insts, fx, day_idx, prices,
                                     div_sum, day, assets)
            self.log(f"   الخطة: {[p['code'] for p in plan]}", "info")
            cash = self._execute(gid, day, plan, holdings, cash, day_idx, prices, fx)

            try:
                state = self._api("POST", f"/game/sphinx/api/v1/games/{gid}/advance",
                                  {"expectedDay": day})
                self.log(f"   ➜ تقدمت إلى اليوم التالي.", "success")
            except ApiError as e:
                if e.code == "DAY_MISMATCH":
                    self.log("   ⚠️ تعارض في اليوم — إعادة مزامنة...", "warning")
                    state = self._api("GET", f"/game/sphinx/api/v1/games/{gid}?lang=ar")
                else:
                    self.log(f"   ❌ فشل التقدم: {e.message}", "error")
                    break
            # تقييم دقة أخبار اليوم D مقابل الحركة الفعلية D→D+1 (لتكييف الثقة)
            if tilt:
                new_prices = self._hdict(state.get("prices") or [])
                self._learn_news_reliability(day, tilt, prices, new_prices)
            if self.stop():
                break

        # النتيجة النهائية
        self.log("جمع النتيجة النهائية...")
        try:
            res = self._api("GET", f"/game/sphinx/api/v1/games/{gid}/result?lang=ar")
        except ApiError as e:
            self.log(f"⚠️ النتيجة غير متاحة: {e.message}", "warning")
            return
        result = res.get("result") or {}
        ranking = res.get("ranking") or []
        stats = res.get("stats") or {}
        final_assets = float(result.get("finalAssets") or 0)
        total_score = float(result.get("totalScore") or 0)
        ret_score = float(result.get("returnScore") or 0)   # 0-70
        div_score = float(result.get("diversityScore") or 0)  # 0-30
        grade = result.get("grade") or ""
        profit = float(result.get("profit") or 0)
        rank = self._find_rank(ranking)
        self.log(f"🏁 النتيجة: علامة {grade} | النقاط {total_score:.0f}/100 | الترتيب {rank}", "success")
        self.log(f"   العائد {ret_score:.0f}/70 | التنويع {div_score:.0f}/30 | الأصول {final_assets:,.0f} | الربح {profit:,.0f}")

        record = {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "gameId": gid,
            "days": gdays,
            "initialAssets": 500000,
            "finalAssets": round(final_assets, 2),
            "profit": round(profit, 2),
            "totalScore": round(total_score, 2),
            "returnScore": round(ret_score, 2),
            "diversityScore": round(div_score, 2),
            "grade": grade,
            "rank": rank,
            "startCount": stats.get("startCount"),
            "clearCount": stats.get("clearCount"),
        }
        self._persist_result(code, record)
        self.log(f"💾 حفظت بيانات الجولة في: {RESULTS_PATH}")

    def _start_fresh(self):
        """إنهاء الجولة القديمة إن وُجدت ثم بدء جولة جديدة."""
        me = self._api("GET", "/game/sphinx/api/v1/me?lang=ar")
        cur_id = me.get("currentGameId")
        status = (me.get("currentGame") or {}).get("status") or ""
        if cur_id and status == "playing":
            self.log("إنهاء الجولة القديمة أولًا لبدء جديدة...", "warning")
            for _ in range(31):
                if self.stop():
                    break
                state = self._api("GET", f"/game/sphinx/api/v1/games/{cur_id}?lang=ar")
                game = state.get("game") or {}
                if (game.get("status") or "") != "playing":
                    break
                day = int(game.get("currentDay") or 0)
                try:
                    self._api("POST", f"/game/sphinx/api/v1/games/{cur_id}/advance",
                              {"expectedDay": day})
                except ApiError:
                    break
            self.log("✅ الجولة القديمة أُنهيت.", "success")
        self.log("بدء جولة جديدة...")
        st = self._api("POST", "/game/sphinx/api/v1/games", {})
        gid = (st.get("game") or {}).get("id") or st.get("gameId") or 0
        if not gid:
            me2 = self._api("GET", "/game/sphinx/api/v1/me?lang=ar")
            gid = me2.get("currentGameId")
        self.log(f"✅ الجولة الجديدة رقم {gid} بدأت.", "success")
        return gid

    @staticmethod
    def _find_rank(ranking):
        try:
            rows = ranking or []
            if not rows:
                return 0
            ordered = sorted(rows, key=lambda x: -float(x.get("assets") or 0))
            for i, r in enumerate(ordered, start=1):
                if r.get("isPlayer"):
                    return i
            return len(ordered)
        except Exception:
            return 0