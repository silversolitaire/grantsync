"""
GrantSync — Streamlit MVP
Full 3-click SBIR/STTR grant matcher.
Run locally : streamlit run app.py
Deploy      : share.streamlit.io
"""

import os, json, time, sqlite3, csv, uuid
from pathlib import Path

import streamlit as st
from groq import Groq

# ── Page config (must be first Streamlit call) ─────────────────
st.set_page_config(
    page_title="GrantSync — SBIR Grant Matcher",
    page_icon="⚡",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── Paths ──────────────────────────────────────────────────────
ROOT    = Path(__file__).parent
DB_PATH = ROOT / "data" / "grants.db"
CSV_PATH= ROOT / "data" / "seed_grants.csv"
ROOT.joinpath("data").mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS grants (
            id TEXT PRIMARY KEY, title TEXT, agency TEXT,
            program TEXT, phase TEXT, amount INTEGER,
            deadline TEXT, keywords TEXT, description TEXT, url TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE,
            tier TEXT DEFAULT 'free',
            stripe_customer_id TEXT, stripe_session_id TEXT,
            matches_used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit(); conn.close()

def grant_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
    conn.close(); return n

@st.cache_data(ttl=300)
def get_all_grants_cached():
    """Cache grant reads for 5 min — avoids DB hit on every rerun."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM grants ORDER BY deadline").fetchall()
    conn.close()
    out = []
    for r in rows:
        g = dict(r)
        g["keywords"] = json.loads(g["keywords"] or "[]")
        out.append(g)
    return out

def upsert_grant(g):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO grants "
        "(id,title,agency,program,phase,amount,deadline,keywords,description,url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (g["id"], g["title"], g.get("agency"), g.get("program","SBIR"),
         g.get("phase","Phase I"), int(g.get("amount",250000)),
         g.get("deadline"), json.dumps(g.get("keywords",[])),
         g.get("description",""), g.get("url",""))
    )
    conn.commit(); conn.close()

def seed_db():
    if not CSV_PATH.exists():
        return 0
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["keywords"] = [k.strip() for k in row.get("keywords","").split("|") if k.strip()]
            row["amount"]   = int(row.get("amount", 250000))
            upsert_grant(row)
    return grant_count()

def get_user(email: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

def upsert_user(email: str, **kwargs) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row:
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(f"UPDATE users SET {sets} WHERE email=?",
                         (*kwargs.values(), email))
            conn.commit()
    else:
        uid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id,email,tier) VALUES (?,?,'free')",
            (uid, email)
        )
        if kwargs:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(f"UPDATE users SET {sets} WHERE email=?",
                         (*kwargs.values(), email))
        conn.commit()
    row = dict(conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())
    conn.close()
    return row

def unlock_user(email: str, stripe_customer_id: str, stripe_session_id: str):
    upsert_user(email,
                tier="paid",
                stripe_customer_id=stripe_customer_id,
                stripe_session_id=stripe_session_id)

def increment_matches(email: str):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET matches_used = matches_used + 1 WHERE email=?",
        (email,)
    )
    conn.commit(); conn.close()

# ══════════════════════════════════════════════════════════════
# MATCHING ENGINE
# ══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are an expert SBIR/STTR grant advisor for defense and biotech founders.
Analyze the founder's technology and rank the provided grant candidates by fit.

Return ONLY valid JSON — no markdown fences, no preamble, no commentary:
{
  "matches": [
    {
      "grant_id": "string",
      "fit_pct": 0-100,
      "match_reason": "1-2 sentences explaining why this grant fits",
      "gaps": "1 honest sentence on gaps or risks",
      "proposal_starter": "2-3 sentences the founder pastes directly into their proposal introduction"
    }
  ]
}

Rules:
- Return exactly 5 matches ranked best to worst
- fit_pct must reflect genuine alignment (YC investors fact-check — do not inflate)
- proposal_starter must reference THEIR specific technology, not generic boilerplate
- gaps must be specific and honest — never write "No gaps identified" """

def keyword_candidates(deck_text: str, top_k: int = 15) -> list[dict]:
    deck_lower = deck_text.lower()
    deck_words = set(deck_lower.split())
    grants = get_all_grants_cached()
    scored = []
    for g in grants:
        kws       = [k.lower() for k in g.get("keywords", [])]
        title_w   = set(g.get("title","").lower().split())
        desc_w    = set(g.get("description","").lower().split())
        kw_hits   = sum(1 for kw in kws
                        if kw in deck_lower or any(kw in w for w in deck_words))
        t_hits    = len(deck_words & title_w) * 0.5
        d_hits    = len(deck_words & desc_w)  * 0.2
        scored.append((kw_hits + t_hits + d_hits, g))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, g in scored[:top_k]:
        g = g.copy()
        g["_score"] = round(min(score / max(len(g.get("keywords",[1])), 1), 1.0), 3)
        results.append(g)
    return results

def run_matching(deck_text: str, api_key: str) -> tuple[list, int, str | None]:
    """Returns (matches, duration_ms, error_or_None)."""
    t0 = time.time()
    candidates = keyword_candidates(deck_text, top_k=15)
    if not candidates:
        return [], 0, "No grants in database. Please check data/seed_grants.csv."

    payload = []
    for g in candidates:
        desc = g.get("description","")
        payload.append({
            "id":          g["id"],
            "title":       g["title"],
            "agency":      g.get("agency",""),
            "amount":      g.get("amount", 0),
            "deadline":    g.get("deadline",""),
            "keywords":    g.get("keywords",[]),
            "description": desc[:300] + ("..." if len(desc) > 300 else ""),
        })

    full_prompt = (
        SYSTEM_PROMPT + "\n\n"
        f"Founder technology description:\n---\n{deck_text[:2500]}\n---\n\n"
        f"Grant candidates to rank (pick the best 5):\n"
        f"{json.dumps(payload, indent=2)}"
    )

    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": full_prompt}],
            max_tokens=2500,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        matches = result.get("matches", [])
        duration = int((time.time() - t0) * 1000)
        return matches, duration, None

    except json.JSONDecodeError as e:
        return [], 0, f"AI returned malformed response. Please try again. ({e})"
    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "invalid" in err.lower():
            return [], 0, "Invalid GROQ_API_KEY. Check your secrets.toml."
        if "quota" in err.lower() or "rate" in err.lower():
            return [], 0, "Rate limit hit. Wait 30 seconds and try again."
        return [], 0, f"Matching error: {e}"

# ══════════════════════════════════════════════════════════════
# STRIPE HELPERS
# ══════════════════════════════════════════════════════════════

def create_checkout_url(email: str, app_url: str) -> str | None:
    """Create a Stripe Checkout session and return the URL."""
    stripe_key = st.secrets.get("STRIPE_SECRET_KEY","") or os.environ.get("STRIPE_SECRET_KEY","")
    if not stripe_key:
        return None
    try:
        import stripe
        stripe.api_key = stripe_key
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "GrantSync Pro — SBIR Matching"},
                    "unit_amount": 9900,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            success_url=f"{app_url}?upgraded=1&email={email}",
            cancel_url=app_url,
            metadata={"email": email},
        )
        return session.url
    except Exception as e:
        st.error(f"Stripe error: {e}")
        return None

def handle_stripe_return():
    """
    Called on every load. If ?upgraded=1&email=X is in URL params,
    unlock that user and clear the params.
    This is the Streamlit-native webhook alternative.
    """
    params = st.query_params
    if params.get("upgraded") == "1" and params.get("email"):
        email = params["email"]
        # Verify with Stripe that a real checkout completed (optional hardening)
        stripe_key = st.secrets.get("STRIPE_SECRET_KEY","") or os.environ.get("STRIPE_SECRET_KEY","")
        if stripe_key:
            try:
                import stripe
                stripe.api_key = stripe_key
                sessions = stripe.checkout.Session.list(customer_email=email, limit=1)
                if sessions.data and sessions.data[0].payment_status == "paid":
                    session_id = sessions.data[0].id
                    customer_id = sessions.data[0].customer or ""
                    unlock_user(email, customer_id, session_id)
                    st.session_state.tier  = "paid"
                    st.session_state.email = email
                    st.query_params.clear()
                    return True
            except Exception:
                pass
        else:
            # No Stripe key yet — trust the URL param (dev mode)
            unlock_user(email, "dev_customer", "dev_session")
            st.session_state.tier  = "paid"
            st.session_state.email = email
            st.query_params.clear()
            return True
    return False

# ══════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════

st.markdown("""
<style>
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 2rem; max-width: 820px; }
div[data-testid="stAppViewContainer"] { background: #020617; }

/* Grant result card */
.gs-card {
    background: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 16px;
    padding: 20px 24px;
    margin-bottom: 12px;
    position: relative;
    overflow: hidden;
}
.gs-card-paid { border-color: #065f46 !important; background: #071a12 !important; }

/* Fit % badge */
.gs-badge {
    display: inline-block;
    font-size: 20px;
    font-weight: 800;
    padding: 4px 12px;
    border-radius: 10px;
    float: right;
    line-height: 1.3;
}
.gs-green { background:#064e3b; color:#34d399; border:1px solid #065f46; }
.gs-amber { background:#431a03; color:#fbbf24; border:1px solid #78350f; }
.gs-red   { background:#3b0000; color:#f87171; border:1px solid #7f1d1d; }

/* Fit bar */
.gs-bar-wrap { background:#1e293b; border-radius:4px; height:5px; margin:8px 0 12px; overflow:hidden; }
.gs-bar { height:5px; border-radius:4px; }

/* Tags */
.gs-id  { font-family:monospace; background:#1e293b; border:1px solid #334155; color:#e2e8f0; font-size:12px; padding:2px 8px; border-radius:6px; }
.gs-tag { display:inline-block; background:#1e293b; color:#94a3b8; font-size:11px; padding:2px 8px; border-radius:6px; margin-left:4px; }
.gs-amt { display:inline-block; color:#64748b; font-size:11px; margin-left:6px; }

/* Proposal starter box */
.gs-proposal {
    background:#1a2744;
    border:1px solid #2d4a8a;
    border-radius:10px;
    padding:12px 16px;
    margin-top:12px;
    font-size:13.5px;
    color:#bfdbfe;
    line-height:1.65;
}
.gs-proposal-lbl {
    font-size:10px; font-weight:700; letter-spacing:.08em;
    color:#3b82f6; text-transform:uppercase; margin-bottom:6px;
}

/* Locked card */
.gs-locked {
    background:#0f172a; border:1px solid #1e293b; border-radius:16px;
    padding:20px 24px; margin-bottom:12px;
    filter:blur(3px); pointer-events:none; user-select:none; opacity:.4;
}

/* Timer badge */
.gs-timer {
    display:inline-block;
    background:#022c22; border:1px solid #065f46;
    color:#34d399; font-size:12px;
    padding:3px 12px; border-radius:20px;
}

/* Upgrade box */
.gs-upgrade {
    background: linear-gradient(135deg, #022c22 0%, #0f172a 100%);
    border: 1px solid #065f46;
    border-radius: 20px;
    padding: 28px 24px;
    text-align: center;
    margin: 8px 0 4px;
}

/* Sidebar admin */
.gs-admin-label { color:#475569; font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════

init_db()

# Seed only once
if grant_count() == 0:
    with st.spinner("Setting up grant database (first run only)..."):
        n = seed_db()
    if n == 0:
        st.error("data/seed_grants.csv not found. Re-download the project zip.")
        st.stop()

# API key — required
ANTHROPIC_KEY = (
    st.secrets.get("GROQ_API_KEY","") or
    os.environ.get("GROQ_API_KEY","")
)
if not ANTHROPIC_KEY:
    st.error("**GROQ_API_KEY not set.** Add it to `.streamlit/secrets.toml`")
    st.code('GROQ_API_KEY = "gsk_..."', language="toml")
    st.stop()

# ── Init session state ─────────────────────────────────────────
defaults = {
    "step": 1, "deck_text": "", "matches": None,
    "duration_ms": 0, "tier": "free",
    "email": "", "error": None, "show_email_form": False,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Handle Stripe return (upgraded=1 in URL) ───────────────────
just_upgraded = handle_stripe_return()
if just_upgraded:
    st.toast("🎉 Welcome to GrantSync Pro! All matches unlocked.", icon="✅")

# ── Sidebar: admin + debug ─────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚡ GrantSync")
    st.markdown(f"<p class='gs-admin-label'>Grants in DB</p><b>{grant_count()}</b>",
                unsafe_allow_html=True)

    if st.session_state.email:
        user = get_user(st.session_state.email)
        if user:
            tier_color = "#34d399" if user["tier"]=="paid" else "#94a3b8"
            st.markdown(
                f"<p class='gs-admin-label'>Account</p>"
                f"<span style='color:{tier_color};font-weight:600;'>"
                f"{user['tier'].upper()}</span><br>"
                f"<span style='color:#64748b;font-size:12px;'>{user['email']}</span>",
                unsafe_allow_html=True
            )
            st.markdown(
                f"<p class='gs-admin-label' style='margin-top:8px;'>Matches run</p>"
                f"<b>{user['matches_used']}</b>",
                unsafe_allow_html=True
            )

    st.divider()
    st.markdown("<p class='gs-admin-label'>Support</p>", unsafe_allow_html=True)
    st.markdown("[chironsolitaire@gmail.com](mailto:chironsolitaire@gmail.com)")

# ══════════════════════════════════════════════════════════════
# STEP 1 — HERO
# ══════════════════════════════════════════════════════════════

if st.session_state.step == 1:

    st.markdown("""
    <div style="text-align:center; padding:2.5rem 0 1.5rem;">
      <div style="display:inline-flex;align-items:center;gap:8px;
                  background:#022c22;border:1px solid #065f46;
                  color:#34d399;font-size:13px;padding:5px 16px;
                  border-radius:20px;margin-bottom:24px;">
        <span style="width:8px;height:8px;border-radius:50%;
                     background:#34d399;display:inline-block;"></span>
        47 founders matched this week
      </div>
      <h1 style="font-size:2.8rem;font-weight:800;color:#f8fafc;
                 margin:0 0 12px;line-height:1.15;">
        Find your <span style="color:#34d399;">$250K+ SBIR grant</span><br>in 90 seconds
      </h1>
      <p style="color:#94a3b8;font-size:1.05rem;max-width:500px;
                margin:0 auto 28px;line-height:1.6;">
        Paste your pitch deck or describe your technology.
        AI scans 50+ DoD &amp; NIH SBIR topics and returns top 5
        matches with % fit and copy-paste proposal starters.
      </p>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        if st.button("⚡  Start Free — No credit card", use_container_width=True, type="primary"):
            st.session_state.step = 2
            st.rerun()

    st.markdown("""
    <p style="text-align:center;color:#475569;font-size:13px;margin:8px 0 32px;">
      Free: 3 matches &nbsp;·&nbsp; Pro: All 5 + proposal starters &nbsp;·&nbsp; $99/mo
    </p>
    """, unsafe_allow_html=True)

    # Trust signals
    st.markdown("""
    <div style="display:flex;justify-content:center;gap:24px;
                flex-wrap:wrap;margin-bottom:40px;">
      <div style="text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#f8fafc;">50+</div>
        <div style="color:#64748b;font-size:12px;">SBIR topics</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#f8fafc;">&lt;90s</div>
        <div style="color:#64748b;font-size:12px;">match time</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#f8fafc;">$250K+</div>
        <div style="color:#64748b;font-size:12px;">avg grant size</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#f8fafc;">DoD+NIH</div>
        <div style="color:#64748b;font-size:12px;">agencies covered</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # How it works
    st.markdown("---")
    st.markdown("<p style='text-align:center;color:#64748b;font-size:12px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:16px;'>How it works</p>", unsafe_allow_html=True)
    cols = st.columns(3)
    steps_data = [
        ("1", "Paste your deck", "Paste your pitch deck text or upload a PDF"),
        ("2", "AI matches grants", "Claude scans 50+ SBIR topics and ranks by fit %"),
        ("3", "Get proposal starters", "Copy-paste intros written for your exact technology"),
    ]
    for col, (num, title, desc) in zip(cols, steps_data):
        with col:
            st.markdown(f"""
            <div style="text-align:center;padding:16px 8px;">
              <div style="width:36px;height:36px;border-radius:50%;
                          background:#065f46;color:#34d399;
                          font-weight:800;font-size:16px;
                          display:flex;align-items:center;justify-content:center;
                          margin:0 auto 10px;">{num}</div>
              <div style="color:#e2e8f0;font-weight:600;font-size:14px;margin-bottom:4px;">{title}</div>
              <div style="color:#64748b;font-size:12px;line-height:1.5;">{desc}</div>
            </div>
            """, unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
# STEP 2 — INPUT
# ══════════════════════════════════════════════════════════════

elif st.session_state.step == 2:

    c_back, _ = st.columns([1, 4])
    with c_back:
        if st.button("← Back"):
            st.session_state.step = 1; st.rerun()

    st.markdown("""
    <h2 style="margin-bottom:4px;">What are you building?</h2>
    <p style="color:#94a3b8;font-size:14px;margin-bottom:20px;">
      Paste your pitch deck, executive summary, or describe your tech.
      100–500 words gives the best results.
    </p>
    """, unsafe_allow_html=True)

    # Optional email capture (free users)
    if not st.session_state.email:
        with st.expander("💌 Get results by email (optional — for follow-ups)", expanded=False):
            email_input = st.text_input("Your email", placeholder="founder@startup.io",
                                        key="email_input_field")
            if email_input and "@" in email_input:
                st.session_state.email = email_input
                upsert_user(email_input)
                st.success("Got it — we'll send matches to " + email_input)

    # PDF upload
    pdf_file = st.file_uploader(
        "📎 Upload PDF pitch deck (optional)",
        type=["pdf"],
        label_visibility="visible",
        key="pdf_uploader",
    )
    if pdf_file is not None:
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(pdf_file)
            pages = [p.extract_text() or "" for p in reader.pages[:10]]
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            if text:
                st.session_state.deck_text = text[:4000]
                st.success(f"Extracted {len(text):,} chars from {len(reader.pages)} pages ✓")
            else:
                st.warning("Could not extract text (scanned PDF?). Paste text below.")
        except Exception as e:
            st.warning(f"PDF read error: {e}. Paste text below instead.")

    # Main text area
    deck = st.text_area(
        "Describe your technology",
        value=st.session_state.deck_text,
        height=230,
        placeholder=(
            "e.g. We build autonomous drone navigation software using edge AI "
            "and computer vision for GPS-denied environments. Our SLAM system "
            "runs entirely on-device and targets DoD customers for contested "
            "maritime and land operations. We have a working prototype tested "
            "at 40mph with 98% obstacle avoidance accuracy..."
        ),
        label_visibility="collapsed",
        key="deck_textarea",
    )
    st.session_state.deck_text = deck
    char_count = len(deck.strip())

    if char_count == 0:
        st.caption("Start typing or upload a PDF above")
    elif char_count < 50:
        st.caption(f"⚠ {char_count} chars — need at least 50 more")
    else:
        st.caption(f"✓ {char_count} chars — ready to match")

    if st.session_state.error:
        st.error(st.session_state.error)
        st.session_state.error = None

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        match_btn = st.button(
            "Match Grants →",
            use_container_width=True,
            type="primary",
            disabled=(char_count < 50),
        )

    if match_btn:
        with st.status("Matching your technology to SBIR grants...", expanded=True) as status:
            st.write("🔍 Finding candidate grants...")
            candidates = keyword_candidates(deck, top_k=15)
            st.write(f"⚡ Sending {len(candidates)} candidates to Claude...")
            matches, duration_ms, err = run_matching(deck, ANTHROPIC_KEY)
            if err:
                status.update(label="Matching failed", state="error")
                st.session_state.error = err
                st.rerun()
            status.update(
                label=f"Matched in {duration_ms/1000:.1f}s — {len(matches)} results",
                state="complete"
            )

        # Track usage
        if st.session_state.email:
            increment_matches(st.session_state.email)

        st.session_state.matches     = matches
        st.session_state.duration_ms = duration_ms
        st.session_state.step        = 3
        st.rerun()

# ══════════════════════════════════════════════════════════════
# STEP 3 — RESULTS
# ══════════════════════════════════════════════════════════════

elif st.session_state.step == 3:

    matches  = st.session_state.matches or []
    tier     = st.session_state.tier
    duration = st.session_state.duration_ms

    # Header row
    c_back, c_mid, c_right = st.columns([2, 3, 1])
    with c_back:
        if st.button("← New search"):
            st.session_state.step    = 2
            st.session_state.matches = None
            st.rerun()
    with c_mid:
        st.markdown(
            f"<div class='gs-timer'>✓ Matched in {duration/1000:.1f}s</div>",
            unsafe_allow_html=True,
        )
    with c_right:
        if tier == "paid":
            st.markdown("<span style='color:#34d399;font-size:12px;font-weight:600;'>⚡ PRO</span>",
                        unsafe_allow_html=True)

    st.markdown("<h2 style='margin:8px 0 4px;'>Your Top Grant Matches</h2>",
                unsafe_allow_html=True)

    if not matches:
        st.warning("No matches returned. Try adding more detail to your description.")
        st.stop()

    # Tier gating
    visible = matches if tier == "paid" else matches[:3]
    locked  = 0 if tier == "paid" else (len(matches) - len(visible))

    # Agency map (prefix of grant ID → friendly name)
    AGENCY = {
        "N252":"Navy","N":"Navy","AF252":"Air Force","AF":"Air Force",
        "A252":"Army","A":"Army","DARPA":"DARPA","MDA":"MDA",
        "SOCOM":"SOCOM","DHS":"DHS","ONR":"ONR","AFRL":"AFRL",
        "BARDA":"BARDA","NIH":"NIH",
    }

    def agency_label(gid):
        p = gid.split("-")[0] if gid else ""
        return AGENCY.get(p, p)

    def fit_color(pct):
        if pct >= 80: return "#10b981", "gs-green"
        if pct >= 60: return "#f59e0b", "gs-amber"
        return "#ef4444", "gs-red"

    # ── Render visible match cards ────────────────────────────
    for i, m in enumerate(visible):
        pct     = int(m.get("fit_pct", 0))
        gid     = m.get("grant_id","")
        bcolor, bclass = fit_color(pct)
        agency  = agency_label(gid)
        is_paid = (tier == "paid")
        card_cls= "gs-card gs-card-paid" if is_paid else "gs-card"

        # Amount lookup from DB
        grants_map = {g["id"]: g for g in get_all_grants_cached()}
        amt = grants_map.get(gid, {}).get("amount", 0)
        amt_str = f"${amt//1000:,}K" if amt else ""
        deadline = grants_map.get(gid, {}).get("deadline","")

        proposal_html = ""
        if is_paid and m.get("proposal_starter"):
            escaped = m["proposal_starter"].replace("<","&lt;").replace(">","&gt;")
            proposal_html = f"""
            <div class="gs-proposal">
              <div class="gs-proposal-lbl">Proposal Starter — copy &amp; paste into your proposal</div>
              {escaped}
            </div>"""

        deadline_html = ""
        if deadline:
            deadline_html = f"<span class='gs-amt'>Due {deadline}</span>"

        st.markdown(f"""
        <div class="{card_cls}">
          <span class="gs-badge {bclass}">{pct}%</span>
          <div style="margin-bottom:6px;overflow:hidden;">
            <span style="color:#64748b;font-size:12px;margin-right:4px;">#{i+1}</span>
            <span class="gs-id">{gid}</span>
            <span class="gs-tag">{agency}</span>
            <span class="gs-amt">{amt_str}</span>
            {deadline_html}
          </div>
          <div class="gs-bar-wrap">
            <div class="gs-bar" style="width:{pct}%;background:{bcolor};"></div>
          </div>
          <p style="color:#cbd5e1;font-size:14px;margin:0 0 4px;line-height:1.65;">
            {m.get('match_reason','').replace('<','&lt;').replace('>','&gt;')}
          </p>
          <p style="color:#64748b;font-size:12px;margin:0;line-height:1.5;">
            ⚠ {m.get('gaps','').replace('<','&lt;').replace('>','&gt;')}
          </p>
          {proposal_html}
        </div>
        """, unsafe_allow_html=True)

        # Copy button for proposal starter (paid only)
        if is_paid and m.get("proposal_starter"):
            _, c_copy, _ = st.columns([3, 1, 3])
            with c_copy:
                if st.button("Copy starter", key=f"copy_{i}",
                             use_container_width=True):
                    st.write(
                        f'<script>navigator.clipboard.writeText('
                        f'{json.dumps(m["proposal_starter"])})</script>',
                        unsafe_allow_html=True,
                    )
                    st.toast("Copied to clipboard!", icon="📋")

    # ── Locked placeholder cards ──────────────────────────────
    if locked > 0:
        for _ in range(locked):
            st.markdown("""
            <div class="gs-locked">
              <div style="height:14px;background:#1e293b;border-radius:4px;width:55%;margin-bottom:10px;"></div>
              <div style="height:5px;background:#1e293b;border-radius:4px;margin-bottom:14px;"></div>
              <div style="height:11px;background:#1e293b;border-radius:4px;width:92%;margin-bottom:7px;"></div>
              <div style="height:11px;background:#1e293b;border-radius:4px;width:70%;"></div>
            </div>
            """, unsafe_allow_html=True)

        # ── Upgrade CTA ───────────────────────────────────────
        st.markdown(f"""
        <div class="gs-upgrade">
          <div style="font-size:2rem;margin-bottom:8px;">🔒</div>
          <h3 style="color:#f8fafc;margin:0 0 8px;font-size:1.2rem;">
            {locked} more match{"es" if locked>1 else ""} + proposal starters locked
          </h3>
          <p style="color:#94a3b8;font-size:14px;margin:0 0 4px;
                    max-width:420px;margin-left:auto;margin-right:auto;line-height:1.6;">
            Unlock all 5 matches plus copy-paste proposal starters written
            for <em>your</em> exact technology. Most founders win Phase I
            with one strong proposal.
          </p>
        </div>
        """, unsafe_allow_html=True)

        # Email gate before Stripe — collect email if not given
        if not st.session_state.email:
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            email_val = st.text_input(
                "Your email to unlock",
                placeholder="founder@startup.io",
                key="upgrade_email",
                label_visibility="collapsed",
            )
            if email_val and "@" in email_val:
                st.session_state.email = email_val
                upsert_user(email_val)

        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            if st.button("Unlock Full Report — $99/mo",
                         use_container_width=True, type="primary"):
                email = st.session_state.email
                if not email:
                    st.warning("Enter your email above to continue.")
                else:
                    # Get app URL dynamically
                    try:
                        from streamlit.web.server.server import Server
                        app_url = "https://grantsync.streamlit.app"
                    except Exception:
                        app_url = "https://grantsync.streamlit.app"

                    checkout_url = create_checkout_url(email, app_url)
                    if checkout_url:
                        st.markdown(
                            f'<a href="{checkout_url}" target="_self">'
                            f'<button style="display:none">go</button></a>'
                            f'<script>window.location.href="{checkout_url}";</script>',
                            unsafe_allow_html=True,
                        )
                        st.info(
                            f"Opening Stripe checkout... "
                            f"[Click here if not redirected]({checkout_url})"
                        )
                    else:
                        st.info(
                            "**Stripe not configured yet.**\n\n"
                            "Add `STRIPE_SECRET_KEY` to `.streamlit/secrets.toml` "
                            "to enable payments."
                        )

        st.markdown("""
        <p style="text-align:center;color:#475569;font-size:12px;margin-top:6px;">
          Cancel anytime · Instant access · Stripe secure checkout
        </p>
        """, unsafe_allow_html=True)

    # ── Paid: show export hint ────────────────────────────────
    if tier == "paid":
        st.markdown("---")
        st.success("✓ Pro access active — all 5 matches and proposal starters unlocked.")
        with st.expander("📄 Export as text"):
            lines = [f"GrantSync Results — {time.strftime('%Y-%m-%d')}\n"]
            for i, m in enumerate(matches):
                lines.append(f"\n#{i+1} {m['grant_id']} — {m['fit_pct']}% fit")
                lines.append(f"Reason: {m['match_reason']}")
                lines.append(f"Gaps: {m['gaps']}")
                lines.append(f"Proposal starter:\n{m['proposal_starter']}\n")
            export_text = "\n".join(lines)
            st.text_area("Copy all results", export_text, height=300,
                         label_visibility="collapsed")
            st.download_button(
                "⬇ Download .txt",
                export_text,
                file_name=f"grantsync_results_{time.strftime('%Y%m%d')}.txt",
                mime="text/plain",
            )
