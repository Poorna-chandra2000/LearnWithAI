"""LearnWithAI backend (simple Python). Run: python app.py -> open http://127.0.0.1:5000"""
import os, re, csv, io, json, base64, datetime, functools, html
from urllib.parse import urlparse
from exports import resume_blocks, text_blocks, clean_blocks, make_pdf, make_docx
from tools import TOOLS
import jwt, requests, markdown, bleach
from bson import ObjectId
from flask import Flask, render_template,request, jsonify, make_response, Response, g, abort
from pymongo import MongoClient, ReturnDocument
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import certifi


load_dotenv()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "poornachandrashekars@gmail.com").lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:5000")
CONSENT_VERSION = "2026-10"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_FIELDS = ("first_name", "last_name", "dob", "degree", "status", "company", "linkedin")
uri=os.getenv("MONGO_URI")
db = MongoClient(uri,tlsCAFile=certifi.where())["learnapp"]
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024      # Vercel allows about 4.5 MB per request
def now(): return datetime.datetime.now(datetime.timezone.utc)
db.events.create_index("at", expireAfterSeconds=86400)   # live notifications are kept for 24 hours

def broadcast(msg):
    """Save a live notification. Open browsers pick it up within ~20 seconds (works on Vercel, no WebSocket needed)."""
    db.events.insert_one({**msg, "at": now()})

SEED = [
 ("spring-boot", "Spring Boot", "Build Java REST APIs with Spring Boot.", "https://docs.spring.io/spring-boot/"),
 ("spring-ai", "Spring AI", "Add AI and LLMs to Java apps.", "https://docs.spring.io/spring-ai/reference/"),
 ("mcp", "Model Context Protocol (MCP)", "Build MCP servers and clients.", "https://modelcontextprotocol.io/docs"),
 ("fastmcp", "FastMCP (Python)", "Build MCP servers fast in Python.", "https://gofastmcp.com"),
 ("python", "Python", "Python from zero to confident.", "https://docs.python.org/3/"),
 ("python-data-science", "Python Data Science", "NumPy, Pandas, Matplotlib, scikit-learn.", "https://pandas.pydata.org/docs/"),
 ("realtime-data", "Real-time Data (Kafka)", "Stream and process data in real time.", "https://kafka.apache.org/documentation/"),
 ("postgresql", "PostgreSQL", "Relational databases with PostgreSQL.", "https://www.postgresql.org/docs/current/"),
 ("neo4j", "Neo4j Graph Database", "Graph data and Cypher queries.", "https://neo4j.com/docs/"),
 ("linux-devops", "Linux and DevOps", "Linux, Git, Docker, CI/CD basics.", "https://docs.docker.com/"),
 ("aws-cli", "AWS CLI", "Manage AWS from your terminal.", "https://docs.aws.amazon.com/cli/"),
]
if db.courses.count_documents({}) == 0:
    db.courses.insert_many([{"slug": s, "title": t, "desc": d, "docs": u} for s, t, d, u in SEED])

def seed_admin():
    """The admin account is created once and always stays admin. Changing the password later is kept."""
    if db.users.find_one({"email": ADMIN_EMAIL}):
        db.users.update_one({"email": ADMIN_EMAIL}, {"$set": {"role": "admin"}})
    elif ADMIN_PASSWORD:
        db.users.insert_one({"email": ADMIN_EMAIL, "password": generate_password_hash(ADMIN_PASSWORD), "role": "admin",
            "first_name": "Poorna Chandra", "last_name": "Shekar", "dob": "", "degree": "", "status": "Employed",
            "company": "LearnWithAI (Founder)", "linkedin": "https://www.linkedin.com/in/poorna-chandras260120/",
            "notify": True, "share_ok": True, "created": now(), "consent": {"version": CONSENT_VERSION, "at": now()}})
    else:
        print("WARNING: set ADMIN_PASSWORD in .env to create the admin account")
seed_admin()

# ---------- AI ----------
def ask_gemini(prompt, as_json=False):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in your .env file")
    problem = ""
    for model in [MODEL, os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")]:
        cfg = {"maxOutputTokens": 8192, "temperature": 0.4}
        if as_json:
            cfg["responseMimeType"] = "application/json"
        if "flash" in model:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY}, timeout=180,
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg})
            data = r.json()
        except Exception as e:
            problem = f"{model}: network problem ({e})"; continue
        if "error" in data:
            problem = f"{model}: " + data["error"].get("message", "unknown error")[:300]; continue
        cands = data.get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
        text = "".join(p.get("text", "") for p in parts)
        if text.strip():
            return text
        problem = f"{model}: empty answer (reason: {cands[0].get('finishReason') if cands else 'blocked'})"
    raise RuntimeError("Gemini problem - " + problem)

TAGS = ["p","pre","code","h1","h2","h3","h4","ul","ol","li","strong","em","a","table","thead","tbody","tr","th","td","blockquote","br","hr"]
def to_html(text):
    return bleach.clean(markdown.markdown(text, extensions=["fenced_code", "tables"]), tags=TAGS, attributes={"a": ["href"]})

def limit(kind, n):
    """Daily per-user limit on AI calls (protects your token cost)."""
    key = {"email": g.user["email"], "kind": kind, "day": now().strftime("%Y-%m-%d")}
    doc = db.usage.find_one_and_update(key, {"$inc": {"c": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return doc["c"] <= n

def safe(fn):
    try:
        return fn()
    except Exception as e:
        return jsonify(error=str(e)), 502

# ---------- auth helpers ----------
def set_login(resp, u):
    token = jwt.encode({"email": u["email"], "exp": now() + datetime.timedelta(days=7)}, JWT_SECRET, algorithm="HS256")
    resp.set_cookie("token", token, httponly=True, samesite="Lax", max_age=7 * 86400, secure=SITE_URL.startswith("https"))
    return resp

@app.before_request
def load_user():
    g.user = None
    try:
        email = jwt.decode(request.cookies.get("token", ""), JWT_SECRET, algorithms=["HS256"])["email"]
        g.user = db.users.find_one({"email": email})      # role always comes from the database
    except jwt.PyJWTError:
        pass

def login_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        return f(*a, **k) if g.user else (jsonify(error="Please log in first"), 401)
    return w

def admin_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        return f(*a, **k) if g.user and g.user["role"] == "admin" else (jsonify(error="Admin only"), 403)
    return w

def user_json(u):
    d = {k: u.get(k, "") for k in ("email", "role") + PROFILE_FIELDS}
    d.update(id=str(u["_id"]), has_photo=bool(u.get("photo")), notify=bool(u.get("notify")), super=u["email"] == ADMIN_EMAIL)
    return d

def clean_profile(d):
    p = {k: str(d.get(k, "")).strip()[:150] for k in PROFILE_FIELDS}
    if not (p["first_name"] and p["last_name"] and p["degree"]):
        return None, "First name, last name and degree are required"
    if p["status"] not in ("Employed", "Learner"):
        return None, "Choose Employed or Learner"
    if p["status"] == "Employed" and not p["company"]:
        return None, "Please enter your company name"
    if p["status"] == "Learner":
        p["company"] = ""
    if not re.match(r"^https?://([\w-]+\.)?linkedin\.com/.+", p["linkedin"]):
        return None, "Enter a valid LinkedIn profile link"
    try:
        if (datetime.date.today() - datetime.date.fromisoformat(p["dob"])).days < 18 * 365.25:
            return None, "You must be 18 or older to use this site"
    except ValueError:
        return None, "Enter a valid date of birth"
    return p, None

def photo_value(d):
    """Photo arrives as a JPEG data URL; we store only the base64 text in MongoDB."""
    s = d.get("photo") or ""
    prefix = "data:image/jpeg;base64,"
    if not s.startswith(prefix) or len(s) > 600000:
        return None
    try:
        base64.b64decode(s[len(prefix):], validate=True)
    except Exception:
        return None
    return s[len(prefix):]

def profile_text(u):
    return (f"Name: {u['first_name']} {u['last_name']}\nEmail: {u['email']}\nDegree: {u['degree']}\n"
            f"Status: {u['status']} {u.get('company','')}\nLinkedIn: {u['linkedin']}")

# ---------- pages ----------
def index_page(title, desc):
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        t = f.read()
    return Response(t.replace("{{TITLE}}", html.escape(title)).replace("{{DESC}}", html.escape(desc, quote=True))
                     .replace("{{SITE}}", SITE_URL), mimetype="text/html")

DEFAULT = ("LearnWithAI - Learn Spring Boot, Python, AWS, DevOps step by step",
           "AI-driven courses with install steps, folder structure, copy-paste code, resume builder and ATS profile optimizer.")

@app.route("/")
@app.route("/login")
@app.route("/signup")
@app.route("/contact")
@app.route("/privacy")
@app.route("/admin")
@app.route("/profile")
@app.route("/about")
@app.route("/feed")
@app.route("/career")
@app.route("/lesson/<slug>/<int:n>")
def spa(slug=None, n=None):
    return index_page(*DEFAULT)

@app.route("/course/<slug>")
def course_seo(slug):
    c = db.courses.find_one({"slug": slug})
    return index_page(f"{c['title']} Course - LearnWithAI", c["desc"]) if c else index_page(*DEFAULT)


@app.route("/index.html")
def index():
    return render_template("index.html")

# ---------- account API ----------
@app.route("/api/me")
def me():
    return jsonify(user=g.user and user_json(g.user))

@app.route("/api/signup", methods=["POST"])
def signup():
    d = request.get_json(force=True)
    email, pw = d.get("email", "").strip().lower(), d.get("password", "")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(pw) < 8:
        return jsonify(error="Enter a valid email and a password of 8+ characters"), 400
    if db.users.find_one({"email": email}):
        return jsonify(error="This email is already registered"), 400
    c = d.get("consent") or {}
    if not all(c.get(k) for k in ("terms", "privacy", "data_use")):
        return jsonify(error="You must accept the Terms, the Privacy Policy and the data-use consent"), 400
    p, err = clean_profile(d)
    if err:
        return jsonify(error=err), 400
    u = {"email": email, "password": generate_password_hash(pw), "role": "user", **p, "notify": bool(d.get("notify")),
         "share_ok": True, "created": now(), "consent": {"version": CONSENT_VERSION, "at": now(), **{k: True for k in ("terms", "privacy", "data_use")}}}
    pv = photo_value(d)
    if pv: u["photo"] = pv
    u["_id"] = db.users.insert_one(u).inserted_id
    return set_login(make_response(jsonify(user=user_json(u))), u)

@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(force=True)
    u = db.users.find_one({"email": d.get("email", "").strip().lower()})
    if not u or not check_password_hash(u["password"], d.get("password", "")):
        return jsonify(error="Wrong email or password"), 401
    db.users.update_one({"_id": u["_id"]}, {"$set": {"last_login": now()}})
    return set_login(make_response(jsonify(user=user_json(u))), u)

@app.route("/api/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify(ok=True)); resp.delete_cookie("token"); return resp

@app.route("/api/profile", methods=["PUT"])
@login_required
def update_profile():
    d = request.get_json(force=True)
    p, err = clean_profile(d)
    if err:
        return jsonify(error=err), 400
    upd = {"$set": {**p, "notify": bool(d.get("notify"))}}
    if d.get("remove_photo"):
        upd["$unset"] = {"photo": ""}
    elif d.get("photo"):
        pv = photo_value(d)
        if not pv:
            return jsonify(error="Photo must be a small JPEG image"), 400
        upd["$set"]["photo"] = pv
    db.users.update_one({"_id": g.user["_id"]}, upd)
    return jsonify(user=user_json(db.users.find_one({"_id": g.user["_id"]})))

@app.route("/api/password", methods=["POST"])
@login_required
def change_password():
    d = request.get_json(force=True)
    if not check_password_hash(g.user["password"], d.get("old", "")):
        return jsonify(error="Current password is wrong"), 400
    if len(d.get("new", "")) < 8:
        return jsonify(error="New password must be 8+ characters"), 400
    db.users.update_one({"_id": g.user["_id"]}, {"$set": {"password": generate_password_hash(d["new"])}})
    return jsonify(ok=True)

@app.route("/api/profile/delete", methods=["POST"])
@login_required
def delete_account():
    """Permanent account closure: removes the profile, photo, personal lessons and usage data."""
    if g.user["role"] == "admin":
        return jsonify(error="The admin account cannot be deleted"), 400
    if not check_password_hash(g.user["password"], request.get_json(force=True).get("password", "")):
        return jsonify(error="Password is wrong"), 400
    for col in (db.mylessons, db.usage):
        col.delete_many({"email": g.user["email"]})
    db.users.delete_one({"_id": g.user["_id"]})
    resp = make_response(jsonify(ok=True)); resp.delete_cookie("token"); return resp

@app.route("/api/photo/<uid>")
@login_required
def photo(uid):
    try:
        u = db.users.find_one({"_id": ObjectId(uid)}, {"photo": 1, "email": 1})
    except Exception:
        abort(404)
    if not u or not u.get("photo"):
        abort(404)
    if g.user["role"] != "admin" and g.user["_id"] != u["_id"] and u["email"] != ADMIN_EMAIL:
        abort(403)
    return Response(base64.b64decode(u["photo"]), mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=60"})

@app.route("/api/founder")
def founder():
    a = db.users.find_one({"email": ADMIN_EMAIL})
    if not a:
        return jsonify(error="none"), 404
    return jsonify(name=f"{a['first_name']} {a['last_name']}", linkedin=a["linkedin"], degree=a.get("degree", ""),
                   id=str(a["_id"]), has_photo=bool(a.get("photo")))

@app.route("/api/notices")
def notices():
    return jsonify(notices=[{"id": str(n["_id"]), "text": n["text"]} for n in db.notices.find().sort("at", -1).limit(3)])

# ---------- courses and lessons ----------
def get_course(slug):
    return db.courses.find_one({"slug": slug}, {"_id": 0})

def fetch_doc(url, limit=60000):
    """Read a real documentation page. Uses the free Jina Reader (turns any web page into clean text); falls back to a plain download."""
    headers = {"Accept": "text/plain"}
    if os.getenv("JINA_API_KEY"):
        headers["Authorization"] = "Bearer " + os.getenv("JINA_API_KEY")
    try:
        r = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=45)
        if r.ok and len(r.text) > 300:
            return r.text[:limit]
    except Exception:
        pass
    try:
        t = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"}).text
        t = re.sub(r"(?s)<(script|style).*?</\1>", " ", t)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))[:limit]
    except Exception:
        return ""

def cached_doc(url):
    """Documentation pages are downloaded once and kept in MongoDB, so lessons are fast and cheap."""
    d = db.docs.find_one({"url": url})
    if d:
        return d["text"]
    text = fetch_doc(url)
    if text:
        db.docs.insert_one({"url": url, "host": urlparse(url).netloc, "text": text})
    return text

def doc_links(root, text):
    host, links = urlparse(root).netloc, []
    for u in re.findall(r"\]\((https?://[^)\s#]+)", text):
        if urlparse(u).netloc == host and u not in links and not re.search(r"\.(png|jpe?g|svg|gif|zip|pdf)$", u):
            links.append(u)
    return links[:150]

def get_syllabus(c):
    cached = db.syllabus.find_one({"slug": c["slug"]})
    if cached:
        return cached["lessons"]
    root = cached_doc(c["docs"])
    if len(root) < 300:
        raise RuntimeError("Could not read the official docs at " + c["docs"] + ". Check the docs link in Admin.")
    links = doc_links(c["docs"], root)
    text = ask_gemini(f"""Create a beginner-to-advanced syllabus for the course '{c['title']}' based ONLY on this official documentation
(its home page text and page links). Follow the documentation's own topics and order. Return ONLY 10 to 14 lesson titles,
one per line, no numbering, no extra text.\n\nDOCS HOME ({c['docs']}):\n{root[:9000]}\n\nDOC PAGES:\n""" + "\n".join(links[:80]))
    lessons = [l.strip("-*#0123456789. ").strip() for l in text.splitlines() if l.strip()][:14]
    if len(lessons) < 3:
        raise RuntimeError("AI returned a bad syllabus, please try again")
    db.syllabus.insert_one({"slug": c["slug"], "lessons": lessons})
    return lessons

def make_lesson(c, lessons, n):
    """Pick the most relevant REAL documentation pages for this lesson, read them, then teach only from them."""
    links = doc_links(c["docs"], cached_doc(c["docs"]))
    pick = ask_gemini(f'Choose the 3 documentation URLs most relevant to the lesson "{lessons[n]}" of the course "{c["title"]}". '
                      "Reply with only the URLs, one per line.\n" + "\n".join(links)) if links else ""
    urls = [u.strip() for u in pick.splitlines() if u.strip() in links][:3] or links[:2] or [c["docs"]]
    pages = "\n\n".join(f"SOURCE: {u}\n{cached_doc(u)[:9000]}" for u in urls)
    text = ask_gemini(f"""You are a patient teacher spoon-feeding a complete beginner. Course: {c['title']}.
Lesson {n+1} of {len(lessons)}: "{lessons[n]}". All lessons: {lessons}.
Use ONLY the official documentation excerpts below as your source of truth. Do not invent APIs, versions, commands or
configuration that the excerpts do not support. If something is not covered, say so and point to the docs link instead of guessing.
Write Markdown with: 1. What you will learn  2. Prerequisites  3. Installation and setup (Windows, Mac, Linux, exact commands from the docs)
4. Project folder structure (text tree)  5. Every file with COMPLETE copy-paste code in fenced code blocks, file name above each
6. Line-by-line explanation in simple words  7. How to run and the expected output  8. Common errors and fixes  9. Summary and a small practice task.
DOCUMENTATION:\n{pages}""")
    return text + "\n\n---\n**Sources (official documentation):**\n" + "\n".join(f"- {u}" for u in urls)

@app.route("/api/courses")
def courses():
    return jsonify(courses=list(db.courses.find({}, {"_id": 0})))

@app.route("/api/course/<slug>")
def course(slug):
    c = get_course(slug)
    if not c:
        return jsonify(error="Course not found"), 404
    if not g.user:
        return jsonify(course=c, lessons=[], locked=True)
    return safe(lambda: jsonify(course=c, lessons=get_syllabus(c), locked=False))

@app.route("/api/lesson/<slug>/<int:n>")
@login_required
def lesson(slug, n):
    c = get_course(slug)
    if not c:
        return jsonify(error="Course not found"), 404
    def work():
        lessons = get_syllabus(c)
        if n >= len(lessons):
            return jsonify(error="Lesson not found"), 404
        mine = db.mylessons.find_one({"email": g.user["email"], "slug": slug, "n": n})
        if mine:                                   # personal version (user asked the tutor to update it)
            return jsonify(title=lessons[n], html=to_html(mine["text"]), n=n, total=len(lessons), course=c, personal=True)
        shared = db.lessons.find_one({"slug": slug, "n": n})   # shared version: generated once, reused for everyone
        if shared:
            text = shared["text"]
        else:
            text = make_lesson(c, lessons, n)
            db.lessons.insert_one({"slug": slug, "n": n, "text": text})
        return jsonify(title=lessons[n], html=to_html(text), n=n, total=len(lessons), course=c, personal=False)
    return safe(work)

@app.route("/api/ask", methods=["POST"])
@login_required
def ask():
    d = request.get_json(force=True)
    c = get_course(d.get("slug", "")) or {"title": "programming"}
    q, n = d.get("q", "")[:1000], int(d.get("n", 0))
    if not limit("ask", 20):
        return jsonify(error="Daily AI tutor limit reached. Try again tomorrow."), 429
    def work():
        if d.get("update"):                        # only now do we spend tokens to rewrite the lesson for THIS user
            key = {"email": g.user["email"], "slug": d.get("slug"), "n": n}
            base = (db.mylessons.find_one(key) or db.lessons.find_one({"slug": d.get("slug"), "n": n}) or {}).get("text", "")
            text = ask_gemini(f"Rewrite this lesson for a learner who says: \"{q}\". Keep the same 9-section Markdown structure, "
                              f"complete copy-paste code and simple explanations, but fix what they struggled with.\n\nLESSON:\n{base}")
            db.mylessons.update_one(key, {"$set": {"text": text, "at": now()}}, upsert=True)
            return jsonify(html=to_html(text), updated=True)
        return jsonify(html=to_html(ask_gemini(f"Course: {c['title']}. A beginner asks: {q}\nAnswer simply with copy-paste code and explanation. Markdown.")))
    return safe(work)

@app.route("/api/lesson/reset", methods=["POST"])
@login_required
def lesson_reset():
    d = request.get_json(force=True)
    db.mylessons.delete_one({"email": g.user["email"], "slug": d.get("slug"), "n": int(d.get("n", 0))})
    return jsonify(ok=True)

# ---------- career tools ----------
def career(kind, prompt):
    if not limit(kind, 10):
        return jsonify(error="Daily limit reached (10 per tool). Try again tomorrow."), 429
    return safe(lambda: (lambda t: jsonify(html=to_html(t), text=t))(ask_gemini(prompt)))

RESUME_SHAPE = '{"name":"","email":"","phone":"","location":"","linkedin":"","summary":"","skills":[""],"experience":[{"title":"","company":"","dates":"","bullets":[""]}],"projects":[{"name":"","bullets":[""]}],"education":[{"degree":"","school":"","dates":""}],"certifications":[""]}'

@app.route("/api/career/resume", methods=["POST"])
@login_required
def career_resume():
    d = request.get_json(force=True)
    if not limit("resume", 10):
        return jsonify(error="Daily limit reached (10 per tool). Try again tomorrow."), 429
    def work():
        raw = ask_gemini(f"""Build ATS-friendly resume content as JSON with exactly this shape: {RESUME_SHAPE}
PROFILE:\n{profile_text(g.user)}\n\nDETAILS (experience, projects, skills, certifications):\n{d.get('details','')[:6000]}\n
TARGET JOB DESCRIPTION (may be empty):\n{d.get('jd','')[:6000]}\n
Rules: bullets start with strong action verbs and show measurable results; naturally include keywords from the job description;
NEVER invent employers, degrees, dates or numbers - use "[add number]" placeholders and leave a field empty when unknown.""", as_json=True)
        return jsonify(blocks=resume_blocks(json.loads(raw)))
    return safe(work)

@app.route("/api/career/optimize", methods=["POST"])
@login_required
def career_optimize():
    d = request.get_json(force=True)
    if not d.get("jd", "").strip():
        return jsonify(error="Please paste the job description"), 400
    return career("optimize", f"""Act as an expert ATS reviewer and LinkedIn coach.
PROFILE:\n{profile_text(g.user)}\n\nCANDIDATE'S CURRENT RESUME / LINKEDIN TEXT:\n{d.get('current','')[:6000]}\n
JOB DESCRIPTION:\n{d['jd'][:6000]}\n
Reply in Markdown with: 1) ATS match score out of 100 with a short reason, 2) table of keywords found vs missing,
3) top 8 fixes ranked by impact, 4) rewritten LinkedIn headline (3 options), About section, and Skills list,
5) 6 rewritten resume bullets (STAR style). NEVER invent facts - use [add number] placeholders. Be honest about gaps.""")

# ---------- contact ----------
@app.route("/api/contact", methods=["POST"])
def contact():
    d = request.get_json(force=True)
    if not d.get("agree"):
        return jsonify(error="Please accept the Privacy Policy"), 400
    if not (d.get("name") and d.get("email") and d.get("message")):
        return jsonify(error="Please fill all fields"), 400
    db.messages.insert_one({"name": d["name"][:100], "email": d["email"][:150], "message": d["message"][:3000], "at": now()})
    return jsonify(ok=True)

# ---------- admin ----------
@app.route("/api/admin/overview")
@admin_required
def admin_overview():
    users = [{**{k: u.get(k, "") for k in ("email", "role", "notify") + PROFILE_FIELDS}, "created": str(u.get("created", ""))[:16],
              "last_login": str(u.get("last_login", "never"))[:16],
              "consent": str(u.get("consent", {}).get("version") or ("added by admin" if u.get("consent", {}).get("by_admin") else "")),
              "ai_calls": sum(x.get("c", 0) for x in db.usage.find({"email": u["email"]})),
              "personal_lessons": db.mylessons.count_documents({"email": u["email"]}), "has_photo": bool(u.get("photo")), "id": str(u["_id"]), "super": u["email"] == ADMIN_EMAIL}
             for u in db.users.find()]
    msgs = [{"name": m["name"], "email": m["email"], "message": m["message"], "at": m["at"].strftime("%d %b %Y %H:%M")}
            for m in db.messages.find().sort("at", -1).limit(100)]
    nts = [{"id": str(n["_id"]), "text": n["text"]} for n in db.notices.find().sort("at", -1)]
    return jsonify(users=users, messages=msgs, notices=nts, courses=list(db.courses.find({}, {"_id": 0})))

@app.route("/api/admin/export.csv")
@admin_required
def export_csv():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(("email",) + PROFILE_FIELDS + ("notify", "consent_version"))
    for u in db.users.find():
        w.writerow([u["email"]] + [u.get(k, "") for k in PROFILE_FIELDS] + [u.get("notify", False), u.get("consent", {}).get("version", "")])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=users.csv"})

def is_super(u):
    """The original admin (ADMIN_EMAIL) is the sudo admin: nobody can edit, demote or delete it."""
    return u["email"] == ADMIN_EMAIL

def admin_profile(d):
    p = {k: str(d.get(k, "")).strip()[:150] for k in PROFILE_FIELDS}
    if not (p["first_name"] and p["last_name"]):
        return None, "First name and last name are required"
    if p["status"] not in ("Employed", "Learner"):
        p["status"] = "Learner"
    if p["status"] == "Learner":
        p["company"] = ""
    if p["linkedin"] and not re.match(r"^https?://([\w-]+\.)?linkedin\.com/.+", p["linkedin"]):
        return None, "Enter a valid LinkedIn link or leave it empty"
    return p, None

def can_manage(target):
    """Normal admins manage normal users. Only the original admin manages admins. Nobody touches the original admin."""
    if is_super(target):
        return is_super(g.user) and target["_id"] == g.user["_id"]
    return target["role"] != "admin" or is_super(g.user) or target["_id"] == g.user["_id"]

@app.route("/api/admin/user", methods=["POST"])
@admin_required
def admin_create_user():
    d = request.get_json(force=True)
    email, pw = d.get("email", "").strip().lower(), d.get("password", "")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(pw) < 8:
        return jsonify(error="Enter a valid email and a password of 8+ characters"), 400
    if db.users.find_one({"email": email}):
        return jsonify(error="This email is already registered"), 400
    role = "admin" if d.get("role") == "admin" else "user"
    if role == "admin" and not is_super(g.user):
        return jsonify(error="Only the original admin can create admins"), 403
    p, err = admin_profile(d)
    if err:
        return jsonify(error=err), 400
    db.users.insert_one({"email": email, "password": generate_password_hash(pw), "role": role, **p, "notify": False,
                         "share_ok": True, "created": now(), "consent": {"by_admin": True, "at": now()}})
    return jsonify(ok=True)

@app.route("/api/admin/user/<email>", methods=["PUT"])
@admin_required
def admin_update_user(email):
    t = db.users.find_one({"email": email.lower()})
    if not t:
        return jsonify(error="User not found"), 404
    if not can_manage(t):
        return jsonify(error="You are not allowed to edit this account"), 403
    d = request.get_json(force=True)
    p, err = admin_profile(d)
    if err:
        return jsonify(error=err), 400
    upd = dict(p)
    if d.get("role") in ("user", "admin") and d["role"] != t["role"]:
        if is_super(t):
            return jsonify(error="The original admin's role can never change"), 400
        if not is_super(g.user):
            return jsonify(error="Only the original admin can change roles"), 403
        upd["role"] = d["role"]
    if d.get("password"):
        if len(d["password"]) < 8:
            return jsonify(error="New password must be 8+ characters"), 400
        upd["password"] = generate_password_hash(d["password"])
    db.users.update_one({"_id": t["_id"]}, {"$set": upd})
    return jsonify(ok=True)

@app.route("/api/admin/user/<email>", methods=["DELETE"])
@admin_required
def admin_delete_user(email):
    t = db.users.find_one({"email": email.lower()})
    if not t:
        return jsonify(error="User not found"), 404
    if is_super(t):
        return jsonify(error="The original admin can never be deleted"), 400
    if t["_id"] == g.user["_id"]:
        return jsonify(error="You cannot delete your own admin account"), 400
    if not can_manage(t):
        return jsonify(error="Only the original admin can delete admins"), 403
    for col in (db.mylessons, db.usage):
        col.delete_many({"email": t["email"]})
    db.users.delete_one({"_id": t["_id"]})
    return jsonify(ok=True)

@app.route("/api/admin/notice", methods=["POST"])
@admin_required
def admin_notice():
    t = request.get_json(force=True).get("text", "").strip()[:300]
    if t:
        db.notices.insert_one({"text": t, "at": now()})
        broadcast({"type": "notice", "text": t, "link": "/"})
    return jsonify(ok=True)

@app.route("/api/admin/notice/<nid>", methods=["DELETE"])
@admin_required
def admin_notice_delete(nid):
    db.notices.delete_one({"_id": ObjectId(nid)})
    return jsonify(ok=True)

@app.route("/api/admin/course", methods=["POST"])
@admin_required
def admin_add():
    d = request.get_json(force=True)
    title = d.get("title", "").strip()
    slug = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")
    if not slug:
        return jsonify(error="Title needed"), 400
    is_new = not db.courses.find_one({"slug": slug})
    db.courses.update_one({"slug": slug}, {"$set": {"slug": slug, "title": title, "desc": d.get("desc", ""), "docs": d.get("docs", ""), "video": clean_video(d.get("video", ""))}}, upsert=True)
    if is_new:
        broadcast({"type": "course", "title": title, "link": "/course/" + slug})
    return jsonify(ok=True)

@app.route("/api/admin/course/<slug>", methods=["DELETE"])
@admin_required
def admin_delete(slug):
    for col in (db.courses, db.syllabus, db.lessons, db.mylessons):
        col.delete_many({"slug": slug})
    return jsonify(ok=True)

@app.route("/api/admin/course/<slug>", methods=["PUT"])
@admin_required
def admin_edit_course(slug):
    d = request.get_json(force=True)
    if not d.get("title", "").strip():
        return jsonify(error="Title needed"), 400
    db.courses.update_one({"slug": slug}, {"$set": {"title": d["title"].strip(), "desc": d.get("desc", ""), "docs": d.get("docs", ""), "video": clean_video(d.get("video", ""))}})
    return jsonify(ok=True)

@app.route("/api/admin/clear/<slug>", methods=["POST"])
@admin_required
def admin_clear(slug):
    for col in (db.syllabus, db.lessons, db.mylessons):
        col.delete_many({"slug": slug})
    c = get_course(slug)
    if c:
        db.docs.delete_many({"host": urlparse(c["docs"]).netloc})   # re-read the docs next time
    return jsonify(ok=True)

@app.route("/api/admin/test-ai")
@admin_required
def test_ai():
    return safe(lambda: jsonify(reply=ask_gemini("Say: Gemini is working!")))

# ---------- about page, team, posts (images are stored as base64 text, like profile photos) ----------
def img_value(s, limit=900000):
    prefix = "data:image/jpeg;base64,"
    if not isinstance(s, str) or not s.startswith(prefix) or len(s) > limit:
        return None
    try:
        base64.b64decode(s[len(prefix):], validate=True)
    except Exception:
        return None
    return s[len(prefix):]

def jpeg(b64):
    return Response(base64.b64decode(b64), mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

def clean_url(u):
    u = str(u).strip()[:300]
    return u if re.match(r"^https?://\S+$", u) else ""      # blocks javascript: links

def lines(s):
    return [l.strip()[:300] for l in str(s).splitlines() if l.strip()][:20]

def oid(x):
    try:
        return ObjectId(x)
    except Exception:
        abort(404)

ABOUT_DEFAULT = {
    "tagline": "Learn to build real things, step by step, with AI.",
    "story": "LearnWithAI started with one simple idea: nobody should get stuck on setup errors. Every lesson gives you installation steps, the exact folder structure, complete copy-paste code and a plain-language explanation of every line.",
    "mission": "Make high-quality tech learning simple, practical and free of confusion, for every student and working professional.",
    "founder_bio": "", "video": "",
    "highlights": ["Founder of LearnWithAI"],
    "commitments": [
        "Practical learning: every lesson comes with setup, full code and simple explanations.",
        "Always based on official documentation and the latest stable versions.",
        "Your data is used only for learning, job and company references, and notifications you chose.",
        "You can delete your profile permanently at any time.",
        "AI is your tutor, not an oracle: we always encourage checking the official docs."],
    "socials": [{"name": "LinkedIn", "url": "https://www.linkedin.com/in/poorna-chandras260120/"}],
}

def get_about():
    a = db.site.find_one({"_id": "about"}) or {}
    return {**ABOUT_DEFAULT, **{k: v for k, v in a.items() if k != "_id"}}

@app.route("/api/about")
def about():
    a = db.users.find_one({"email": ADMIN_EMAIL}) or {}
    team = [{"id": str(t["_id"]), "name": t["name"], "role": t.get("role", ""), "link": t.get("link", ""), "has_photo": bool(t.get("photo"))}
            for t in db.team.find().sort("at", 1)]
    founder = {"name": f"{a.get('first_name', '')} {a.get('last_name', '')}".strip(), "degree": a.get("degree", ""),
               "company": a.get("company", ""), "has_photo": bool(a.get("photo"))}
    return jsonify(about=get_about(), founder=founder, team=team,
                   stats={"courses": db.courses.count_documents({}), "members": db.users.count_documents({})})

@app.route("/api/founder-photo")
def founder_photo():
    a = db.users.find_one({"email": ADMIN_EMAIL}, {"photo": 1})
    return jpeg(a["photo"]) if a and a.get("photo") else abort(404)

@app.route("/api/team")
def team_list():
    return jsonify(team=[{"id": str(t["_id"]), "name": t["name"], "role": t.get("role", "")} for t in db.team.find().sort("at", 1)])

@app.route("/api/team-photo/<tid>")
def team_photo(tid):
    t = db.team.find_one({"_id": oid(tid)}, {"photo": 1})
    return jpeg(t["photo"]) if t and t.get("photo") else abort(404)

@app.route("/api/posts")
def posts():
    q = {} if g.user else {"public": True}       # visitors only see public posts
    return jsonify(posts=[{"id": str(p["_id"]), "title": p["title"], "text": p.get("text", ""), "public": p["public"],
                           "has_image": bool(p.get("image")), "video": p.get("video", ""), "links": p.get("links", []), "files": p.get("files", []), "at": p["at"].strftime("%d %b %Y")}
                          for p in db.posts.find(q).sort("at", -1).limit(60)])

@app.route("/api/post-image/<pid>")
def post_image(pid):
    p = db.posts.find_one({"_id": oid(pid)})
    if not p or not p.get("image"):
        abort(404)
    if not p["public"] and not g.user:
        abort(403)
    return jpeg(p["image"])

@app.route("/api/admin/about", methods=["PUT"])
@admin_required
def admin_about():
    d = request.get_json(force=True)
    socials = []
    for line in lines(d.get("socials", "")):
        name, _, url = line.partition("|")
        if name.strip() and clean_url(url):
            socials.append({"name": name.strip()[:30], "url": clean_url(url)})
    db.site.update_one({"_id": "about"}, {"$set": {
        "tagline": str(d.get("tagline", ""))[:200], "story": str(d.get("story", ""))[:3000], "mission": str(d.get("mission", ""))[:1500],
        "founder_bio": str(d.get("founder_bio", ""))[:2000], "video": clean_video(d.get("video", "")), "highlights": lines(d.get("highlights", "")),
        "commitments": lines(d.get("commitments", "")), "socials": socials}}, upsert=True)
    return jsonify(ok=True)

@app.route("/api/admin/team", methods=["POST"])
@admin_required
def admin_team_add():
    d = request.get_json(force=True)
    if not d.get("name", "").strip():
        return jsonify(error="Name needed"), 400
    t = {"name": d["name"].strip()[:100], "role": d.get("role", "").strip()[:100], "link": clean_url(d.get("link", "")), "at": now()}
    if d.get("photo"):
        t["photo"] = img_value(d["photo"], 300000)
        if not t["photo"]:
            return jsonify(error="Photo must be a small JPEG"), 400
    db.team.insert_one(t)
    return jsonify(ok=True)

@app.route("/api/admin/team/<tid>", methods=["DELETE"])
@admin_required
def admin_team_delete(tid):
    db.team.delete_one({"_id": oid(tid)})
    return jsonify(ok=True)

@app.route("/api/admin/post", methods=["POST"])
@admin_required
def admin_post_add():
    d = request.get_json(force=True)
    title = d.get("title", "").strip()[:150]
    if not title:
        return jsonify(error="Title needed"), 400
    links = []
    for line in lines(d.get("links", "")):
        name, _, url = line.partition("|")
        if clean_url(url):
            links.append({"title": name.strip()[:80] or "Link", "url": clean_url(url)})
    p = {"title": title, "text": d.get("text", "")[:3000], "public": bool(d.get("public")), "video": clean_video(d.get("video", "")),
         "links": links, "files": [], "at": now()}
    if d.get("image"):
        p["image"] = img_value(d["image"])
        if not p["image"]:
            return jsonify(error="Image must be a JPEG under about 650 KB"), 400
    raws, total = [], 0
    for f in (d.get("files") or [])[:3]:                 # attachments: any file type, 2.5 MB in total
        try:
            raw = base64.b64decode(f.get("data", ""), validate=True)
        except Exception:
            continue
        total += len(raw)
        raws.append((re.sub(r"[^\w.\- ]", "_", str(f.get("name", "file")))[:80], raw))
    if total > 2500000:
        return jsonify(error="Attachments are too big (max 2.5 MB in total)"), 400
    pid = db.posts.insert_one(p).inserted_id
    files = []
    for name, raw in raws:
        fid = db.files.insert_one({"post": str(pid), "name": name, "data": raw, "public": p["public"]}).inserted_id
        files.append({"id": str(fid), "name": name, "size": max(1, round(len(raw) / 1024))})
    if files:
        db.posts.update_one({"_id": pid}, {"$set": {"files": files}})
    broadcast({"type": "post", "title": title, "public": p["public"], "link": "/feed"})
    return jsonify(ok=True)

@app.route("/api/admin/post/<pid>", methods=["DELETE"])
@admin_required
def admin_post_delete(pid):
    db.posts.delete_one({"_id": oid(pid)})
    db.files.delete_many({"post": pid})
    return jsonify(ok=True)

# ---------- live events, video links, file download, tools, export ----------
def clean_video(u):
    """Accepts YouTube, Vimeo or a direct .mp4/.webm link and returns a safe embed address."""
    u = str(u).strip()
    m = re.match(r"^https?://(?:www\.)?(?:youtube\.com/watch\?(?:\S*&)?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([\w-]{11})", u)
    if m:
        return "https://www.youtube.com/embed/" + m.group(1)
    m = re.match(r"^https?://(?:www\.)?vimeo\.com/(\d+)", u)
    if m:
        return "https://player.vimeo.com/video/" + m.group(1)
    return u if re.match(r"^https?://\S+\.(mp4|webm)(\?\S*)?$", u, re.I) else ""

@app.route("/api/events")
def events():
    out = []
    try:
        t = datetime.datetime.fromisoformat(request.args.get("since", ""))
        for e in db.events.find({"at": {"$gt": t}}).sort("at", 1).limit(10):
            if e.get("type") == "post" and not e.get("public") and not g.user:
                continue
            out.append({k: e.get(k) for k in ("type", "title", "text", "link", "public")})
    except ValueError:
        pass
    return jsonify(events=out, now=now().isoformat())

@app.route("/api/file/<fid>")
def get_file(fid):
    f = db.files.find_one({"_id": oid(fid)})
    if not f or (not f["public"] and not g.user):
        abort(404)
    return Response(bytes(f["data"]), mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{f["name"]}"', "X-Content-Type-Options": "nosniff"})

@app.route("/api/tools")
def tools_list():
    return jsonify(tools=[{k: t[k] for k in ("key", "icon", "title", "desc", "fields")} for t in TOOLS])

@app.route("/api/tools/<key>", methods=["POST"])
@login_required
def tools_run(key):
    t = next((x for x in TOOLS if x["key"] == key), None)
    if not t:
        abort(404)
    if not limit("tool", 20):
        return jsonify(error="Daily limit reached (20 tools per day). Try again tomorrow."), 429
    body = request.get_json(force=True)
    vals = {f[0]: str(body.get(f[0], ""))[:5000] for f in t["fields"]}
    prompt = t["prompt"].format(profile=profile_text(g.user), **vals)
    return safe(lambda: (lambda txt: jsonify(html=to_html(txt), text=txt))(ask_gemini(prompt)))

@app.route("/api/export", methods=["POST"])
@login_required
def export_file():
    d = request.get_json(force=True)
    blocks = clean_blocks(d.get("blocks") or []) or text_blocks(str(d.get("text", ""))[:20000])
    fmt, name = d.get("fmt"), (re.sub(r"[^\w-]", "_", str(d.get("name", "document")))[:40] or "document")
    if not blocks or fmt not in ("pdf", "docx"):
        return jsonify(error="Nothing to export"), 400
    try:
        data = make_pdf(blocks) if fmt == "pdf" else make_docx(blocks)
    except ImportError:
        return jsonify(error="Run: pip install -r requirements.txt (reportlab and python-docx are needed)"), 500
    mime = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(data, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})

# ---------- SEO ----------
@app.route("/sitemap.xml")
def sitemap():
    urls = ["/", "/about", "/feed", "/contact", "/privacy", "/career"] + ["/course/" + c["slug"] for c in db.courses.find()]
    body = "".join(f"<url><loc>{SITE_URL}{u}</loc></url>" for u in urls)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>", mimetype="application/xml")

@app.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nDisallow: /admin\nDisallow: /api/\nSitemap: {SITE_URL}/sitemap.xml\n", mimetype="text/plain")

if __name__ == "__main__":
    app.run(debug=True)
