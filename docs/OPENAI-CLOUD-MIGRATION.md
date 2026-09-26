> **SUPERSEDED 2026-09-26** by [`EXPRESS.md`](EXPRESS.md): kept as history, do not follow it. Its status and the reason are in [`DOC-REGISTER.md`](DOC-REGISTER.md).

# OpenAI cloud migration: remove the laptop as an execution dependency

## Outcome

The laptop becomes an optional interactive client. It is not the scheduler,
credential vault, state store, or only checkout capable of running Blackboard.

OpenAI Codex cloud should own repository-backed engineering work. A narrow
always-on broker/runtime should own the bus doorbell and external credentials.
The two are deliberately separate: an agent sandbox that can edit arbitrary
code must not automatically receive every long-lived production credential.

## What is already portable

- `origin/main` contains every adapter named by `scripts/agent_waker.py` and
  the reply poster; `tests/test_waker_dependencies.py` proves a clean checkout
  can import them.
- Blackboard source is on GitHub, so Codex cloud can clone it, run tests, push a
  branch, and open a pull request without a laptop checkout.
- The Apps Script bus is internet-reachable and its Python transport already
  handles Google's redirect shape.
- ORDER decision logic has passed cross-host fixture and captured-live-board
  comparisons under PowerShell 7.5+. This does not prove live execution.

## Laptop dependencies that remain

1. **Scheduling:** `SFDC24 Blackboard Waker` is a Windows Task Scheduler job.
2. **Credentials:** the live bus and provider credentials are held in the
   laptop's gitignored `.env`.
3. **Durable cursor/state:** waker watermarks are local JSON files.
4. **Windows-only actions:** WhatsApp receipt delivery still invokes
   `powershell.exe` and `wa_notify.ps1`; `fleet_agent.py` still shells out to
   `bus.ps1` for reads and posts.
5. **Hardware/local surfaces:** Glasses capture, desktop/browser control, and
   anything requiring an already-authenticated local UI cannot become an
   OpenAI-hosted sandbox task. Keep these as explicitly optional edge workers.

## Target design

### A. OpenAI-hosted Codex environment — engineering plane

- Connect the GitHub repository `sfdc-24/Blackboard` to a dedicated Codex
  cloud environment.
- Run setup and tests from committed source only.
- Give it no broad laptop `.env` and no Azure/desktop credentials.
- Use reviewed PRs for changes. Production promotion and decommission retain
  their existing decision gates.

### B. Narrow broker/runtime — unattended control plane

- Run only the audited waker entry point on an always-on service.
- Give it the minimum bus capability required for the lane. Prefer a broker or
  function tool that performs allowlisted read/append operations over exposing
  the raw bus secret to arbitrary sandbox code.
- Persist watermark and answered IDs outside an ephemeral checkout.
- Emit health/read-back evidence to Blackboard; unchanged cycles stay quiet.

OpenAI's current sandbox security guidance explicitly recommends brokering
third-party credentials and notes that injected secrets remain readable by
agent-generated code. That is why the broker boundary is part of the design,
not optional hardening.

## Runtime contract added by this migration

The Python clients now accept either:

1. injected `BUS_URL` and `BUS_SECRET` environment variables (cloud path), or
2. a gitignored file selected by `BLACKBOARD_ENV` (local/legacy path).

`BLACKBOARD_STATE_DIR` can move waker state outside an ephemeral checkout. No
secret value is committed, printed, or copied.

## Cutover sequence

1. Create the GitHub-backed Codex cloud environment and run the offline gate:
   `python tests/test_waker_dependencies.py` and
   `python tests/test_cloud_runtime_contract.py`.
2. Provision a minimal bus broker/secret injection path. Allow only the Google
   Apps Script and redirect hosts needed by the bus.
3. Run a **read-only** cloud probe and compare the addressed-row result with the
   laptop against the same bounded query.
4. Move watermark state to durable cloud storage and prove restart continuity.
5. Run cloud and laptop in shadow mode with cloud writes disabled.
6. Enable one allowlisted cloud acknowledgement with exact row-ID read-back.
7. Soak for at least 48 hours with no missed rows, duplicates, or laptop reads.
8. Only Mr. Salam decides whether to disable the laptop scheduler. Do not
   delete the laptop checkout, `.env`, Azure resources, or rollback path during
   the soak.

## Human setup still required

These are credential/organization UI operations and cannot be safely inferred:

- Enable/select the GitHub-backed Blackboard environment in Codex cloud.
- Configure the cloud secret or broker policy without pasting credentials into
  chat, source, setup logs, or a PR.
- Approve the final scheduler cutover after the measured soak.

Everything else should be automated and read back before it is called done.
