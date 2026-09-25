"""Mutation harness for the client-workspace guards (PR #260, Gate 1).

Each mutant breaks one guard - or, where two independent layers guard the same
thing, both of them - and must turn tests.test_studio_clients red. Run from the
repository root:

    python scripts/mutate_studio_clients.py            # every mutant
    python scripts/mutate_studio_clients.py redirect   # only mutants whose name contains "redirect"

It prints KILLED or SURVIVED per mutant and exits 1 if any survived. Files are
restored after each mutant, also on failure or interruption.
"""
from __future__ import annotations

import io
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = "cloud/studio-controller/app/"
MAIN, CORE, PAGE, CLIENTS, TOKENS = (APP + f for f in ("main.py", "core.py", "project_page.py", "clients.py", "tokens.py"))

MUTANTS = [
    # -- which addresses may be reached (SSRF) -------------------------------------------------
    ("dns: the explicit block list off", [(PAGE,
        "    if any(ip in net for net in _BLOCKED if net.version == ip.version):\n        return False\n", "")]),
    ("dns: the is_global check off", [(PAGE, "    return bool(ip.is_global) and not (", "    return True or not (")]),
    ("dns: mapped, 6to4 and teredo forms allowed", [(PAGE,
        "(ip.ipv4_mapped or ip.sixtofour or ip.teredo or getattr(ip, \"scope_id\", None))", "(False)")]),
    ("dns: only the first answer checked", [(PAGE,
        "        if not address_ok(ip):\n            raise PageFetchError(\"an address was refused\")",
        "        if chosen is None and not address_ok(ip):\n            raise PageFetchError(\"an address was refused\")")]),
    ("rebinding: connect by name, not the checked address", [(PAGE,
        'pinned = urlunsplit(("https", "[%s]" % ip if ip.version == 6 else str(ip), parts.path or "/", "", ""))',
        'pinned = url')]),
    ("rebinding: SNI not kept", [(PAGE, 'extensions={"sni_hostname": host})', 'extensions={})')]),
    ("fetch: environment proxy trusted", [(PAGE, "follow_redirects=False, trust_env=False)",
                                           "follow_redirects=False, trust_env=True)")]),
    ("redirect: followed", [(PAGE, "response = client.send(request, stream=True, follow_redirects=False)",
                             "response = client.send(request, stream=True, follow_redirects=True)"), (PAGE,
        '            if 300 <= response.status_code < 400:\n                raise PageFetchError("a redirect was refused")\n', '')]),
    ("fetch: any content type", [(PAGE, "if media.strip().lower() not in CONTENT_TYPES:", "if False:")]),
    ("fetch: no decompressed cap", [(PAGE, "MAX_DECOMPRESSED_BYTES = 3_000_000", "MAX_DECOMPRESSED_BYTES = 10 ** 9")]),
    ("fetch: no compressed cap", [(PAGE, "MAX_COMPRESSED_BYTES = 1_500_000", "MAX_COMPRESSED_BYTES = 10 ** 9")]),
    ("fetch: no first-byte cap", [(PAGE, "            if clock() - started > FIRST_BYTE_SECONDS:\n", "            if False:\n")]),
    ("fetch: no per-chunk total cap", [(PAGE, "                if clock() - started > TOTAL_SECONDS:\n", "                if False:\n")]),
    ("fetch: no wall-clock total", [(PAGE, "    worker.join(TOTAL_SECONDS)", "    worker.join()")]),
    ("fetch: no bound on outstanding loads", [(PAGE, "    slots = _SLOTS\n", "    slots = threading.BoundedSemaphore(10 ** 6)\n")]),
    ("registry: port, query and fragment allowed (both layers)", [(CLIENTS,
        "            and port is None and not parts.username and not parts.password\n"
        "            and not parts.query and not parts.fragment",
        "            and not parts.username and not parts.password"), (CLIENTS,
        "    if any(ch.isspace() or ch in \"\\\\%@#?\" or not ch.isprintable() for ch in url):",
        "    if any(ch.isspace() or ch in \"\\\\%@\" or not ch.isprintable() for ch in url):"), (CLIENTS,
        "parts.netloc == host and", "True and")]),
    ("registry: private-name belt off", [(CLIENTS,
        '    if host.endswith(_PRIVATE_SUFFIXES) or host.split(".")[0] in ("localhost", "metadata"):', '    if False:')]),
    # -- the tree -----------------------------------------------------------------------------
    ("tree: redaction off", [(PAGE, "    if redacted:\n        text = redact(text)\n", "")]),
    ("tree: emails kept", [(PAGE, "    text = _EMAIL_RE.sub(EMAIL, text)\n", "")]),
    # -- which providers a client session reaches (Codex Gate 1 blocker_2) -------------------------
    ("providers: client sessions keep every agent", [(MAIN,
        "        return [a for a in agents if a in client_providers] if is_client_session(state) else list(agents)",
        "        return list(agents)")]),
    ("providers: explicit talk choice not checked", [(MAIN,
        "        allowed = session_agents(state, agents)\n        if agent is not None and agent not in allowed:\n"
        "            raise HTTPException(403, CLIENT_PROVIDER_DENIED)\n        if not allowed:\n"
        "            raise HTTPException(503, \"talk is not available\")",
        "        allowed = session_agents(state, agents)\n        if not allowed:\n"
        "            raise HTTPException(503, \"talk is not available\")")]),
    ("providers: talk routes over every agent", [(MAIN,
        "            agent = route_agent(state.get(\"topic\"), allowed)\n        used = talk_counts",
        "            agent = route_agent(state.get(\"topic\"), agents)\n        used = talk_counts")]),
    ("providers: recap routes over every agent", [(MAIN,
        "            agent = route_agent(state.get(\"topic\"), allowed)\n        spend(recap_counts",
        "            agent = route_agent(state.get(\"topic\"), agents)\n        spend(recap_counts")]),
    ("providers: advisor open to clients", [(MAIN,
        "        if is_client_session(state) and \"gemini\" not in client_providers:\n"
        "            raise HTTPException(403, CLIENT_PROVIDER_DENIED)\n", "")]),
    ("tree: email scan not anchored (quadratic)", [(PAGE, '_EMAIL_RE = re.compile("(?<![^" + _NOT_ADDR + "])[^"',
                                                    '_EMAIL_RE = re.compile("[^"')]),
    ("providers: any setting accepted", [("cloud/studio-controller/app/settings.py",
        "            raise RuntimeError(\"STUDIO_CLIENT_PROVIDERS must be distinct names from: \" + \", \".join(KNOWN_PROVIDERS))",
        "            pass")]),
    ("tree: bare domains kept", [(PAGE, "    return _DOMAIN_RE.sub(LINK, text)", "    return text")]),
    ("tree: only ASCII hosts", [(PAGE, '_LABEL = r"[^\\W_](?:[\\w-]{0,61}[^\\W_])?"',
                                 '_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"')]),
    ("tree: only short TLDs", [(PAGE, "|[^\\W\\d_]{2,63})", "|[a-z]{2,6})")]),
    ("tree: punycode TLD kept", [(PAGE, '_TLD = r"(?:xn--[a-z0-9-]{1,59}|', '_TLD = r"(?:')]),
    ("tree: full-width dots kept", [(PAGE, '_DOT = "[.\\u3002\\uff0e\\uff61]"', '_DOT = "[.]"')]),
    ("tree: host paths kept", [(PAGE, r"(?:[/?#]\S*)?", r"(?:)?")]),
    ("tree: ASCII-only emails", [(PAGE, '_NOT_ADDR + "]+@[^"', '_NOT_ADDR + "]+@[^\\x80-\\U0010ffff"')]),
    ("tree: IPv4 kept", [(PAGE, "    text = _IPV4_RE.sub(LINK, text)\n", "")]),
    ("tree: IPv6 kept", [(PAGE, "    text = _IPV6_RE.sub(_ipv6_link, text)\n", "")]),
    ("tree: no depth cap", [(PAGE, "MAX_DEPTH = 4 ", "MAX_DEPTH = 99 ")]),
    ("tree: no image cap", [(PAGE, "MAX_IMAGES = 12", "MAX_IMAGES = 1000")]),
    ("tree: no parse time cap", [(PAGE, "PARSE_SECONDS = 2.0", "PARSE_SECONDS = 10 ** 9")]),
    ("tree: no event cap", [(PAGE, "MAX_EVENTS = 50_000", "MAX_EVENTS = 10 ** 9")]),
    ("tree: no input cap", [(PAGE, '    text = (html or "")[:MAX_HTML_CHARS]', '    text = html or ""')]),
    ("tree: no unparsed-remainder cap", [(PAGE,
        "            if builder.full or clock() > builder.deadline or len(builder.rawdata) > MAX_PENDING:",
        "            if builder.full or clock() > builder.deadline:")]),
    ("tree: fed in one piece", [(PAGE, "FEED_CHUNK = 16_384", "FEED_CHUNK = 10 ** 9")]),
    ("tree: svg kept", [(PAGE, '"script", "style", "noscript", "template", "svg", ', '"script", "style", "noscript", "template", ')]),
    # -- tokens -------------------------------------------------------------------------------
    ("token: audience not checked", [(TOKENS, 'claims["aud"] == CLIENT_AUDIENCE', 'True')]),
    ("token: type not checked", [(TOKENS, 'claims["typ"] == "client" and ', '')]),
    ("token: future iat accepted", [(TOKENS, '    if claims["iat"] > current + CLOCK_SKEW_SECONDS:\n', '    if False:\n')]),
    ("token: lifetime unbounded", [(TOKENS,
        "          and 0 < claims[\"exp\"] - claims[\"iat\"] <= CLIENT_TOKEN_MAX_SECONDS)",
        "          and 0 < claims[\"exp\"] - claims[\"iat\"])")]),
    ("token: v1 key and signing input (both layers)", [(TOKENS,
        'return hmac.new(secret.encode(), b"sfdc24-studio-client-token-v2", hashlib.sha256).digest()',
        'return secret.encode()'), (TOKENS, 'b"client." + payload.encode()', 'payload.encode()')]),
    ("token: extra claims tolerated", [(TOKENS, "set(claims) != _CLIENT_CLAIMS", "not _CLIENT_CLAIMS <= set(claims)")]),
    ("token: session binding not minted", [(MAIN,
        'binding = {"tnt": tenant, "csub": operator["sid"], "prj": state.get("project") or ""} if client else None',
        'binding = None')]),
    # -- scope, revocation, binding -----------------------------------------------------------
    ("scope: workspace trusts the token alone", [(MAIN,
        "        client = clients.member(claims[\"tnt\"], claims[\"sub\"], auth_service._subject_hash)\n"
        "        if client is None:\n            raise HTTPException(403, WORKSPACE_DENIED)\n        return claims, client",
        "        client = clients.for_subject(claims[\"sub\"], auth_service._subject_hash)\n        if client is None:\n"
        "            client = {\"id\": claims[\"tnt\"], \"name\": \"x\", \"emails\": [], \"projects\": []}\n"
        "        return claims, client")]),
    ("scope: project not required in the token", [(MAIN,
        "            if project is None or requested not in operator.get(\"prj\", []):",
        "            if project is None:")]),
    ("scope: empty operator subject accepted", [(MAIN,
        "            if not claims[\"sid\"]:\n                raise HTTPException(401, \"token has no subject\")\n", "")]),
    ("scope: workspace view not limited to the token's projects", [(MAIN,
        'return public_view(client, allowed=claims["prj"])', 'return public_view(client)')]),
    ("revocation: session requests skip the registry", [(MAIN,
        "        client = clients.member(tenant, subject, auth_service._subject_hash)\n"
        "        if client is None or (project and clients.project(client, project) is None):\n"
        "            raise HTTPException(403, WORKSPACE_DENIED)", "        pass")]),
    ("revocation: project removal not enforced on sessions", [(MAIN,
        "        if client is None or (project and clients.project(client, project) is None):",
        "        if client is None:")]),
    ("revocation: membership ignores the tenant", [(CLIENTS, "            if client[\"id\"] == tenant:", "            if True:")]),
    ("revocation: membership read from cache", [(CLIENTS,
        "        for client in self.clients(fresh=True):\n            if client[\"id\"] == tenant:",
        "        for client in self.clients():\n            if client[\"id\"] == tenant:")]),
    ("binding: token not compared with the stored session", [(MAIN,
        "                and hmac.compare_digest(tenant, state.get(\"client_tenant\") or \"\")\n"
        "                and hmac.compare_digest(project, state.get(\"project\") or \"\"))",
        "                and True)")]),
    ("binding: an unbound token reaches a client session", [(MAIN,
        "        if not bound and not owner:\n            return", "        if not bound:\n            return")]),
    ("binding: the session state is not read", [(MAIN,
        "        bound = any(key in claims for key in (\"tnt\", \"csub\", \"prj\"))\n",
        "        bound = any(key in claims for key in (\"tnt\", \"csub\", \"prj\"))\n        return\n")]),
    ("keys: a blank session key without the tenant", [(CORE,
        "        parts = [subject, creation_id] + ([tenant, project] if tenant else [])",
        "        parts = [subject, creation_id] + ([tenant, project] if project else [])")]),
    ("keys: replay ownership ignores tenant and project", [(CORE,
        "                or (existing.get(\"client_tenant\") or \"\") != tenant\n"
        "                or (existing.get(\"project\") or \"\") != project):",
        "                ):")]),
    # -- budgets before the fetch ---------------------------------------------------------------
    ("budget: no per-tenant page-load budget", [(MAIN,
        "if not await asyncio.to_thread(reserve_fetch_attempt, tenant, project_id):", "if False:")]),
    ("budget: no single flight per tenant", [(MAIN, "if not claim_fetch_slot(tenant):", "if False:")]),
    # -- audit and redacted failures --------------------------------------------------------------
    ("audit: none at all", [(CORE,
        "        if state.get(\"client_tenant\"):\n            self._audit(state, command, prior_revision, result)\n", "")]),
    ("audit: stop not audited", [(CORE,
        "            if candidate.get(\"client_tenant\"):\n"
        "                self._audit(candidate, command, candidate[\"artifact_version\"], result, outcome=\"applied\")\n", "")]),
    ("audit: failure not audited", [(CORE,
        "            if state.get(\"client_tenant\"):\n"
        "                self._audit(state, command, state[\"artifact_version\"], {}, outcome=\"failed\")\n", "")]),
    ("audit: only patch op ids", [(CORE,
        "        op_ids = [e[\"op_id\"] for e in events if e.get(\"op_id\")]",
        "        op_ids = [e[\"op_id\"] for e in events if e.get(\"type\") == \"artifact.patch\"]")]),
    ("errors: unexpected failures not redacted (both layers)", [(MAIN,
        "        except HTTPException:\n            raise\n        except Exception as exc:\n"
        "            # A worker failure is recorded on the command (and in a client\n"
        "            # session's audit); the answer and the log carry no words.\n"
        "            governance.log_event(\"studio.command_failed\", session_id=session_id, error=type(exc).__name__)\n"
        "            raise HTTPException(500, SERVER_ERROR) from None\n", ""), (MAIN,
        "        try:\n            response = await call_next(request)\n        except Exception as exc:",
        "        try:\n            response = await call_next(request)\n        except ZeroDivisionError as exc:")]),
]


def run(selected) -> int:
    results = []
    for name, edits in selected:
        originals = {}
        try:
            for rel, a, b in edits:
                path = ROOT / rel
                if path not in originals:
                    originals[path] = io.open(path, encoding="utf-8", newline="").read()
                current = io.open(path, encoding="utf-8", newline="").read()
                nl = "\r\n" if "\r\n" in current else "\n"
                a2, b2 = a.replace("\n", nl), b.replace("\n", nl)
                if current.count(a2) != 1:
                    raise SystemExit("mutant %r no longer matches %s (found %d times): update the harness"
                                     % (name, rel, current.count(a2)))
                io.open(path, "w", encoding="utf-8", newline="").write(current.replace(a2, b2))
            r = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_studio_clients"],
                               cwd=ROOT, capture_output=True, text=True)
            verdict = "KILLED" if r.returncode else "SURVIVED"
            results.append((name, verdict))
            print("%-8s %s" % (verdict, name), flush=True)
        finally:
            for path, text in originals.items():
                io.open(path, "w", encoding="utf-8", newline="").write(text)
    survived = [n for n, v in results if v == "SURVIVED"]
    print("killed %d of %d" % (len(results) - len(survived), len(results)))
    return 1 if survived else 0


if __name__ == "__main__":
    wanted = sys.argv[1:]
    chosen = [m for m in MUTANTS if not wanted or any(w in m[0] for w in wanted)]
    sys.exit(run(chosen))
