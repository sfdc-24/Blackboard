> **OBSOLETE 2026-09-26**: it replicates the Azure VM, which was dropped on 2026-09-24; line 61 also carries a wrong BUS_URL. Kept as history, do not follow it. How we work now: [`EXPRESS.md`](EXPRESS.md).

# GCloud Linux Migration Playbook — Blackboard Development Environment

**Prepared for:** Claude (claude-code-cli) leading autonomous setup  
**Target:** GCloud Linux instance replicating Azure Windows 11 Pro VM  
**Objective:** Scale beyond Windows, leverage stable Linux OS + GCloud controls  
**Date:** 2026-09-10

---

## Part 1: Current Baseline (Azure Windows 11 Pro VM)

### Development Tools & IDEs
- **Visual Studio Code** or **Visual Studio** (for editing)
- **PowerShell 5.1+** (legacy) / **PowerShell Core** (if installed)
- **Git for Windows** (with credential manager)
- **Node.js** (>=22.0.0 for clasp)
- **Python** (3.x, with pip, venv)
- **Chrome** (for testing, extensions)

### Critical Applications & Services
| Component | Current (Windows) | Notes |
|---|---|---|
| **Apps Script Deploy** | `clasp` (Node.js) via Windows CMD | Deploys to Google Apps Script production |
| **PowerShell Scripts** | `*.ps1` in `scripts/` and `infra/azure/` | Bus clients, deployment, order supervisor |
| **Blackboard Bus** | Python service (historically on Apps Script) | Now: Python `src/bus_server.py` (stdlib-only, Debian-ready) |
| **Zoom Agent** | JavaScript + Apps Script | Runs in browser + server-side |
| **WhatsApp Gateway** | Backend integration | Message routing via bus |
| **Board Clients** | PowerShell watchers (`wa_watch.ps1`, `fleet_watch.ps1`) | Read board, dispatch to agents |
| **Test Suites** | Node.js (`npm test`), Python (`pytest`), PowerShell | 51 JS tests, 50 PS tests, mutation gates |
| **CI/CD** | GitHub Actions `.github/workflows/` | Runs on every PR |

### Folder Structure (Key Directories)
```
Blackboard/
├── .clasp.json                     # Apps Script config (scriptId, rootDir)
├── .github/workflows/              # CI/CD pipeline definitions
├── apps-script/governor-page-api/  # Apps Script source (deploy target)
├── docs/                           # Architecture, runbooks, decisions
├── scripts/                        # PowerShell + Python agents
│   ├── bus.ps1                     # Board bus client
│   ├── wa_watch.ps1                # Watch WhatsApp inbox
│   ├── fleet_watch.ps1             # Watch board for tags
│   ├── order_supervisor.ps1        # Lease management
│   └── ask_groq.py                 # Bulk LLM offload to Groq
├── src/                            # Python source
│   ├── bus_server.py               # Self-hosted Blackboard bus (stdlib-only)
│   └── Blackboard/                 # Bus service package
├── tests/                          # Test suites (JS, Python, PS)
├── infra/azure/                    # Azure-specific deployment scripts
├── tooling/clasp/                  # Node.js clasp wrapper
├── trading/                        # Market data + backtesting
├── web/                            # Web prototypes
└── intake/                         # Project intake funnel schema
```

### Environment Variables & Secrets (Required)
```powershell
# Apps Script
$env:BUS_URL = "https://sheets.googleapis.com/v4/spreadsheets/..."
$env:BUS_SECRET = "<api-key-never-hardcode>"

# Salesforce
$env:SFDC_ORG_ID = "00D..." 
$env:SFDC_API_KEY = "<jwt-or-oauth>"

# Zoom
$env:ZOOM_CLIENT_ID = "..."
$env:ZOOM_CLIENT_SECRET = "..."

# WhatsApp
$env:WHATSAPP_TOKEN = "..."

# Groq (free LLM)
$env:GROQ_API_KEY = "..."

# Claude/Anthropic
$env:ANTHROPIC_API_KEY = "..."

# Local settings (never in repo)
$env:GATE_SECRET = "<proof-of-work-gate>"
$env:CHAT_ENABLED = "true"
$env:CHAT_SESSION_CAP = "12"
$env:CHAT_DAILY_DEFAULT = "150"
```

---

## Part 2: Linux Target Environment (GCloud)

### Recommended GCloud Setup
```yaml
Instance Type:     e2-medium (2 vCPU, 4 GB RAM, $25-30/mo)
OS:                Ubuntu 22.04 LTS (Jammy Jellyfish)
Region:            us-central1 (closest to Azure for feature parity)
Boot Disk:         30 GB (SSD)
Network:           Default VPC with firewall rules for SSH, HTTP/HTTPS
Service Account:   Cloud Build, Cloud Run (if needed)
```

### Quick Launch (gcloud CLI)
```bash
gcloud compute instances create blackboard-dev \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --machine-type=e2-medium \
  --zone=us-central1-a \
  --scopes=cloud-platform \
  --boot-disk-size=30GB \
  --enable-display-device=false \
  --tags=blackboard

# SSH in
gcloud compute ssh blackboard-dev --zone=us-central1-a
```

---

## Part 3: Tool Mapping — Windows → Linux Equivalents

### Development Tools
| Windows Tool | Linux Equivalent | Install |
|---|---|---|
| **Visual Studio Code** | VS Code (same binary) | `snap install code --classic` OR build from `.deb` |
| **Visual Studio** | NA (use VS Code or JetBrains IDEs) | `code` or `vim/neovim` for server-side editing |
| **PowerShell 5.1** | **PowerShell 7+ (Core)** | `snap install powershell --classic` OR `apt install powershell` |
| **Git for Windows** | `git` (native Linux) | `apt install git` |
| **Node.js 22+** | `node` (native) | `apt install nodejs npm` (22.x from NodeSource PPA) |
| **Python 3.x** | `python3` (native) | `apt install python3 python3-pip python3-venv` |
| **Chrome** | Chrome, Chromium, or headless testing | `apt install chromium-browser` (headless for CI) |

### Runtime & Package Managers
| Windows | Linux | Purpose |
|---|---|---|
| `npm` (Node Package Manager) | `npm` (same) | Clasp, Node.js deps |
| `pip` (Python) | `pip3` (same) | Python deps, bus_server |
| `powershell.exe` | `pwsh` (PowerShell 7) | Script execution |
| Windows Task Scheduler | `systemd` timers OR `cron` | Scheduled jobs (bus reader, watchers) |
| Registry / HKLM | `/etc/` config files | System settings |
| `C:\Users\username\` | `/home/username/` | Home directory |

---

## Part 4: PowerShell Scripts → Bash/Python Conversion

### Critical Scripts Needing Porting

| Original (.ps1) | Linux Target | Strategy | Priority |
|---|---|---|---|
| `bus.ps1` | Python wrapper or `bus_server.py` integration | **Bus is now Python stdlib-only** (see below) | **HIGH** |
| `wa_watch.ps1` | Python board reader + WhatsApp poller | Port to `src/bus_reader.py` | **HIGH** |
| `fleet_watch.ps1` | Python board reader + tag router | Port to `src/bus_reader.py` | **HIGH** |
| `order_supervisor.ps1` | Python order processor | Partial Python exists; complete conversion | **MEDIUM** |
| `alpha.ps1` | Bash script OR Python wrapper | Board row appender (simple) | **MEDIUM** |
| `invoke_order_claude.ps1` | Direct Python function call | Order dispatch to agents | **LOW** |

### Pattern: PowerShell → Python/Bash

**Windows PowerShell (current):**
```powershell
$sheet = Invoke-GoogleSheetsAPI -SheetId $BUS_ID -Range "A:J"
$rows = $sheet.Values | Where-Object { $_.Timestamp -gt $LastRead }
foreach ($row in $rows) {
    if ($row[2] -match "claude") { LogAndRoute $row }
}
```

**Linux Python (target):**
```python
import gspread
from oauth2client.service_account import ServiceAccountCredentials

creds = ServiceAccountCredentials.from_json_keyfile_dict(secrets.BUS_CREDS)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.getenv('BUS_ID')).sheet1
rows = sheet.get_all_records()

for row in rows:
    if row['Timestamp'] > last_read and 'claude' in row.get('Source_Tag', ''):
        log_and_route(row)
```

---

## Part 5: Blackboard Bus — Special Case

### Current State
The **Blackboard bus is now Python** (`src/bus_server.py`), not PowerShell:

**Features (stdlib-only, no external deps):**
- HTTP server on port 8787
- SQLite3 backing store
- Supports: `/read`, `/append`, `/replace`, `/tail`, `/schema`
- Systemd service integration
- Debian package: `blackboard-bus_1.1.0_all.deb`

**Install on GCloud Ubuntu:**
```bash
# Build Debian package
cd ~/Blackboard
packaging/build-deb.sh

# Install
sudo apt-get install -y ~/build/blackboard-bus_1.1.0_all.deb

# Configure secret
sudo nano /etc/blackboard-bus/env
# Add: BUS_SECRET=<your-api-key>

# Enable & start
sudo systemctl enable --now blackboard-bus

# Verify
curl -s http://127.0.0.1:8787/
```

**No more Apps Script bus dependency** (optional upgrade path):
- PowerShell `bus.ps1` → routes to Python bus via HTTP
- Python clients directly hit Python bus
- Apps Script can still use bus if needed (via HTTPS bridge)

---

## Part 6: Environment Setup on GCloud Ubuntu

### Initial Provisioning Script

```bash
#!/bin/bash
set -e

echo "=== Blackboard GCloud Linux Setup ==="

# Update system
sudo apt update
sudo apt upgrade -y

# Install base tools
sudo apt install -y \
  git \
  curl \
  wget \
  build-essential \
  python3 \
  python3-pip \
  python3-venv \
  nodejs \
  npm \
  chromium-browser \
  vim \
  htop

# Install PowerShell 7 (for legacy .ps1 scripts if needed)
sudo snap install powershell --classic

# Install clasp for Apps Script deployment
npm install -g @google/clasp@3.4.1

# Clone Blackboard repo
cd $HOME
git clone https://github.com/sfdc-24/Blackboard.git
cd Blackboard

# Setup Python venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install Python bus server deps (stdlib only, but verify)
# (no external deps needed for bus_server.py)

# Setup Node.js clasp wrapper
cd tooling/clasp
npm install
npm link @google/clasp

# Setup environment file (secrets not hardcoded)
cat > ~/.blackboard.env << 'EOF'
# Never commit these to repo (D-18 doctrine)
export BUS_URL="https://sheets.googleapis.com/v4/spreadsheets/120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY/values"
export BUS_SECRET="<your-api-key-here>"
export SFDC_ORG_ID="00Dbm00000wK2ibEAC"
export ANTHROPIC_API_KEY="<claude-key>"
export GROQ_API_KEY="<groq-key>"
EOF

# Load on shell startup
echo 'source ~/.blackboard.env' >> ~/.bashrc

# Build and install bus server
cd ~/Blackboard
packaging/build-deb.sh
sudo apt install -y ~/build/blackboard-bus_1.1.0_all.deb
sudo systemctl enable blackboard-bus

echo "=== Setup Complete ==="
echo "Next: Edit ~/.blackboard.env and run 'source ~/.bashrc'"
echo "Then: sudo systemctl start blackboard-bus && curl http://127.0.0.1:8787/"
```

Save as `~/setup-blackboard-linux.sh` and run:
```bash
chmod +x ~/setup-blackboard-linux.sh
~/setup-blackboard-linux.sh
```

---

## Part 7: Porting PowerShell Scripts to Python/Bash

### Example 1: `wa_watch.ps1` → `src/bus_reader_whatsapp.py`

**Windows PowerShell (current, ~200 lines):**
- Validates range, prevents operator trust boundary violations
- Reads board at watermark, filters for new `to=wa_*` rows
- Routes to WhatsApp gateway
- Persists watermark in file (case-sensitive path handling)

**Linux Python (target, ~100 lines):**
```python
#!/usr/bin/env python3
"""
WhatsApp board reader — port of wa_watch.ps1
Watches Blackboard bus for WhatsApp-routed rows
"""

import os
import json
import time
from datetime import datetime, timedelta
import requests
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WhatsAppBoardReader:
    def __init__(self, bus_url: str, bus_secret: str, watermark_file: str):
        self.bus_url = bus_url
        self.bus_secret = bus_secret
        self.watermark_file = watermark_file
        self.last_row_id = self._load_watermark()
    
    def _load_watermark(self) -> str:
        """Load last-read row_id from persistent file"""
        if os.path.exists(self.watermark_file):
            with open(self.watermark_file, 'r') as f:
                return f.read().strip()
        return None
    
    def _save_watermark(self, row_id: str):
        """Persist current row_id"""
        with open(self.watermark_file, 'w') as f:
            f.write(row_id)
    
    def read_board(self) -> list:
        """Fetch new rows from bus (tail read from last_row_id)"""
        params = {
            'op': 'tail',
            'after_row_id': self.last_row_id or '0',
            'limit': 50
        }
        headers = {'Authorization': f'Bearer {self.bus_secret}'}
        
        try:
            resp = requests.get(f'{self.bus_url}/read', params=params, headers=headers)
            resp.raise_for_status()
            return resp.json().get('rows', [])
        except Exception as e:
            logger.error(f'Board read failed: {e}')
            return []
    
    def route_to_whatsapp(self, row: dict):
        """Send row payload to WhatsApp if to=wa_*"""
        payload = row.get('Payload', '')
        if 'to=wa_' in payload:
            target = payload.split('to=')[1].split('|')[0]
            logger.info(f'Routing to WhatsApp: {target}')
            # Call WhatsApp gateway API here
    
    def watch(self, interval_sec: int = 30):
        """Poll board indefinitely"""
        logger.info('WhatsApp board watcher started')
        while True:
            rows = self.read_board()
            for row in rows:
                self.route_to_whatsapp(row)
                self._save_watermark(row['Row_ID'])
            time.sleep(interval_sec)

if __name__ == '__main__':
    reader = WhatsAppBoardReader(
        bus_url=os.getenv('BUS_URL'),
        bus_secret=os.getenv('BUS_SECRET'),
        watermark_file=os.path.expanduser('~/.blackboard/wa_watermark')
    )
    reader.watch()
```

**Run as systemd service:**
```ini
# /etc/systemd/system/blackboard-wa-watch.service
[Unit]
Description=Blackboard WhatsApp Board Watcher
After=blackboard-bus.service
Wants=blackboard-bus.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Blackboard
ExecStart=/home/ubuntu/Blackboard/venv/bin/python3 src/bus_reader_whatsapp.py
Environment="PATH=/home/ubuntu/Blackboard/venv/bin"
EnvironmentFile=/home/ubuntu/.blackboard.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl enable blackboard-wa-watch
sudo systemctl start blackboard-wa-watch
sudo systemctl status blackboard-wa-watch
```

---

## Part 8: Apps Script Deployment from Linux

### Clasp on Linux (identical to Windows)

```bash
# Login (one-time, stores credentials in ~/.clasprc.json)
clasp login

# Pull current Apps Script source
cd ~/Blackboard
clasp clone 1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp

# OR use existing .clasp.json
clasp pull  # Fetch current production source

# Make changes to apps-script/governor-page-api/Code.js
# (clasp writes server-side JavaScript as .js - it was Code.gs until #167)
# Test locally...

# Push to Apps Script
clasp push

# Create & deploy new version
clasp version "My changes"
clasp deploy <deployment-id>

# List current deployments
clasp deployments
```

**Note:** Clasp works identically on Linux. No script changes needed.

---

## Part 9: GitHub Actions & CI/CD on Linux

### Workflows (Unchanged)
- `.github/workflows/` runs on GitHub's runners, not your local machine
- Linux instance can **trigger** workflows via `git push`
- Instance does NOT run the CI pipeline itself

**From GCloud Linux:**
```bash
cd ~/Blackboard
git checkout -b feature/my-fix
# Make changes...
git add .
git commit -m "Fix: ..."
git push origin feature/my-fix
# Creates PR, GitHub Actions runs tests
```

---

## Part 10: Test Suites on Linux

### JavaScript Tests (no changes needed)
```bash
npm ci  # Install exact dependencies
npm test  # Run 51 tests
npm run mutate  # Run mutation gates (38/38 must pass)
```

### Python Tests (no changes needed)
```bash
source venv/bin/activate
python3 -m pytest tests/  # All tests
python3 -m pytest tests/test_bus.py  # Bus server tests
```

### PowerShell Tests (can run via pwsh, or convert to Python)
```bash
# Option A: Run via PowerShell 7 (if .ps1 has no Windows-specific code)
pwsh -File tests/test_order_release_candidate_preflight.ps1

# Option B: Convert to Python (recommended for long-term)
# Scripts are mostly text processing; Python is simpler on Linux
```

---

## Part 11: Secrets & Credentials Management (Linux Best Practice)

### D-18 Doctrine: Never Hardcode Secrets

**Current (Windows):**
```powershell
# ❌ BAD (if found in repo)
$env:BUS_SECRET = "abc123xyz"
```

**Linux (right way):**

```bash
# Option 1: Environment file (local-only, never in repo)
echo 'export BUS_SECRET="abc123xyz"' >> ~/.blackboard.env
chmod 600 ~/.blackboard.env
source ~/.blackboard.env

# Option 2: Systemd environment file (for services)
sudo tee /etc/blackboard-bus/env > /dev/null << EOF
BUS_SECRET=abc123xyz
BUS_URL=https://...
EOF
sudo chmod 600 /etc/blackboard-bus/env

# Option 3: GCloud Secret Manager (recommended for production)
gcloud secrets create blackboard-bus-secret --data-file=- << EOF
BUS_SECRET=abc123xyz
EOF

# Retrieve in script:
BUS_SECRET=$(gcloud secrets versions access latest --secret="blackboard-bus-secret")
```

### Git Ignore Secrets
```bash
# .gitignore (already in repo)
.env
.env.local
~/.blackboard.env
/etc/blackboard-bus/env
```

Verify before push:
```bash
git diff --cached | grep -i secret  # Should return nothing
```

---

## Part 12: Filesystem & Permissions (Windows → Linux)

### Case Sensitivity
**Windows:** `Case-Insensitive` (`scripts\bus.ps1` = `SCRIPTS\BUS.PS1`)  
**Linux:** `Case-Sensitive` (`scripts/bus.ps1` ≠ `SCRIPTS/BUS.PS1`)

**Action:** All paths in cloned Blackboard repo are lowercase. No changes needed if you:
- Always use lowercase in new scripts
- Use `git clone` (not manual copy) to preserve case

### Line Endings
**Windows:** `CRLF` (`\r\n`)  
**Linux:** `LF` (`\n`)

**Action:** Git handles this automatically:
```bash
git config core.autocrlf input  # On Linux: convert CRLF→LF on commit
```

Attestation files (e.g., X-Ray HTML) have `-text` rule to prevent this:
```
docs/evidence/xray-page-v1/index.html -text
```
No changes needed.

### File Permissions
**Windows:** Not enforced (everyone can read/write)  
**Linux:** Enforce restrictive permissions:

```bash
# Home directory
chmod 700 ~

# Repos
chmod 755 ~/Blackboard
chmod 755 ~/Blackboard/scripts

# Scripts
chmod 755 ~/Blackboard/scripts/*.py
chmod 755 ~/Blackboard/scripts/*.sh

# Secrets
chmod 600 ~/.blackboard.env
sudo chmod 600 /etc/blackboard-bus/env
```

---

## Part 13: Monitoring & Logging on Linux

### Systemd Services
```bash
# View service status
sudo systemctl status blackboard-bus
sudo systemctl status blackboard-wa-watch

# View logs (last 50 lines, follow in real-time)
sudo journalctl -u blackboard-bus -n 50 -f

# View all Blackboard logs
sudo journalctl -g blackboard -f
```

### Log Files (Optional, if services write files)
```bash
# Setup log directory
sudo mkdir -p /var/log/blackboard
sudo chown ubuntu:ubuntu /var/log/blackboard
sudo chmod 750 /var/log/blackboard

# In Python script:
import logging
logging.basicConfig(
    filename='/var/log/blackboard/bus_reader.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
```

---

## Part 14: Migration Checklist

- [ ] **Provision GCloud instance** (e2-medium, Ubuntu 22.04 LTS)
- [ ] **SSH access & authorized_keys** setup
- [ ] **Run setup-blackboard-linux.sh** (or manual steps)
- [ ] **Clone Blackboard repo** from GitHub
- [ ] **Create ~/.blackboard.env** with secrets
- [ ] **Build & install bus server** (`packaging/build-deb.sh`)
- [ ] **Test bus:** `curl http://127.0.0.1:8787/`
- [ ] **Port wa_watch.ps1 → bus_reader_whatsapp.py** (or reuse existing Python)
- [ ] **Port fleet_watch.ps1 → bus_reader_fleet.py**
- [ ] **Test npm:** `npm run build`, `npm test` (51 tests pass)
- [ ] **Test clasp:** `clasp login`, `clasp list`, verify Apps Script access
- [ ] **Setup systemd services** for watchers & bus server
- [ ] **Test GitHub Actions trigger** (push test branch, verify CI runs)
- [ ] **Backup secrets** (GCloud Secret Manager or secure vault)
- [ ] **Document custom changes** made during migration
- [ ] **Decommission Azure VM** (once stable on GCloud)

---

## Part 15: GCloud-Specific Advantages (Why This Migration Matters)

### Scaling
- **Autoscaling:** Increase CPU/RAM dynamically (Azure requires VM resize + downtime)
- **Load balancing:** Multi-instance setup behind Cloud Load Balancer (easy)
- **Containers:** Deploy bus_server as Cloud Run (serverless, $0.04/hour idle)

### Controls
- **Networking:** Cloud VPC, private IPs, service mesh (Anthos)
- **Firewall:** Stateful rules, service accounts, fine-grained IAM
- **Monitoring:** Cloud Logging, Cloud Trace, Prometheus-compatible metrics

### Cost
- **Committed use discounts:** 25-30% savings vs. on-demand (Azure has similar)
- **Sustained use discounts:** Automatic after 30 days
- **Spot VMs:** 70% cheaper for non-critical workloads (testing, builds)

### Linux Benefits
- **Smaller footprint:** Ubuntu 22.04 vs. Windows 11 Pro (fewer resources)
- **Faster boot:** 30 seconds vs. 2-3 minutes (Windows)
- **Fewer updates:** Stable LTS releases, no surprise major upgrades
- **Open-source tooling:** Python, Node, PowerShell 7 all stable on Linux

---

## Part 16: Rollback Plan (If Needed)

### Stay on Azure (Parallel)
1. Keep Azure VM running during migration
2. Run Blackboard on both until GCloud is proven stable
3. Route traffic/agents gradually (feature flag)
4. Decommission Azure once GCloud has 2 weeks of stable uptime

### Revert Steps
```bash
# On GCloud (if failed)
sudo systemctl stop blackboard-bus
sudo apt remove blackboard-bus

# Switch agents back to Azure
# (Update BUS_URL in .blackboard.env to point to Azure)

# Delete GCloud instance
gcloud compute instances delete blackboard-dev --zone=us-central1-a
```

---

## References

- **Blackboard Repo:** https://github.com/sfdc-24/Blackboard
- **GCloud Documentation:** https://cloud.google.com/docs
- **Ubuntu 22.04 LTS:** https://releases.ubuntu.com/jammy/
- **PowerShell 7 on Linux:** https://learn.microsoft.com/en-us/powershell/scripting/install/installing-powershell-on-linux
- **Clasp (Apps Script Deploy):** https://github.com/google/clasp
- **Systemd Services:** https://www.freedesktop.org/software/systemd/man/systemd.service.html

---

**Status:** Ready for Claude's autonomous execution  
**Next Step:** Claude confirms setup approach, then provisions GCloud instance and begins migration  
**Support:** Full tool access for PR creation, branch management, and script porting
