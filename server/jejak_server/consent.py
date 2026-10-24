"""
The page an MCP client sends the user to: sign in (password, or a first-time
invite code plus a new password), see exactly which app asks for what, and
approve or decline.

Defences:
  * CSRF: a random value in a SameSite=Strict, HttpOnly cookie must equal the
    one in the form, and its hash must match the one stored with this pending
    request. A cross-site form post carries no Strict cookie, so it fails.
  * Clickjacking: frame-ancestors 'none' and X-Frame-Options DENY.
  * Injection: the app's name and every other value is HTML-escaped; the
    page loads nothing and runs no script.
  * The pending request is single-use and expires after 10 minutes; the
    password check is throttled per user and per address.
  * Where approval sends access is spelled out (this computer, an app, or a
    named website), so a look-alike app name cannot hide it.
"""
import html
import secrets
import urllib.parse

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, oauth
from .config import driver

COOKIE = "jejak_consent"
SCOPE_TEXT = {
    "jejak:read": "Read knowledge in your spaces",
    "jejak:write": "Save, update and delete knowledge where your role allows",
    "jejak:admin": "Manage members, users, tokens and public views where your role allows",
}

STYLE = """
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#05080f;color:#e6e6e6;font:15px/1.5 -apple-system,system-ui,sans-serif;padding:16px}
main{width:100%;max-width:420px;background:#0f1420;border:1px solid #232b3a;border-radius:12px;padding:24px}
h1{font-size:18px;margin:0 0 12px} p{margin:8px 0} .muted{color:#9aa4b2;font-size:13px}
.app{font-weight:600} .where{background:#1b2333;border-radius:8px;padding:10px;margin:12px 0}
.warn{background:#3a1d1d;color:#fca5a5;border-radius:8px;padding:10px;margin:12px 0}
label{display:block;margin:8px 0} input[type=text],input[type=password]{width:100%;box-sizing:border-box;
 background:#0b1019;border:1px solid #2b3445;border-radius:6px;color:#e6e6e6;font-size:16px;padding:9px 10px}
fieldset{border:1px solid #232b3a;border-radius:8px;margin:12px 0;padding:8px 12px}
.row{display:flex;flex-direction:row-reverse;gap:8px;margin-top:16px} button{flex:1;border:0;border-radius:6px;font-size:15px;padding:11px;cursor:pointer}
.allow{background:#3b82f6;color:#fff} .deny{background:#232b3a;color:#e6e6e6}
details{margin-top:8px} .err{color:#f87171}
"""


def _headers(redirect_uri, secure):
    """No framing, no scripts, and forms may only post here and redirect to the
    app's registered address (browsers apply form-action to the redirect)."""
    p = urllib.parse.urlsplit(redirect_uri)
    target = f"{p.scheme}:" if p.scheme not in ("http", "https") else f"{p.scheme}://{p.netloc}"
    return {
        "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; "
                                   f"form-action 'self' {target}; frame-ancestors 'none'; base-uri 'none'",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }


def _page(d, req, csrf, error=None):
    e = html.escape
    where = oauth.describe_redirect(d["redirect_uri"])
    scopes = "".join(
        f'<label><input type="checkbox" name="scope" value="{e(sc)}" '
        f'{"checked disabled" if sc == "jejak:read" else "checked" if sc != "jejak:admin" else ""}> '
        f'{e(SCOPE_TEXT[sc])}</label>'
        for sc in d["scopes"] if sc in SCOPE_TEXT)
    warn = "" if oauth.is_loopback_redirect(d["redirect_uri"]) else (
        f'<div class="warn">Approving sends access to <b>{e(d["redirect_uri"])}</b>. Only continue if you '
        f'started this yourself, just now, from that app or site.</div>')
    err = f'<p class="err">{e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect to Jejak</title>
<style>{STYLE}</style></head><body><main>
<h1>Connect an app to Jejak</h1>
<p>An app that calls itself <span class="app">{e(d['client_name'])}</span> wants to use your Jejak knowledge.</p>
<div class="where">Access goes to: <b>{e(where)}</b></div>{warn}{err}
<form method="post" action="/oauth/consent">
<input type="hidden" name="req" value="{e(req)}"><input type="hidden" name="csrf" value="{e(csrf)}">
<label>Username<input type="text" name="username" autocomplete="username" autocapitalize="none"></label>
<label>Password<input type="password" name="password" autocomplete="current-password"></label>
<details><summary class="muted">First time? Use your invite code</summary>
<label>Invite code<input type="text" name="invite" autocomplete="off" autocapitalize="none"></label>
<label>Choose a password (12+ characters)<input type="password" name="new_password" autocomplete="new-password"></label>
</details>
<fieldset><legend class="muted">It may</legend>{scopes}</fieldset>
<p class="muted">You can disconnect it at any time: <code>jejak token list</code>, then <code>jejak token revoke &lt;id&gt;</code>.</p>
<div class="row"><button class="allow" name="action" value="allow">Allow</button>
<button class="deny" name="action" value="deny">Decline</button></div>
</form></main></body></html>"""


def _message(text, status=400):
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><title>Jejak</title><style>{STYLE}</style><main><h1>Jejak</h1><p>{html.escape(text)}</p></main>""",
                        status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                     "Content-Security-Policy": "default-src 'none'; "
                                                     "style-src 'unsafe-inline'; frame-ancestors 'none'"})


def register(app):
    secure = oauth.public_url().startswith("https://")

    def show(req, error=None, status=200):
        csrf = secrets.token_urlsafe(32)
        with driver().session() as s:
            try:
                d = oauth.pending_request(s, req)
            except oauth.ConsentError as e:
                return _message(str(e))
            s.run("MATCH (r:OAuthRequest {hash: $h}) SET r.csrf = $c",
                  h=auth.hash_token(req), c=auth.hash_token(csrf))
        resp = HTMLResponse(_page(d, req, csrf, error), status_code=status,
                            headers=_headers(d["redirect_uri"], secure))
        resp.set_cookie(COOKIE, csrf, max_age=oauth.REQUEST_TTL, httponly=True, secure=secure,
                        samesite="strict", path="/oauth")
        return resp

    @app.get("/oauth/consent")
    def consent_page(req: str = ""):
        return show(req)

    @app.post("/oauth/consent")
    async def consent_submit(request: Request):
        form = await request.form()
        req, csrf = str(form.get("req") or ""), str(form.get("csrf") or "")
        cookie = request.cookies.get(COOKIE, "")
        addr = request.client.host if request.client else "?"

        def go():
            with driver().session() as s:
                stored = s.run("MATCH (r:OAuthRequest {hash: $h}) RETURN r.csrf AS c",
                               h=auth.hash_token(req)).single()
                if not (csrf and cookie and secrets.compare_digest(csrf, cookie) and stored
                        and stored["c"] and secrets.compare_digest(stored["c"], auth.hash_token(csrf))):
                    return _message("This form expired or did not come from this page. Start again from your app.",
                                    403)
                try:
                    oauth.pending_request(s, req)
                except oauth.ConsentError as e:
                    return _message(str(e))
                if form.get("action") != "allow":
                    return RedirectResponse(oauth.finish(s, req, None, []), status_code=303)
                try:
                    if form.get("invite"):
                        new_pw = str(form.get("new_password") or "")
                        auth.hash_password(new_pw)  # validate before using up the invite
                        uid = auth.consume_invite(s, str(form.get("invite")).strip(), addr)
                        s.run("MATCH (u:User {user_id: $u}) SET u.password_hash = $h",
                              u=uid, h=auth.hash_password(new_pw))
                    else:
                        uid = auth.verify_password(s, str(form.get("username") or ""),
                                                   str(form.get("password") or ""), addr)
                except (auth.AuthError, ValueError) as e:
                    return str(e)
                scopes = ["jejak:read"] + [str(x) for x in form.getlist("scope") if str(x) in auth.SCOPES]
                return RedirectResponse(oauth.finish(s, req, uid, scopes), status_code=303)

        import anyio
        result = await anyio.to_thread.run_sync(go)
        if isinstance(result, str):
            return show(req, error=result, status=401)
        result.headers["Cache-Control"] = "no-store"
        result.delete_cookie(COOKIE, path="/oauth")
        return result
