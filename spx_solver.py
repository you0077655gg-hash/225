# -*- coding: utf-8 -*-
"""
حل كورس الثقافة المالية على منصة سبريكس عبر الواجهة المباشرة (API).
وضع مباشر سريع: لا انتظار، دعم الفتح التسلسلي (درس → يفتح التالي).
يدعم أكثر من مادة: لكل مادة بنك إجابات وبنك نصي خاصين بها.
"""
import http.cookiejar
import json
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request

try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:  # بيئات بدون Playwright (مثل Termux) — نشتغل بجلسة urllib خالصة
    sync_playwright = None
    _HAS_PLAYWRIGHT = False

BASE = "https://egy.app.spx-learning-square.com"
TEST_ATTEMPTS = 6
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

class _HttpSession:
    """يغلّف جلسة Playwright API (من غير متصفح) ويعيد واجهة context.request المألوفة."""
    def __init__(self, api):
        self.request = api
        self._api = api

    def __getattr__(self, name):
        return getattr(self._api, name)


class _UrllibResp:
    """استجابة بسيطة بنفس شكل استجابة Playwright (status + json())."""
    def __init__(self, status, data):
        self.status = status
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("لا توجد بيانات JSON")
        return self._data


class _UrllibRequest:
    """واجهة request مطابقة لجلسة Playwright لكن مبنية على urllib (بدون متصفح)."""
    def __init__(self, session):
        self._session = session

    def get(self, url, headers=None, **kwargs):
        return self._session.fetch("GET", url, None, headers)

    def post(self, url, data=None, headers=None, **kwargs):
        return self._session.fetch("POST", url, data, headers)

    def put(self, url, data=None, headers=None, **kwargs):
        return self._session.fetch("PUT", url, data, headers)


class _UrllibSession:
    """جلسة HTTP خالصة (urllib) بديلة عن جلسة Playwright — بدون متصفح ولا مكتبات خارجية."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.request = _UrllibRequest(self)

    def _full(self, url):
        url = str(url or "")
        return url if url.startswith("http") else BASE + url

    def fetch(self, method, url, data=None, headers=None):
        body = None
        if data is not None and method != "GET":
            body = data.encode("utf-8") if isinstance(data, str) else data
        req = urllib.request.Request(self._full(url), data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=30) as r:
                raw = r.read()
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    parsed = None
                return _UrllibResp(r.status, parsed)
        except urllib.error.HTTPError as e:
            return _UrllibResp(e.code, None)
        except Exception:
            return _UrllibResp(0, None)

    def dispose(self):
        pass
# المواد المدعومة. لكل مادة ملفاها الخاصان حتى لا تختلط أرقام الأسئلة بينها.
SUBJECTS = {
    "7": {
        "name": "الثقافة المالية (عربي)",
        "bank": "spx_api_bank.json",
        "text": "answers_archive.json",
    },
    "3": {
        "name": "Financial Literacy (لغات)",
        "bank": "spx_api_bank_subject3.json",
        "text": "answers_archive_subject3.json",
    },
}
DEFAULT_SUBJECT_ID = "7"
SUBJECT_ID = DEFAULT_SUBJECT_ID  # يبقى متاحًا للتوافق مع أي استدعاء قديم
# مسارات احتياطية قابلة للدمج من الأداة القديمة لو وُجدت (اختياري) — للمادة الأساسية فقط
SEED_BANK = os.path.normpath(
    os.path.join(APP_DIR, "..", "اداة مساعدة", "مخرجات", "spx_api_bank.json")
)
TEXT_BANK_FALLBACK = os.path.normpath(
    os.path.join(APP_DIR, "..", "اداة مساعدة", "مخرجات", "إجابات_الاختبارات.json")
)


def subject_conf(subject_id):
    """إعدادات المادة، أو إعدادات افتراضية لمادة غير مسجّلة."""
    sid = str(subject_id or DEFAULT_SUBJECT_ID)
    return SUBJECTS.get(sid) or {
        "name": "مادة %s" % sid,
        "bank": "spx_api_bank_%s.json" % sid,
        "text": "answers_archive_%s.json" % sid,
    }


def _norm_key(text):
    """تطبيع نص السؤال/الإجابة للمطابقة: إزالة التشكيل، توحيد الألف/الياء، وتفريغ الفراغات."""
    if not text:
        return ""
    s = str(text)
    try:
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    except Exception:
        pass
    s = (s.replace("\u0623", "\u0627")
         .replace("\u0625", "\u0627")
         .replace("\u0622", "\u0627")
         .replace("\u0649", "\u064a"))
    return " ".join(s.split()).strip()


def _read_bank_file(path, slot, log):
    """يملأ خانة مادة من ملف بنك. مفاتيح الاختبارات تُحفظ كأرقام لأنها كما تأتي من الـ API."""
    try:
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        n = 0
        for k, v in (data.get("test") or {}).items():
            if v is None:
                continue
            try:
                slot["test"][int(k)] = v
            except (TypeError, ValueError):
                continue
            n += 1
        for k, v in (data.get("block") or {}).items():
            if v is None:
                continue
            slot["block"][str(k)] = v
            n += 1
        return n
    except Exception as e:
        log(f"تعذر قراءة بنك {os.path.basename(path)}: {e}", "warning")
        return 0


def _read_text_file(path, text_bank, log):
    try:
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            arch = json.load(f)
        cnt = 0
        for lesson in arch.get("lessons") or []:
            for q in lesson.get("questions") or []:
                atext = ((q.get("answer") or {}).get("text") or "").strip()
                nq = _norm_key(q.get("question"))
                if nq and atext:
                    text_bank[nq] = (_norm_key(atext), atext)
                    cnt += 1
        return cnt
    except Exception as e:
        log(f"تعذر قراءة البنك النصي {os.path.basename(path)}: {e}", "warning")
        return 0


def _load_banks(store, log):
    """يحمّل بنوك كل المواد مرة واحدة فقط."""
    for sid, cfg in SUBJECTS.items():
        slot = store.slot(sid)
        paths = [os.path.join(APP_DIR, cfg["bank"])]
        if sid == DEFAULT_SUBJECT_ID:
            paths.append(SEED_BANK)
        n = 0
        for p in paths:
            n += _read_bank_file(p, slot, log)
        t = store.text(sid)
        tpaths = [os.path.join(APP_DIR, cfg["text"])]
        if sid == DEFAULT_SUBJECT_ID:
            tpaths.append(TEXT_BANK_FALLBACK)
        tcnt = 0
        for tp in tpaths:
            tcnt = _read_text_file(tp, t, log)
            if tcnt:
                break
        if n or tcnt:
            log(
                f"المادة {sid} ({cfg['name']}): {len(slot['test'])} إجابة اختبار + "
                f"{len(slot['block'])} إجابة درس + {tcnt} سؤال نصي.",
                "info",
            )


class BankStore:
    """بنك مشترك آمن بين كل الحسابات المتوازية، بمقصد منفصل لكل مادة.

    الإجابات تُفهرس بـ question_id وهو رقم خاص بالمادة — لذلك لا يجوز
    خلط مادة بأخرى، وإلا رُشّحت إجابات مادة على أسئلة مادة أخرى.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = {}
        self.texts = {}
        self._loaded = False

    def slot(self, subject_id):
        return self.slots.setdefault(str(subject_id or DEFAULT_SUBJECT_ID), {"test": {}, "block": {}})

    def text(self, subject_id):
        return self.texts.setdefault(str(subject_id or DEFAULT_SUBJECT_ID), {})

    def load(self, log):
        with self.lock:
            if not self._loaded:
                self._loaded = True
                _load_banks(self, log)

    def save(self, log):
        with self.lock:
            for sid, slot in self.slots.items():
                if not slot["test"] and not slot["block"]:
                    continue
                path = os.path.join(APP_DIR, subject_conf(sid)["bank"])
                # ندمج أحدث ما في الملف أولًا (لو أداة/عملية تانية شغّالة في نفس
                # الوقت) قبل الكتابة — عشان ما نمسحش إجابات تعلّمتها العملية الأخرى.
                try:
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            disk = json.load(f)
                        if isinstance(disk, dict):
                            for k, v in (disk.get("test") or {}).items():
                                if v is None:
                                    continue
                                try:
                                    slot["test"].setdefault(int(k), v)
                                except (TypeError, ValueError):
                                    continue
                            for k, v in (disk.get("block") or {}).items():
                                if v is not None:
                                    slot["block"].setdefault(str(k), v)
                except Exception:
                    pass
                body = json.dumps({"test": slot["test"], "block": slot["block"]}, ensure_ascii=False)
                try:
                    # لا نكتب إن لم يتغير المحتوى — حتى لا نمسّ ملف بيانات بلا داعٍ.
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            if f.read().strip() == body.strip():
                                continue
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(body)
                    os.replace(tmp, path)
                except Exception as e:
                    log(f"تعذر حفظ بنك المادة {sid}: {e}", "warning")


class Solver:
    def __init__(self, log=None, stop=None, shared=None, subject_id=None, turbo=True):
        self.log = log or (lambda *a: None)
        self.is_stopped = stop or (lambda: False)
        self.shared = shared if shared is not None else BankStore()
        self.subject_id = str(subject_id or DEFAULT_SUBJECT_ID)
        self.subject_name = subject_conf(self.subject_id)["name"]
        self.bank = self.shared.slot(self.subject_id)
        self.text_bank = self.shared.text(self.subject_id)
        self.quiz_count = 0
        self.solved = 0
        self.on_progress = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.turbo = bool(turbo)  # وضع السرعة القصوى: جلسة HTTP من غير متصفح

    # ------------------------------------------------------------------
    # البنك
    # ------------------------------------------------------------------
    def _use_subject(self, subject_id):
        self.subject_id = str(subject_id or DEFAULT_SUBJECT_ID)
        self.subject_name = subject_conf(self.subject_id)["name"]
        self.bank = self.shared.slot(self.subject_id)
        self.text_bank = self.shared.text(self.subject_id)

    def _bank_load(self):
        self.shared.load(self.log)
        self._use_subject(self.subject_id)

    def _bank_save(self):
        self.shared.save(self.log)

    # ------------------------------------------------------------------
    # أدوات API
    # ------------------------------------------------------------------
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    @staticmethod
    def _ok(resp):
        try:
            d = resp.json()
        except Exception:
            d = None
        if not isinstance(d, dict):
            return None, resp.status
        return d, resp.status

    @staticmethod
    def _opts_for(payload, quest_id):
        opts = payload.get("options") or {}
        if str(quest_id) in opts:
            return opts[str(quest_id)]
        if quest_id in opts:
            return opts[quest_id]
        for v in opts.values():
            if v and v[0].get("question_id") == quest_id:
                return v
        return None

    def _match_test_answer(self, payload, quest_id, opts):
        qmap = payload.get("questions") or {}
        text = qmap.get(str(quest_id)) or qmap.get(quest_id) or ""
        nq = _norm_key(text)
        if not nq or nq not in self.text_bank:
            return None
        n_ans, _ = self.text_bank[nq]
        for o in opts:
            if _norm_key(o.get("text")) == n_ans:
                return o["id"]
        return None

    # ------------------------------------------------------------------
    # اكتشاف مواد الحساب (قبل تشغيل المتصفح)
    # ------------------------------------------------------------------
    def _check_subject(self):
        """يتأكد أن الحساب له صلاحية على المادة المختارة قبل بدء الشغل."""
        r = self.context.request.get(
            BASE + f"/api/v1/subjects/{self.subject_id}/curriculum", headers=self._headers()
        )
        d, status = self._ok(r)
        if status == 403 or not (d or {}).get("success"):
            err = (d or {}).get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            self.log(
                f"الحساب لا يملك صلاحية على المادة {self.subject_id} ({self.subject_name})"
                f"{f': {msg}' if msg else ''} — جرّب مادة أخرى.",
                "error",
            )
            return False
        return True

    def _launch(self):
        if self.turbo or not _HAS_PLAYWRIGHT:
            # وضع السرعة القصوى: جلسة HTTP خفيفة (نفس واجهة context.request — أسرع بكثير من متصفح)
            # (ولو Playwright غير متاح — زي Termux — نستخدم urllib مباشرة)
            self.turbo = True
            self.context = _UrllibSession()
            self.log("وضع السرعة القصوى: جلسة HTTP سريعة (بدون متصفح).", "info")
            return
        self.playwright = sync_playwright().start()
        self._launch_browser()

    def _launch_browser(self):
        if not _HAS_PLAYWRIGHT:
            raise RuntimeError("تشغيل المتصفح غير متاح في هذه البيئة (Playwright غير مثبّت).")
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-infobars"]
        # وضع السرعة لا يشغّل Playwright أصلًا — لازم نبدأه هنا قبل التحويل للمتصفح.
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        last_err = None
        for channel, label in (("chrome", "Chrome"), ("msedge", "Edge"), (None, "Chromium المدمج")):
            try:
                if channel:
                    self.browser = self.playwright.chromium.launch(
                        channel=channel, headless=True, args=args
                    )
                else:
                    self.browser = self.playwright.chromium.launch(headless=True, args=args)
                self.log(f"تم تشغيل المتصفح ({label}).", "info")
                break
            except Exception as e:
                last_err = e
        if self.browser is None:
            raise RuntimeError(f"تعذر تشغيل المتصفح: {last_err}")
        self.context = self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 820},
            locale="ar-EG",
        )

    def cleanup(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.context is not None and self.browser is None:
                self.context.dispose()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.browser = None
        self.context = None

    # ------------------------------------------------------------------
    # تسجيل الدخول (مع احتمالية الرجوع للمتصفح لو الموقع رفض الجلسة السريعة)
    # ------------------------------------------------------------------
    def _login_api(self, code, password):
        """طلب تسجيل الدخول عبر HTTP. يعيد (json, status) أو (None, 0) عند خطأ شبكة."""
        try:
            resp = self.context.request.post(
                BASE + "/api/v1/auth/login",
                headers=self._headers(),
                data=json.dumps({"login_id": code, "password": password}),
            )
            return self._ok(resp)
        except Exception:
            return None, 0

    @staticmethod
    def _should_retry_browser(status, d):
        """نعيد المحاولة بمتصفح فقط لو الموقع حظر الجلسة — وليس لو البيانات نفسها غلط."""
        if status == 0:
            return True  # خطأ شبكة/حظر تام
        if status in (403, 429, 503):
            return True  # حظر ظاهر من الموقع
        return d is None  # رد غير JSON — غالبًا صفحة حظر/كابتشا

    def _switch_to_browser(self):
        """يتحول من جلسة HTTP إلى متصفح حقيقي لإعادة المحاولة."""
        if not _HAS_PLAYWRIGHT:
            return
        try:
            if self.context is not None and self.browser is None:
                self.context.dispose()
        except Exception:
            pass
        self.context = None
        self._launch_browser()

    # ------------------------------------------------------------------
    # الجري الرئيسي
    # ------------------------------------------------------------------
    def run(self, code, password, subject=None):
        started = time.time()
        try:
            self.log("بدء وضع الحل المباشر عبر API...", "info")
            if subject:
                self._use_subject(subject)
            self.log(f"المادة المطلوبة: {self.subject_name} (رقم {self.subject_id}).", "info")
            self._bank_load()
            self._launch()
            if self.is_stopped():
                return False, "أوقفت العملية قبل البدء."

            d, status = self._login_api(code, password)
            if (
                self.turbo
                and self.browser is None
                and not (d or {}).get("success")
                and self._should_retry_browser(status, d)
            ):
                self.log("الموقع رفض الجلسة السريعة — إعادة المحاولة بمتصفح.", "warning")
                self._switch_to_browser()
                d, status = self._login_api(code, password)
            if not (d or {}).get("success"):
                err = (d or {}).get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                self.log(f"فشل تسجيل الدخول: {msg or 'بيانات غير صحيحة'}", "error")
                return False, "فشل تسجيل الدخول — تحقق من الكود وكلمة المرور."
            self.log("تم تسجيل الدخول بنجاح.", "success")
            if not self._check_subject():
                return False, f"لا تملك صلاحية على مادة «{self.subject_name}»."

            failed = set()
            first_pass = True

            while not self.is_stopped():
                r = self.context.request.get(
                    BASE + f"/api/v1/subjects/{self.subject_id}/curriculum",
                    headers=self._headers(),
                )
                d2, _ = self._ok(r)
                data = (d2 or {}).get("data") or {}
                units = data if isinstance(data, list) else data.get("units") or []

                total = 0
                done = 0
                pending = []
                for u in units:
                    items = u.get("items") or []
                    total += len(items)
                    for it in items:
                        if it.get("is_completed"):
                            done += 1
                    for it in items:
                        if it.get("is_unlocked") and not it.get("is_completed"):
                            if (it.get("item_type"), it.get("id")) not in failed:
                                pending.append((u.get("id"), it))
                if self.on_progress:
                    try:
                        self.on_progress(
                            done, total,
                            (pending[0][1].get("title") or "") if pending else "",
                        )
                    except Exception:
                        pass

                if not pending:
                    if first_pass:
                        self.log("لا بنود مفتوحة الآن — لم يظهر شيء. سأنهي الجلسة.", "warning")
                    else:
                        self.log("تم الانتهاء من كل البنود المفتوحة.", "success")
                    self.log(
                        f"الملخص [{self.subject_name}]: حل {self.solved} بندًا، "
                        f"أجريت {self.quiz_count} اختبار، في {time.time() - started:.1f} ثانية.",
                        "success",
                    )
                    return True, f"اكتمل حل كل البنود في {self.subject_name}."
                first_pass = False

                # نحل كل البنود المفتوحة دفعة واحدة قبل إعادة طلب المنهج (أقل طلبات وأسرع)
                for uid, it in pending:
                    if self.is_stopped():
                        break
                    itype = it.get("item_type")
                    iid = it.get("id")
                    title = (it.get("title") or f"{itype} {iid}").strip()
                    self.log(f"جاري حل: {title}", "info")

                    try:
                        if itype == "chapter":
                            ok = self._solve_chapter(iid)
                        elif itype == "review":
                            ok = self._solve_review(iid)
                        elif itype == "test":
                            ok = self._solve_test(iid)
                        else:
                            ok = False
                            self.log(f"نوع بند غير مدعوم: {itype} — تجاوز.", "warning")
                    except Exception as e:
                        if self.is_stopped():
                            break
                        self.log(f"تعذر حل {itype} #{iid}: {str(e)[:120]}", "error")
                        failed.add((itype, iid))
                        self._bank_save()
                        continue

                    if ok:
                        self.solved += 1
                    else:
                        failed.add((itype, iid))
                    self._bank_save()

            self.log("تم إيقاف العملية بطلب منك — جرى حفظ ما اكتمل.", "info")
            return False, "أوقفت العملية — حفظ ما اكتمل."
        except Exception as e:
            self.log(f"خطأ غير متوقع: {str(e)[:200]}", "error")
            return False, f"حدث خطأ: {str(e)[:150]}"
        finally:
            self.cleanup()
            self.log("تم إغلاق الجلسة بأمان.", "info")

    # ------------------------------------------------------------------
    # حل أنواع البنود
    # ------------------------------------------------------------------
    def _solve_chapter(self, cid):
        r = self.context.request.get(BASE + f"/api/v1/chapters/{cid}/blocks", headers=self._headers())
        d, _ = self._ok(r)
        data = (d or {}).get("data") or {}
        blocks = data.get("blocks") or []
        qblocks = [b for b in blocks if b.get("question_id")]
        if not qblocks:
            self.log(f"درس #{cid}: لا أسئلة داخلية — سيُكمل مباشرة.", "info")
        for b in qblocks:
            opts = b.get("options") or []
            if not opts:
                continue
            key = str(b["id"])
            # نبدأ بالإجابة المحفوظة، ولو فشلت نجرّب كل الخيارات حتى لا نتعطّل.
            known = self.bank["block"].get(key)
            trials = ([known] if known is not None else []) + [o["id"] for o in opts]
            seen = set()
            solved_q = False
            for oid in trials:
                if oid is None or oid in seen:
                    continue
                seen.add(oid)
                if self.is_stopped():
                    return False
                ra = self.context.request.post(
                    BASE + f"/api/v1/chapters/{cid}/answer",
                    headers=self._headers(),
                    data=json.dumps({"block_id": b["id"], "selected_option_id": oid, "hint_used": 0}),
                )
                da, _ = self._ok(ra)
                if da and da.get("data", {}).get("is_correct"):
                    if known is not None and known != oid:
                        self.log(f"تصحيح إجابة محفوظة لكتلة {key}.", "warning")
                    self.bank["block"][key] = oid
                    solved_q = True
                    break
            if not solved_q:
                self.log(f"لم تُجب كتل الدرس #{cid} كلها — لمتابعة على أي حال.", "warning")
        rc = self.context.request.post(BASE + f"/api/v1/chapters/{cid}/complete", headers=self._headers(), data="{}")
        dc, _ = self._ok(rc)
        ok = bool(dc and dc.get("data", {}).get("completed"))
        if ok:
            star = dc["data"].get("star_score")
            self.log(f"درس اكتمل: #{cid} (نجوم {star}).", "success")
        else:
            msg = (dc or {}).get("error") or {}
            self.log(f"تعذر إكمال الدرس #{cid}: {msg.get('message') if isinstance(msg, dict) else msg}", "error")
        return ok

    def _solve_review(self, rid):
        r = self.context.request.get(BASE + f"/api/v1/reviews/{rid}/items", headers=self._headers())
        d, _ = self._ok(r)
        items = (((d or {}).get("data") or {}).get("items")) or []
        r2 = self.context.request.post(BASE + f"/api/v1/reviews/{rid}/complete", headers=self._headers(), data="{}")
        d2, _ = self._ok(r2)
        ok = bool(d2 and d2.get("data", {}).get("completed"))
        if ok:
            self.log(f"مراجعة اكتملت: #{rid} ({len(items)} بطاقة).", "success")
        else:
            msg = (d2 or {}).get("error") or {}
            self.log(f"تعذر إكمال المراجعة #{rid}: {msg.get('message') if isinstance(msg, dict) else msg}", "error")
        return ok

    def _solve_test(self, tid):
        attempts = 0
        while attempts < TEST_ATTEMPTS and not self.is_stopped():
            attempts += 1
            r = self.context.request.post(BASE + f"/api/v1/tests/{tid}/start", headers=self._headers(), data="{}")
            d, _ = self._ok(r)
            data = (d or {}).get("data")
            if not data:
                self.log(f"تعذر بدء الاختبار #{tid}: {(d or {}).get('error', r.status)}", "error")
                break
            resp = data.get("response") or {}
            rid_attempt = resp.get("id")
            tq_list = data.get("test_questions") or []
            unknown = 0
            for qi, tq in enumerate(tq_list):
                qid = tq.get("id")
                opt = self.bank["test"].get(qid)
                if opt is None:
                    opts = self._opts_for(data, tq.get("question_id")) or []
                    if not opts:
                        continue
                    unknown += 1
                    opt = self._match_test_answer(data, tq.get("question_id"), opts)
                    if opt is None:
                        # سؤال جديد: نبدّل الخيارات بين المحاولات بدل تكرار الأول دائمًا،
                        # فتتسع دائرة التعلّم مع كل محاولة.
                        opt = opts[(attempts - 1 + qi) % len(opts)]["id"]
                if opt is None:
                    continue
                self.context.request.put(
                    BASE + f"/api/v1/tests/{tid}/questions/{qid}/answer",
                    headers=self._headers(),
                    data=json.dumps({"selected_option_id": opt}),
                )
            rs = self.context.request.post(BASE + f"/api/v1/tests/{tid}/submit", headers=self._headers(), data="{}")
            ds, _ = self._ok(rs)
            sdata = (ds or {}).get("data") or {}
            passed = bool((sdata.get("response") or {}).get("passed"))
            score = (sdata.get("response") or {}).get("total_score")

            learned = 0
            if rid_attempt:
                try:
                    rr = self.context.request.get(
                        BASE + f"/api/v1/tests/{tid}/result?response_id={rid_attempt}",
                        headers=self._headers(),
                    )
                    dr, _ = self._ok(rr)
                    rdata = (dr or {}).get("data") or {}
                    for tq in rdata.get("test_questions") or []:
                        opts = self._opts_for(rdata, tq.get("question_id"))
                        if opts:
                            for o in opts:
                                if o.get("is_correct"):
                                    if tq.get("id") not in self.bank["test"]:
                                        learned += 1
                                    self.bank["test"][tq.get("id")] = o["id"]
                                    break
                except Exception:
                    pass
            if learned:
                self.log(f"تعلّمت {learned} إجابة جديدة للاختبار #{tid} — حُفظت في بنك المادة.", "info")
                self._bank_save()

            if passed:
                self.quiz_count += 1
                self.log(f"اختبار اجتاز بنجاح بدرجة {score} في محاولة {attempts}.", "success")
                return True
            self.log(
                f"الاختبار #{tid} لم يجتز (درجة {score}) — محاولة {attempts}/{TEST_ATTEMPTS}"
                + (f" — {unknown} سؤال بلا إجابة محفوظة." if unknown else "."),
                "warning",
            )
        self.log(
            f"فشل اجتياز الاختبار #{tid} بعد {attempts} محاولات — أعد تشغيل الأداة فسيكمل من البنك.",
            "error",
        )
        return False


class _Http:
    """جلسة HTTP خفيفة بدون متصفح — تُستخدم لعرض مواد الحساب في الواجهة."""

    def __init__(self):
        self._op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def json(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            BASE + path,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": USER_AGENT},
            method=method,
        )
        try:
            with self._op.open(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"success": False, "error": "http %s" % e.code}
        except Exception as e:
            return {"success": False, "error": str(e)}


def list_subjects(code, password):
    """يسجّل الدخول ويجلب مواد الحساب — لملء قائمة المادة في الواجهة.

    يرجع (قائمة المواد، رسالة الخطأ)."""
    h = _Http()
    d = h.json("POST", "/api/v1/auth/login", {"login_id": code, "password": password})
    if not (d or {}).get("success"):
        err = (d or {}).get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err or "")
        return [], msg or "بيانات الدخول غير صحيحة"
    out = []
    for s in (h.json("GET", "/api/v1/subjects") or {}).get("data") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id"))
        out.append({
            "id": sid,
            "name": subject_conf(sid)["name"] if sid in SUBJECTS else (s.get("name") or sid),
            "platform_name": s.get("name") or sid,
            "supported": sid in SUBJECTS,
            "total_items": s.get("total_items") or 0,
        })
    return out, None
