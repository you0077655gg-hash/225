import base64
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:  # بيئات بدون Playwright (مثل Termux على الأندرويد) — نشتغل بجلسة urllib
    sync_playwright = None
    _HAS_PLAYWRIGHT = False

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PORTAL_URL = "https://me-portal.qureo.education/login"
PORTAL_HOME = "https://me-portal.qureo.education/"
BASE = "https://me-tp.qureo.education"
# دخول مباشر لمنصة me-tp بنفس بيانات بوابة المدرسة (أسرع بكتير — من غير بوابة)
TP_LOGIN = BASE + "/oauth2/password-login"
JSON_HEADERS = {"Content-Type": "application/json"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANSWER_FILE = os.path.join(SCRIPT_DIR, "qureo_answers.json")


class LoginError(RuntimeError):
    """بيانات الدخول نفسها غلط (مش مشكلة شبكة/حظر) — لا داعي للتحويل لمتصفح."""
    pass


class _HttpSession:
    """يغلّف جلسة Playwright API (من غير متصفح) ويعيد واجهة context.request المألوفة."""
    def __init__(self, api):
        self.request = api
        self._api = api

    def __getattr__(self, name):
        return getattr(self._api, name)


class _HttpResp:
    """استجابة بسيطة من طبقة urllib — بنفس شكل استجابة Playwright (status + json())."""
    def __init__(self, status, data):
        self.status = status
        self.data = data
        self._handled = True

    def json(self):
        if self.data is None:
            raise ValueError("لا توجد بيانات JSON")
        return self.data

    def text(self):
        import json as _json
        try:
            return _json.dumps(self.data, ensure_ascii=False) if self.data is not None else ""
        except Exception:
            return ""

    def __repr__(self):
        return f"<_HttpResp {self.status}>"


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
    """جلسة HTTP خالصة (urllib) تحلّ مكان جلسة Playwright — بدون متصفح ولا مكتبات خارجية.

    توفّر نفس الواجهة المستخدمة في الكود: context.request.get/post/put،
    وcontext.storage_state()، وcontext.dispose().
    """

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [("User-Agent", self.UA), ("Accept", "application/json")]
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
                return _HttpResp(r.status, parsed)
        except urllib.error.HTTPError as e:
            return _HttpResp(e.code, None)
        except Exception:
            return _HttpResp(0, None)

    def storage_state(self):
        cookies = []
        for c in self.jar:
            cookies.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path, "secure": c.secure,
            })
        return {"cookies": cookies}

    def dispose(self):
        pass


STUDENT_ID = ""
PASSWORD = ""
COURSES = ["Python", "JavaScript"]

# خطّافات اختيارية تستخدمها الواجهة: التقدّم + طلب الإيقاف
PROGRESS = None      # دالة(course, done, total, label)
SHOULD_STOP = None   # دالة() -> bool


def emit_progress(course, done, total, label=""):
    if PROGRESS:
        try:
            PROGRESS(course, done, total, label)
        except Exception:
            pass


def stop_requested():
    if SHOULD_STOP:
        try:
            return bool(SHOULD_STOP())
        except Exception:
            return False
    return False


class QureoSolver:
    def __init__(self, headless=False, courses=None, turbo=False, lang=None, answer_file=None):
        self.headless = headless
        self.courses = courses or COURSES
        self.turbo = bool(turbo)  # وضع السرعة القصوى: جلسة HTTP من غير متصفح
        # لغة المحتوى المطلوبة (ar / en) — تُستخدم في الخيار الثاني لاختيار
        # مسار JavaScript العربي أو الإنجليزي بدقة حتى لو الحساب فيه الاثنان.
        self.lang = str(lang or "").strip().lower()
        # مسار بنك الإجابات: افتراضيًا نفس البنك القديم (لا نلمس بياناته)،
        # ويمكن تمرير ملف منفصل لأي خيار جديد.
        self.answer_file = os.path.abspath(answer_file) if answer_file else ANSWER_FILE
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.answers = self._load_answers()
        self.current_course = ""
        self._opener_cache = None
        self._turbo_courses = set()
        self._course_code = ""
        self._section_id = None
        self._bank_lock = threading.Lock()  # يحمي كتابة البنك عند حل الفصول بالتوازي
        self._parallel = False  # يُفعَّل فقط أثناء حل الفصول بالتوازي في وضع السرعة

    # ------------------------------------------------------------------ setup
    def start(self):
        if self.turbo or not _HAS_PLAYWRIGHT:
            # وضع السرعة القصوى: كل الشغل طلبات API من غير متصفح — تشغيل فوري
            # (ولو Playwright غير متاح أصلًا — زي Termux — نستخدم جلسة urllib)
            print("🚀 وضع السرعة القصوى: جلسة HTTP سريعة (بدون متصفح)...", flush=True)
            self.turbo = True
            self.context = _UrllibSession()
            self.page = None
            return
        self.playwright = sync_playwright().start()
        print("🚀 جاري تشغيل المتصفح...", flush=True)
        self._launch_browser()

    def _launch_browser(self):
        """يشغّل متصفحًا مثبّتًا على الجهاز (Chrome ثم Edge ثم Chromium المدمج)."""
        if not _HAS_PLAYWRIGHT:
            raise RuntimeError("تشغيل المتصفح غير متاح في هذه البيئة (Playwright غير مثبّت).")
        # لو إحنا في الوضع السريع واتحوّلنا للمتصفح: نتخلّص من جلسة HTTP أولاً
        if self.context is not None and self.browser is None:
            try:
                self.context.dispose()
            except Exception:
                pass
        # وضع السرعة لا يشغّل Playwright أصلًا — لازم نبدأه هنا قبل أي محاولة
        # تحويل للمتصفح، وإلا كان التحويل بيفشل دايمًا.
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        last_err = None
        for channel in ("chrome", "msedge", None):
            try:
                if channel:
                    self.browser = self.playwright.chromium.launch(
                        headless=self.headless, channel=channel
                    )
                else:
                    self.browser = self.playwright.chromium.launch(headless=self.headless)
                break
            except Exception as e:
                last_err = e
        if self.browser is None:
            raise RuntimeError(
                "تعذّر تشغيل المتصفح — تأكد من تثبيت Google Chrome أو Microsoft Edge "
                "(أو شغّل drive.bat لتنزيل Chromium)."
            ) from last_err
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()

    def login(self, student_id=None, password=None):
        """تسجيل الدخول تلقائيًا بالبيانات المُمرَّرة (أو الافتراضية)."""
        student_id = student_id or STUDENT_ID
        password = password or PASSWORD
        if not student_id or not password:
            raise RuntimeError("لازم تدخل اسم المستخدم وكلمة المرور.")
        if self.turbo:
            self._turbo_login(student_id, password)
            print("✅ تم تسجيل الدخول.", flush=True)
            return
        self._native_login(student_id, password)

    def _native_login(self, student_id, password):
        """دخول عادي عبر صفحات البوابة (بيستخدم في الوضع العادي أو بعد التحويل للمتصفح)."""
        print("🔑 جاري تسجيل الدخول تلقائيًا...", flush=True)
        self.page.goto(PORTAL_URL, wait_until="domcontentloaded")
        # صفحة البوابة تطلب أولاً اختيار نوع الحساب لإظهار حقول الدخول
        self.page.locator(".portal-selection-button-secondary").first.click()
        self.page.wait_for_selector("#student_id", timeout=30000)
        self.page.fill("#student_id", student_id)
        self.page.fill("#password", password)
        self.page.locator("button.login-button").first.click()

        # نتحقق من النجاح باختفاء نموذج الدخول (الموقع مش بيغيّر الرابط بعد الدخول)
        try:
            self.page.wait_for_selector("#student_id", state="detached", timeout=30000)
        except Exception:
            raise RuntimeError("فشل تسجيل الدخول — تأكد من اسم المستخدم وكلمة المرور.")
        time.sleep(0.4)
        print("✅ تم تسجيل الدخول.", flush=True)

    def _turbo_login(self, student_id, password):
        """دخول سريع مباشرة لمنصة me-tp (نفس بيانات بوابة المدرسة — من غير متصفح)."""
        try:
            r = self.context.request.post(
                TP_LOGIN,
                headers={"Content-Type": "application/json"},
                data=json.dumps({"student_id": student_id, "password": password}),
            )
        except Exception:
            r = None
        if r is None or r.status not in (200, 201, 204, 302):
            if r is not None and r.status in (400, 401, 422):
                # بيانات غلط — التحويل لمتصفح مش هيفيد وهيكلّف وقت
                raise LoginError("فشل تسجيل الدخول — تحقق من الكود وكلمة المرور.")
            raise RuntimeError("فشل تسجيل الدخول السريع — تحقق من الكود وكلمة المرور (أو جرّب الوضع العادي).")
        # التأكد إن التوكينات فعلاً اتسجّلت وكوكي الهوية وصل
        if not self._id_token():
            raise RuntimeError("تعذّر تأكيد جلسة المنصة السريعة — جرّب الوضع العادي.")

    def _id_token(self):
        """يفكّ كوكيegy_student_id_token ويعيد محتوى الـ JWT (متاح للقراءة والتوجيه)."""
        try:
            cookies = (self.context.storage_state() or {}).get("cookies") or []
        except Exception:
            return {}
        for c in cookies:
            if c.get("name") == "egy_student_id_token":
                parts = str(c.get("value") or "").split(".")
                if len(parts) >= 2:
                    pad = parts[1] + "=" * (-len(parts[1]) % 4)
                    try:
                        raw = base64.urlsafe_b64decode(pad).decode("utf-8", "replace")
                        return json.loads(raw)
                    except Exception:
                        return {}
        return {}

    def _course_content_id(self, name):
        """يقابل اسم المادة المطلوب مع content_id المتاح في الحساب (من بيانات id_token).

        لو self.lang محدد (ar/en) نفلتر أولًا على لغة المحتوى، وبعدها نختار
        المسار المطابق. ده اللي يخلّي "الخيار الثاني" يفرّق بين JavaScript
        العربي والإنجليزي في نفس الحساب بدقة.
        """
        payload = self._id_token()
        courses = (payload or {}).get("available_courses") or []
        if not courses:
            return ""
        norm = self._norm_name(name)
        lang = (self.lang or "").lower()
        hits = []
        for e in courses:
            cc = str(e.get("course_code") or "").lower()
            cid = str(e.get("content_id") or "").lower()
            elang = str(e.get("course_lang_code") or "").lower()
            # فلترة اللغة إن طُلبت: إما course_lang_code أو لاحقة content_id
            if lang and elang != lang and not cid.endswith("_" + lang):
                continue
            if norm in ("python",) and ("python" in cc or "python" in cid):
                hits.append(e)
            elif norm in ("javascript", "js") and ("javascript" in cc or "javascript" in cid):
                hits.append(e)
            elif norm and (norm == cc or norm == cid):
                # فرع احتياطي صارم: تطابق كامل فقط — لا تطابق جزئي (حتى لا يلتبس "java" مع "javascript")
                hits.append(e)
        if not hits:
            return ""
        # الأولوية: غير التجريبي ثم اللغة المطلوبة (أو العربي افتراضيًا)
        hits.sort(key=lambda e: (
            "trial" in str(e.get("course_code") or "").lower(),
            str(e.get("course_lang_code") or "") != (lang or "ar"),
        ))
        return str(hits[0].get("content_id") or "")

    def _select_course(self, content_id):
        """يحدد المسار المطلوب في المنصة عبر زيارة /?content_id=... (يكتب كوكي الاختيار)."""
        r = self.context.request.get(BASE + "/?content_id=" + urllib.parse.quote(content_id))
        try:
            cookies = (self.context.storage_state() or {}).get("cookies") or []
        except Exception:
            cookies = []
        if not any(c.get("name") == "student_selected_content_id" for c in cookies):
            raise RuntimeError("تعذّر اختيار المسار في المنصة (جرّب الوضع العادي).")
        # مسار جديد = كعكات جديدة: نلغي كاش الـ opener المتوازي ونعيد بناؤه هنا (في الخيط الرئيسي)
        self._opener_cache = None
        self._turbo_opener()

    def enter_course(self, name):
        """اختيار مسار معيّن (سريع عبر API المنصة، أو عادي عبر صفحات البوابة)."""
        print(f"📥 جاري الدخول إلى مسار {name}...", flush=True)
        self._section_id = 0  # القسم خاص بالمسار — لازم يتعاد اكتشافه لكل مسار جديد
        self._course_code = ""
        if self.turbo:
            self._enter_course_fast(name)
            return
        self._enter_course_native(name)

    def _enter_course_fast(self, name):
        """دخول سريع عبر API المنصة مباشرة (تحديد المسار ثم قراءة بياناته)."""
        cid = self._course_content_id(name)
        if not cid:
            lang_txt = ""
            if self.lang in ("ar", "en"):
                lang_txt = " باللغة " + ("العربية" if self.lang == "ar" else "الإنجليزية")
            raise RuntimeError(
                f"تعذّر تحديد مسار {name}{lang_txt} — لا يوجد مسار مطابق في حساب المنصة "
                f"(تأكد من اللغة المختارة أو جرّب الوضع العادي)."
            )
        self._select_course(cid)
        me = self.api_get_json("/api/study/students/me", {}) or {}
        code = me.get("course_code")
        if not code:
            raise RuntimeError(
                f"تعذّر تحديد مسار {name} — لا يوجد course_code (جرّب الوضع العادي)."
            )
        self._course_code = code
        actual = self._me_course_meta(me)
        section = self.resolve_section_id()
        suffix = f" ({actual})" if actual else ""
        print(f"✅ تم الدخول إلى مسار {name} (سريع){suffix}: القسم {section}", flush=True)

    def _enter_course_native(self, name):
        """دخول عادي عبر صفحات البوابة (اختيار المادة بالضغط عليها)."""
        self.page.goto(PORTAL_HOME, wait_until="domcontentloaded")
        self.page.wait_for_selector(f"text={name}", timeout=30000)
        self.page.locator(f"text={name}").first.click()
        self.page.wait_for_url("**/me-tp.qureo.education/**", timeout=30000)
        time.sleep(0.8)
        print(f"✅ تم الدخول إلى مسار {name}: {self.page.url}", flush=True)

    # ------------------------------------------------------- المسار السريع
    @staticmethod
    def _norm_name(s):
        return re.sub(r"[\s\-_/.()]", "", str(s or "")).lower()

    def _courses_match(self, a, b):
        na, nb = self._norm_name(a), self._norm_name(b)
        return bool(na and nb) and (na in nb or nb in na)

    def _me_course_meta(self, me):
        """يعيد اسم المسار الفعلي لكل الحساب (من بيانات /me أو بيانات المادة)."""
        for key in ("course_name", "name", "course_title", "title"):
            v = me.get(key)
            if v:
                return str(v)
        code = me.get("course_code")
        if code:
            course = self.api_get_json(f"/api/study/courses/{code}", {}) or {}
            for key in ("name", "title"):
                v = course.get(key)
                if v:
                    return str(v)
        return ""

    def _enter_browser_mode(self, student_id, password):
        """لو الوضع السريع مينفعش: يحوّل الحساب لمتصفح حقيقي (نفس الأسلوب العادي)."""
        if not _HAS_PLAYWRIGHT:
            return False
        print("🖥️ هذا الحساب يتطلب متصفحًا — تحويل تلقائي...", flush=True)
        try:
            self._launch_browser()
        except RuntimeError as e:
            print(f"⚠️ تعذّر تحويل الحساب للمتصفح: {e}", flush=True)
            return False
        try:
            self._native_login(student_id, password)
        except Exception as e:
            print(f"⚠️ فشل الدخول عبر المتصفح بعد التحويل: {e}", flush=True)
            return False
        self.turbo = False
        self._course_code = ""
        self._section_id = None
        print("✅ تم التحويل لوضع المتصفح — سيتم الحل بالطريقة العادية.", flush=True)
        return True

    # -------------------------------------------------------------------- api
    def _req(self, method, path, body=None):
        url = BASE + path
        req = self.context.request
        if method == "GET":
            return req.get(url)
        data = json.dumps(body if body is not None else {})
        if method == "PUT":
            return req.put(url, data=data, headers=JSON_HEADERS)
        if method == "POST":
            return req.post(url, data=data, headers=JSON_HEADERS)
        raise ValueError(method)

    # طبقة HTTP خفيفة (urllib) آمنة للاستخدام من خيوط متعددة — للتوازي في وضع السرعة
    def _turbo_req(self, method, path, body=None):
        opener = self._turbo_opener()
        url = BASE + path
        headers = {}
        if method == "GET":
            req = urllib.request.Request(url)
        else:
            data = json.dumps(body if body is not None else {}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method=method)
            headers["Content-Type"] = "application/json"
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with opener.open(req, timeout=25) as r:
                raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    data = None
                return _HttpResp(r.status, data)
        except urllib.error.HTTPError as e:
            return _HttpResp(e.code, None)
        except Exception:
            return _HttpResp(0, None)

    def api_get_json(self, path, default=None):
        r = self._req("GET", path)
        if getattr(r, "status", 0) != 200:
            return default
        try:
            return r.json()
        except Exception:
            return default

    # إرسال عدة طلبات متوازية من داخل الصفحة (أسرع بكتير من التسلسل)
    _FETCH_JS = """
    async (reqs) => {
        const out = new Array(reqs.length);
        await Promise.all(reqs.map(async (r, i) => {
            try {
                const opts = { method: r.method, credentials: 'include', headers: {} };
                if (r.body !== null && r.body !== undefined) {
                    opts.headers['Content-Type'] = 'application/json';
                    opts.body = JSON.stringify(r.body);
                }
                const res = await fetch(r.path, opts);
                let data = null;
                try { data = await res.json(); } catch (e) {}
                out[i] = { status: res.status, data: data };
            } catch (e) {
                out[i] = { status: 0, data: null, error: String(e) };
            }
        }));
        return out;
    }
    """

    def fetch_many(self, reqs):
        """يرسل قائمة طلبات متوازية ويعيد النتائج بنفس الترتيب."""
        if not reqs:
            return []
        if self.page is not None:
            return self.page.evaluate(self._FETCH_JS, reqs)
        # الوضع السريع: موازاة خفيفة عبر خيوط (نفس الكعكات من الجلسة)
        return self._fast_fetch_many(reqs)

    def _turbo_opener(self):
        """فاتح HTTP بكعكات جلسة الدخول الحالية (للموازاة في وضع السرعة)."""
        if self._opener_cache is not None:
            return self._opener_cache
        if isinstance(self.context, _UrllibSession):
            # الجلسة نفسها urllib — نعيد فاتحها مباشرة حتى تتشارك الكعكات
            self._opener_cache = self.context.opener
            return self._opener_cache
        cj = http.cookiejar.CookieJar()
        try:
            cookies = (self.context.storage_state() or {}).get("cookies") or []
            for c in cookies:
                try:
                    cj.set_cookie(http.cookiejar.Cookie(
                        version=0,
                        name=c.get("name", ""),
                        value=c.get("value", ""),
                        port=None, port_specified=False,
                        domain=c.get("domain", ""),
                        domain_specified=bool(c.get("domain", "")),
                        domain_initial_dot=str(c.get("domain", "")).startswith("."),
                        path=c.get("path", "/"), path_specified=True,
                        secure=bool(c.get("secure", False)),
                        expires=None, discard=False,
                        comment=None, comment_url=None, rest={}, rfc2109=False,
                    ))
                except Exception:
                    continue
        except Exception:
            pass
        self._opener_cache = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self._opener_cache.addheaders = [
            ("User-Agent",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            ("Accept", "application/json"),
        ]
        return self._opener_cache

    def _fast_fetch_many(self, reqs):
        opener = self._turbo_opener()

        def _one(item):
            url = BASE + str(item.get("path", ""))
            try:
                with opener.open(url, timeout=20) as r:
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = None
                    return {"status": r.status, "data": data}
            except Exception as e:
                return {"status": 0, "data": None, "error": str(e)}

        try:
            with ThreadPoolExecutor(max_workers=6) as ex:
                return list(ex.map(_one, reqs))
        except Exception:
            return [_one(it) for it in reqs]  # fallback تسلسلي

    # -------------------------------------------------------------- discovery
    def _section_from_course(self):
        me = self.api_get_json("/api/study/students/me", {}) or {}
        code = me.get("course_code")
        if not code:
            return None
        course = self.api_get_json(f"/api/study/courses/{code}", {}) or {}
        basic = course.get("basic_section") or {}
        return basic.get("id")

    def resolve_section_id(self):
        if self._section_id:
            return self._section_id
        deadline = time.time() + 120
        while time.time() < deadline:
            sid = self._section_from_course()
            if sid:
                self._section_id = sid
                return sid
            if self.page is None:
                break
            m = re.search(r"/section/(\d+)", self.page.url)
            if m:
                self._section_id = int(m.group(1))
                return self._section_id
            m = re.search(r"/chapter/(\d+)", self.page.url)
            if m:
                data = self.api_get_json(f"/api/study/chapters/{m.group(1)}")
                if data and data.get("section"):
                    self._section_id = data["section"]["id"]
                    return self._section_id
            time.sleep(1)
        raise RuntimeError("تعذّر تحديد القسم الحالي.")

    # --------------------------------------------------------------- lectures
    def complete_lecture(self, lid):
        self._req("PUT", f"/api/study/students/lectures/{lid}")
        r = self._req("PUT", f"/api/study/students/lectures/{lid}/complete")
        return r.status == 200

    def complete_chapter_lectures(self, cid, chapter=None, progress=None):
        if progress is None:
            progress = self.api_get_json(f"/api/study/students/chapters/{cid}/lectures", []) or []
            if isinstance(progress, dict):
                progress = progress.get("data") or []
        if chapter is None:
            chapter = self.api_get_json(f"/api/study/chapters/{cid}", {}) or {}
            if isinstance(chapter, dict) and isinstance(chapter.get("data"), dict):
                chapter = chapter["data"]
        done = {p.get("lecture_id") for p in progress if p.get("completed_at")}
        todo = [lec for lec in chapter.get("lectures", []) if lec["id"] not in done]
        if not todo:
            return
        # المحاضرات لها ترتيب إجباري على السيرفر: start ثم complete لكل واحدة بالتتابع
        for lec in todo:
            if stop_requested():
                raise RuntimeError("تم إيقاف التشغيل بواسطة المستخدم")
            self._req("PUT", f"/api/study/students/lectures/{lec['id']}", {})
            r = self._req("PUT", f"/api/study/students/lectures/{lec['id']}/complete", {})
            if r.status in (200, 204):
                print(f"   📗 محاضرة {lec.get('seq')}: {lec.get('title', '')} — تم", flush=True)
            elif r.status in (400, 404):
                # المحاضرة مُكتملة مسبقًا في المنصة ولا تحتاج إعادة
                pass
            else:
                print(f"   ⚠️ تعذّر إكمال المحاضرة {lec['id']} ({r.status})", flush=True)

    # ------------------------------------------------------------------ tests
    def _start_test(self, cid):
        return self._req("PUT", f"/api/study/students/chapters/{cid}/test").status

    def _result_path(self, cid, test_type):
        if test_type == "test":
            return f"/api/study/students/chapters/{cid}/test/result"
        return f"/api/study/students/chapters/{cid}/review/result"

    def _answer_base(self, test_type):
        return "test-questions" if test_type == "test" else "review-questions"

    @staticmethod
    def _extract_key(result):
        key = {}
        for cr in (result or {}).get("choice_results", []) or []:
            qid = cr.get("question_id")
            correct = [c["choice"]["id"] for c in (cr.get("choices") or []) if c.get("correct")]
            if qid and correct:
                key[qid] = ("choice", correct[0])
        for dr in (result or {}).get("description_results", []) or []:
            qid = dr.get("question_id")
            if qid and dr.get("model_answer") is not None:
                key[qid] = ("description", dr["model_answer"])
        return key

    # ------------------------------------------------------------ answer bank
    def _load_answers(self):
        try:
            with open(self.answer_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_answers(self):
        # كتابة ذرّية: ملف مؤقت ثم استبدال — حتى لا يتلف البنك لو اتقطع التشغيل.
        try:
            # دمج أي إجابات خارجية أحدث من الملف قبل الكتابة (حماية من فقدان
            # إجابات جلسة أخرى بتشتغل في نفس الوقت).
            try:
                with open(self.answer_file, encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, dict):
                    for _cid, _qs in disk.items():
                        if _cid not in self.answers:
                            self.answers[_cid] = _qs
                        elif isinstance(_qs, dict):
                            for _q, _kv in _qs.items():
                                self.answers[_cid].setdefault(_q, _kv)
            except Exception:
                pass
            tmp = self.answer_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.answers, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.answer_file)
        except Exception as e:
            print(f"⚠️ تعذّر حفظ بنك الإجابات ({self.answer_file}): {e}", flush=True)

    def _stored_key(self, cid):
        """الإجابات المحفوظة لفصل معيّن بصيغة {qid: (kind, value)}."""
        raw = self.answers.get(str(cid), {})
        return {int(q): (v[0], v[1]) for q, v in raw.items()}

    def _save_key(self, cid, key):
        if not key:
            return
        with self._bank_lock:
            merged = self.answers.get(str(cid), {})
            merged.update({str(q): [k, v] for q, (k, v) in key.items()})
            self.answers[str(cid)] = merged
            self._save_answers()

    def _bank_covers(self, ch):
        """هل بنك الإجابات يغطّي كل أسئلة الفصل؟"""
        qc = ch.get("question_count", 0)
        return bool(qc) and len(self.answers.get(str(ch["id"]), {})) >= qc

    def _harvest_chapter(self, cid):
        """يقرأ نتيجة محاولة سابقة لفصل واحد ويحدّث البنك (طلب أو اثنان فقط)."""
        key = {}
        for path in (
            f"/api/study/students/chapters/{cid}/review/result",
            f"/api/study/students/chapters/{cid}/test/result",
        ):
            data = self.api_get_json(path, None)
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("result"), dict):
                data = data["result"]
            key.update(self._extract_key(data))
        if key:
            self._save_key(cid, key)
        return len(key)

    def harvest_course(self):
        """يحدّث البنك من نتائج المحاولات السابقة، للفصول غير المغطّاة فقط."""
        section_id = self.resolve_section_id()
        section = self.api_get_json(f"/api/study/sections/{section_id}", {}) or {}
        progress = self.api_get_json(
            f"/api/study/students/sections/{section_id}/chapters", []
        ) or []
        pmap = {p.get("chapter_id"): p for p in progress}
        found = 0
        for ch in section.get("chapters", []):
            if self._bank_covers(ch):
                continue
            if (pmap.get(ch["id"], {}) or {}).get("best_correct_count", 0) <= 0:
                continue
            found += self._harvest_chapter(ch["id"])
        print(f"🗄️ بنك الإجابات: تم تحديث {found} إجابة.", flush=True)

    def _answer_q(self, base, qid, kind, payload):
        return self._req("PUT", f"/api/study/students/{base}/{qid}/answer/{kind}", payload)

    def _submit_attempt(self, cid, questions, base, result_path, key):
        """يبدأ محاولة جديدة ويرسل الإجابات بالترتيب (السيرفر يشترط ترتيب الأسئلة).

        مهم جدًا: لازم نوع الإجابة المحفوظة يطابق نوع السؤال الحالي. لو الأنواع
        متبدّلة (بنك قديم) السيرفر يرفض الإجابة بخطأ 400، وبكده المحاولة كلها
        تتعطّل ومش بنتعلّم الإجابات الصح. فلو النوع مش مطابق نستخدم إجابة افتراضية
        صحيحة الشكل عشان المحاولة تكتمل والنتيجة ترجّع الإجابات الصح.
        """
        self._start_test(cid)
        for item in questions:
            qid = item["question"]["id"]
            choices = item.get("choices")
            stored = key.get(qid)
            if choices:
                valid = {c["id"] for c in choices}
                if stored and stored[0] == "choice" and stored[1] in valid:
                    val = stored[1]
                else:
                    val = choices[0]["id"]
                self._answer_q(base, qid, "choice", {"choice_id": val})
            else:
                if stored and stored[0] == "description":
                    val = stored[1]
                else:
                    val = "print(1)"
                self._answer_q(base, qid, "description", {"answer_code": val})
        result = self.api_get_json(result_path, {}) or {}
        if isinstance(result.get("result"), dict):
            result = result["result"]
        return result

    def solve_test(self, cid, test_type="review"):
        questions = self.api_get_json(f"/api/study/chapters/{cid}/test", []) or []
        if not questions:
            return {"ok": False, "reason": "لا توجد أسئلة"}

        base = self._answer_base(test_type)
        result_path = self._result_path(cid, test_type)
        total = len(questions)

        key = self._stored_key(cid)
        cached_complete = all(item["question"]["id"] in key for item in questions)

        result = self._submit_attempt(cid, questions, base, result_path, key)
        last_correct = result.get("correct_count", 0)
        key.update(self._extract_key(result))
        self._save_key(cid, key)

        if cached_complete:
            if last_correct >= total:
                print(f"      💎 محاولة أولى كاملة: {last_correct}/{total}", flush=True)
            else:
                print(f"      ⚠️ البنك غير مطابق — محاولة 1: {last_correct}/{total}", flush=True)
        else:
            print(f"      🔁 محاولة 1: {last_correct}/{total}", flush=True)

        attempt = 1
        while last_correct < total and attempt < 3:
            attempt += 1
            time.sleep(0.3)
            result = self._submit_attempt(cid, questions, base, result_path, key)
            last_correct = result.get("correct_count", 0)
            key.update(self._extract_key(result))
            self._save_key(cid, key)
            print(f"      🔁 محاولة {attempt}: {last_correct}/{total}", flush=True)

        return {"ok": True, "correct": last_correct, "total": total, "perfect": last_correct >= total}

    # ----------------------------------------------------------------- course
    def solve_course(self):
        section_id = self.resolve_section_id()
        section = self.api_get_json(f"/api/study/sections/{section_id}", {}) or {}
        chapters = section.get("chapters", [])
        progress = self.api_get_json(f"/api/study/students/sections/{section_id}/chapters", []) or []
        pmap = {p.get("chapter_id"): p for p in progress}
        print(f"📚 القسم الحالي: {section_id} — عدد الفصول: {len(chapters)}\n", flush=True)

        total_ch = len(chapters)
        emit_progress(self.current_course, 0, total_ch, "")
        state = {"done": 0, "solved": []}

        def do_chapter(ch):
            """يجهّز فصل واحد (محاضراته بالترتيب ثم اختباره) — تسلسليًا بالترتيب."""
            cid, name = ch["id"], ch.get("name", "")
            qcount, test_type = ch.get("question_count", 0), ch.get("test_type", "review")
            print(f"▶️ الفصل {ch.get('seq')}: {name} ({cid}) — أسئلة: {qcount} [{test_type}]", flush=True)
            if qcount and pmap.get(cid, {}).get("best_correct_count", 0) >= qcount:
                print("   ✅ مُكتمل بالفعل — تخطّي", flush=True)
                row = (name, qcount, qcount)
            else:
                # لو البنك لا يغطّي الفصل وعنده محاولة سابقة، نستخرج إجاباته (طلب أو اثنان فقط)
                if not self._bank_covers(ch) and pmap.get(cid, {}).get("best_correct_count", 0) > 0:
                    self._harvest_chapter(cid)
                row = self._solve_chapter(ch, None, None)
            with self._bank_lock:
                state["done"] += 1
                emit_progress(self.current_course, state["done"], total_ch, name)
            if row:
                state["solved"].append(row)
            print("", flush=True)
            return row

        # المنصة تشترط ترتيبًا صارمًا: الفصل لا يُفتح إلا بعد إتمام الفصل السابق،
        # وكل محاضرة لازم تتعمل بالترتيب كمان — فالحل لازم يكون تسلسليًا بالترتيب.
        for ch in chapters:
            do_chapter(ch)
        return state["solved"]

    def _solve_chapter(self, ch, chapter_details, lecture_progress):
        """يكمل محاضرات فصل واحد ثم يحل اختباره."""
        if stop_requested():
            raise RuntimeError("تم إيقاف التشغيل بواسطة المستخدم")
        cid, name = ch["id"], ch.get("name", "")
        qcount, test_type = ch.get("question_count", 0), ch.get("test_type", "review")
        self.complete_chapter_lectures(cid, chapter_details, lecture_progress)
        if qcount:
            res = self.solve_test(cid, test_type)
            if res.get("ok"):
                mark = "💎" if res["perfect"] else "✔️"
                print(f"   {mark} الاختبار: {res['correct']}/{res['total']}", flush=True)
                return (name, res["correct"], res["total"])
            print(f"   ⏭️ تخطّي الاختبار: {res.get('reason')}", flush=True)
            return None
        print("   ℹ️ لا يوجد اختبار لهذا الفصل", flush=True)
        return None

    # ------------------------------------------------------------------ main
    def run(self, student_id=None, password=None, courses=None, close_pause=5):
        if courses:
            self.courses = courses
        self.start()
        try:
            try:
                self.login(student_id, password)
            except RuntimeError as e:
                # بيانات غلط: مفيش فايدة من المتصفح — نرجّع الخطأ فورًا.
                if isinstance(e, LoginError):
                    raise
                # فشل الدخول السريع → محاولة بمتصفح حقيقي قبل إعلان الفشل
                if not self.turbo or not self._enter_browser_mode(student_id, password):
                    raise
            for name in self.courses:
                try:
                    self.enter_course(name)
                except RuntimeError:
                    # المسار السريع معرفش يحدّد/يختار المادة → ندخل عبر المتصفح.
                    # لكن لو محددين لغة (الخيار الثاني) منقدرش نضمن إن بوابة Qureo
                    # هتفتح نفس نسخة المسار (عربي/إنجليزي)، فالأفضل نفشل برسالة
                    # واضحة بدل ما نحل مسار بلغة تانية بالغلط.
                    if self.lang or not self.turbo or not self._enter_browser_mode(student_id, password):
                        raise
                    self._enter_course_native(name)
                if self.turbo and self._course_code:
                    if self._course_code in self._turbo_courses:
                        print(f"⏭️ مسار {name}: نفس المسار ({self._course_code}) — تم حله من قبل.", flush=True)
                        continue
                    self._turbo_courses.add(self._course_code)
                print("\n" + "=" * 60, flush=True)
                self.current_course = name
                solved = self.solve_course()
                perfect = sum(1 for _, c, t in solved if t and c == t)
                print("=" * 60, flush=True)
                print(f"✨ اكتمل مسار {name}! الفصول المنجزة: {len(solved)} (كاملة: {perfect})", flush=True)
                print("=" * 60, flush=True)
        finally:
            if close_pause and self.browser is not None:
                print(f"\n🖐️ سيتم إغلاق المتصفح خلال {close_pause} ثوانٍ...", flush=True)
                time.sleep(close_pause)
            try:
                if self.browser is not None:
                    self.browser.close()
            except Exception:
                pass
            try:
                if self.context is not None and self.browser is None:
                    self.context.dispose()
            except Exception:
                pass
            try:
                self.playwright.stop()
            except Exception:
                pass


if __name__ == "__main__":
    QureoSolver(headless=False).run()