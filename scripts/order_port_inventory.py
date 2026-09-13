#!/usr/bin/env python3
"""What in the ORDER lane is actually Windows-only?

An inventory BEFORE any porting. "Ten PowerShell scripts need Windows" is a
claim I have been repeating without ever counting, and a port plan built on a
guess is how a migration overruns. This counts.

Each pattern is tagged with what it costs to move:
  BLOCKER   - no Linux equivalent in PowerShell; needs a different mechanism
  REWRITE   - has a Linux equivalent but the call must change
  CHECK     - usually portable, but worth eyeballing
Read-only. Nothing is modified.
"""
import collections
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

PATTERNS = [
    # (severity, label, regex)
    ("BLOCKER", "Windows Scheduled Task",
     r"Register-ScheduledTask|New-ScheduledTask|Get-ScheduledTask|Unregister-ScheduledTask|schtasks"),
    ("BLOCKER", "Windows registry",
     r"HK(LM|CU|CR|U|CC):"),
    ("BLOCKER", "WMI / CIM",
     r"Get-CimInstance|Get-WmiObject|New-CimSession"),
    ("BLOCKER", "COM object",
     r"New-Object\s+-ComObject|\[System\.Runtime\.InteropServices"),
    ("BLOCKER", "Windows event log",
     r"Get-EventLog|Write-EventLog|New-EventLog"),
    ("BLOCKER", "Windows service control",
     r"Get-Service|Start-Service|Stop-Service|New-Service|Set-Service"),
    ("BLOCKER", "WSL bridge",
     r"\bwsl(\.exe)?\b|/mnt/c/"),
    ("REWRITE", "DPAPI / SecureString-to-plaintext",
     r"ConvertTo-SecureString|ConvertFrom-SecureString|ProtectedData"),
    ("REWRITE", "Windows ACL",
     r"Get-Acl|Set-Acl|icacls"),
    ("REWRITE", "hardcoded drive path",
     r"[\"'][A-Za-z]:\\\\|[\"'][A-Za-z]:/"),
    ("REWRITE", "Windows env var",
     r"\$env:(USERPROFILE|APPDATA|LOCALAPPDATA|PROGRAMFILES|SYSTEMROOT|COMPUTERNAME|USERNAME)"),
    ("REWRITE", "powershell.exe by name",
     r"powershell\.exe|pwsh\.exe|cmd\.exe|cmd /c"),
    ("REWRITE", "Start-Process -Verb / -WindowStyle",
     r"Start-Process[^\n]*-(Verb|WindowStyle)"),
    ("CHECK", "backslash path join",
     r"\\\\(scripts|tests|logs|state|outputs)\\\\"),
    ("CHECK", "Azure reference",
     r"(?i)akatia|azure|windows\.net"),
    ("CHECK", "CRLF-sensitive text handling",
     r"`r`n|\\r\\n"),
    ("CHECK", "Test-Path -PathType Leaf on a device",
     r"NUL\b|\\\\\\\\\.\\\\"),
]

COMPILED = [(sev, label, re.compile(rx)) for sev, label, rx in PATTERNS]

files = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
    for fn in filenames:
        if fn.lower().endswith((".ps1", ".psm1")):
            files.append(os.path.join(dirpath, fn))

by_file = collections.OrderedDict()
totals = collections.Counter()
label_files = collections.defaultdict(set)

for path in sorted(files):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        continue
    hits = collections.Counter()
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue                      # a comment mentioning Azure is not a call
        for sev, label, rx in COMPILED:
            if rx.search(line):
                hits[(sev, label)] += 1
                totals[(sev, label)] += 1
                label_files[label].add(os.path.basename(path))
    by_file[path] = (len(lines), hits)

print("ORDER lane port inventory")
print("=" * 78)
print("{0:<34} {1:>6}  {2}".format("file", "lines", "windows-only findings"))
print("-" * 78)
for path, (nlines, hits) in by_file.items():
    name = os.path.basename(path)
    blockers = sum(n for (sev, _), n in hits.items() if sev == "BLOCKER")
    rewrites = sum(n for (sev, _), n in hits.items() if sev == "REWRITE")
    checks = sum(n for (sev, _), n in hits.items() if sev == "CHECK")
    flag = "  <-- BLOCKED" if blockers else ""
    print("{0:<34} {1:>6}  B={2:<4} R={3:<4} C={4}{5}".format(
        name, nlines, blockers, rewrites, checks, flag))

print("")
print("By finding, worst first")
print("-" * 78)
for (sev, label), n in sorted(totals.items(), key=lambda kv: (kv[0][0], -kv[1])):
    print("{0:<9} {1:<34} {2:>4} hits in {3} file(s)".format(
        sev, label, n, len(label_files[label])))
    print("          " + ", ".join(sorted(label_files[label])))

tot_lines = sum(n for n, _ in by_file.values())
blocked = [os.path.basename(p) for p, (_, h) in by_file.items()
           if any(sev == "BLOCKER" for sev, _ in h)]
print("")
print("{0} files, {1} lines of PowerShell.".format(len(by_file), tot_lines))
print("{0} file(s) contain a BLOCKER: {1}".format(len(blocked), ", ".join(blocked) or "none"))
