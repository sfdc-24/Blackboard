# Operating surface: what has to be up for the fleet to work

SFDC24 | OPS-20260915 | claude-code-cli (session 36c6e94a) | 15 September 2026

Asked for by Mr. Salam: *"a list of things that need to be open for you to operate without blockers
in blackboard, sfdc24.com and other project work including chrome tabs"*, an organized list of what
should start with the computer, and the same for the Azure VM.

**The short version: almost nothing needs a window.** Agents work through CLIs and APIs. What blocks
them is an expired login, a missing file, or a host with nobody awake on it. The tab list below is
for *you*; each tab costs roughly 130 MB on a laptop that is already short of memory.

---

## 1. Laptop — Tier 0: must be valid (no window, no memory cost)

| What | Checked how | State 2026-09-15 |
|---|---|---|
| `gh` login, account **sfdc-24** | `gh auth status` | OK, scopes gist/read:org/repo/workflow. **A stale default account `Salesforce-SFDC24` has an invalid token** — see Blockers |
| `az` login | `az account show` | OK, abdus@sfdc24.com, Subscription 1 |
| `gcloud` login and project | `gcloud config list` | OK, abdus@sfdc24.com, project `sfdc24` |
| Apps Script `clasp` token | `~/.clasprc.json` exists | Present |
| Board credentials `C:\Users\salam\Quantum\Blackboard\.env` | key names only | Present: BUS_URL, BUS_SECRET, ALPHA_URL, ALPHA_SECRET, GLASSES_*, META_TOKEN, WA_*, PIPEDREAM_*, GOVERNOR_PASS, Headless_* , model keys |
| Salesforce CLI (`sf`) org auth | not re-checked this session | Unverified |
| Tools on PATH | `git`, `gh`, `az`, `gcloud`, `python 3.14`, `node`, `npm`, `clasp`, `sf`, `claude` | All present. **`pwsh` (PowerShell 7) is absent** |

## 2. Laptop — Tier 1: must actually be running

| What | Why | Cost |
|---|---|---|
| **A Claude Code CLI session** | Nothing else wakes this lane. Board rows addressed to `claude-code-cli` are read only while a session is open | ~1.6 GB with the desktop app |
| **Claude desktop app** | Only needed for the connectors (Notion, Drive, Slack, Zoom). The CLI does not need it for git, gh, az, gcloud or the board | part of the 1.6 GB |

## 3. Laptop — Tier 2: open on demand, not always

Codex desktop (~1.5 GB plus 13 node helpers), VS Code, the RDP client for the VM, Chrome.

## 4. Laptop — Tier 3: should not start with Windows

| Entry | State |
|---|---|
| Edge auto-launch, Chrome auto-launch, GitHub Desktop | **Disabled 2026-09-15** |
| Send to OneNote, testcontainers, Brother iPSMonitor | Already disabled |
| Microsoft PC Manager | **Uninstalled 2026-09-15** |
| OneDrive, Microsoft.Lists, Google Drive FS | Still enabled — keep only if you use the synced folders |
| Realtek/Maxim audio, LG Control Center, SecurityHealth, Defender | Keep |

## 5. Chrome — the working set

Pin at most five. Everything else belongs in a bookmark folder ("SFDC24 · open all") so it is one
click when needed and zero memory when not. Turn on Chrome's Memory Saver.

**Pin these five:**

| Tab | Why it earns a pin |
|---|---|
| Blackboard Alpha DB (Google Sheet) | The record. You read it; agents write it through the bus |
| Notion "SFDC24 — Project Board" | Your daily status view |
| GitHub `sfdc-24/Blackboard` pull requests | Where decisions and reviews land |
| Governor console | Where your one-tap decisions live |
| sfdc24.com | The product face; look at it, don't take our word |

**Open on demand (bookmark folder), with what I use instead:**

| Tab | I can do this headless with |
|---|---|
| GitHub Actions runs | `gh run list/view` |
| Azure portal — `COPILOT-DEV-RG` | `az` for everything except **Cost Management**, which the consumption API returns empty for |
| Google Cloud console — project `sfdc24` | `gcloud` |
| Apps Script editor (Governor) | `clasp` for deploys; the editor is for UI-only work |
| Pipedream workflow | Nothing safe — the API PUT replaces rather than merges, so this is UI-only |
| Meta WhatsApp Business manager | Nothing — templates and numbers are UI-only |
| Salesforce org setup | `sf` CLI and the Salesforce MCP for data; setup screens are UI-only |
| Drive folder "SFDC 24 - Claude" | Drive connector, but it truncates the board around row 200 — use the bus |

## 6. Azure VMs

Both run Windows 11 Pro with 42 GB of RAM, and both report the hostname `AkatiaVM` because the disk
was cloned. Tell them apart by resource name.

### 6.1 `AkatiaVM-regular` — the ORDER host (non-evictable)

| Must be true | State |
|---|---|
| Scheduled task `SFDC24 Blackboard Order Worker`, SYSTEM, boot + every 15 min | Ready, **last result 0x14 (20)** — failing, the cutover fixes this |
| `C:\ProgramData\SFDC24\OrderSupervisor` with the release tree | Present |
| Workspace `C:\users\akatiawam\blackboard` with `.env` | Present (BUS_*, ALPHA_*, WA_*, model keys) |
| Network egress to the bus | Implied by worker runs |
| **No interactive login required** | The worker runs as SYSTEM |

### 6.2 `AkatiaVM` — the Spot box where the VM Claude session works

| Must be true | State |
|---|---|
| Someone logged in, with a Claude Code session running | Yes today: Claude 1.9 GB, Chrome 6.4 GB, ChatGPT 1.3 GB |
| Workspace `C:\users\akatiawam\blackboard` with `.env` | Present, and it also carries PIPEDREAM_* and Headless_* |
| It is **Spot** | Azure can evict it. Anything that must survive belongs on the regular VM or on GCE |

### 6.3 Should not start on either VM

`GoogleDriveFS`, the three OneDrive tasks, the Edge and Google updater tasks: a headless worker box
needs none of them. `WindowsAdminCenterHeartbeatCheck` and `WindowsAdminCenterTroubleshootTask` run
every 15 minutes each, about 190 PowerShell starts a day; keep them only if you use Windows Admin
Center. `Akatia Shipping Receipt Processor` was **disabled 2026-09-15** on both.

### 6.4 Unverified, and why

`az vm run-command` executes as SYSTEM, so user-scoped tools and logins under `akatiawam` —
`claude`, `python`, `gcloud`, and the `gh`/`az` sessions — are invisible to it. They are not
necessarily missing. vm-claude-code-cli has been asked to confirm its own surface.

## 7. What actually blocks work today

| Blocker | Fix | Owner |
|---|---|---|
| ORDER worker failing every 15 min (0x14) | PR chain #111 ← #113 ← #114, then a fresh release and a new MRC | agents |
| `gh` default account `Salesforce-SFDC24` has an invalid token | `gh auth logout -h github.com -u Salesforce-SFDC24` | one command, safe |
| No agent wakes on its own | Every session is started by hand. This is the autonomy gap the re-evaluation (PR #112) addresses | decision |
| EOD Notion snapshot | The scheduled run has not posted since Sep 11 | claude-code-cli |
| `pwsh` absent on the laptop | Linux-path suites run only in WSL or CI | acceptable |
| `requests` and `clipspy` not installed for Python 3.14 | Needed before any CLIPS work starts | later |
| Azure cost not readable by API | Use the portal's Cost Management blade | you |

## 8. Startup summary

**Laptop, at sign-in:** Windows security and audio only. Then start what the day needs: a Claude Code
session, and Chrome with the five pinned tabs.

**ORDER VM, at boot:** the ORDER worker task and the Azure agents. Nothing else.

**Spot VM:** treat every start as fresh. Log in, start a Claude Code session, and expect eviction.
