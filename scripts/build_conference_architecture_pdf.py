"""Render the agreed conference design as a compact two-page architecture brief."""
from pathlib import Path
import hashlib
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output' / 'pdf' / 'Blackboard-Conference-Architecture-v1.pdf'
OUT.parent.mkdir(parents=True, exist_ok=True)
FONT = Path(r'C:\Windows\Fonts')
pdfmetrics.registerFont(TTFont('Segoe', str(FONT / 'segoeui.ttf')))
pdfmetrics.registerFont(TTFont('Segoe-Bold', str(FONT / 'segoeuib.ttf')))
pdfmetrics.registerFontFamily('Segoe', normal='Segoe', bold='Segoe-Bold')
W, H = landscape(A4)
NAVY, INK, MUTED = map(HexColor, ('#142C45', '#18364E', '#52697C'))
BLUE, TEAL, LINE, PALE = map(HexColor, ('#1673CF', '#148572', '#D4DFE8', '#F3F7FA'))
C = canvas.Canvas(str(OUT), pagesize=(W, H), invariant=1)
C.setTitle('Blackboard Conference - Architecture and Delivery Contract v1')
C.setAuthor('Codex')
C.setSubject('Private LiveKit conference POC; architecture, ownership and acceptance')

def rect(x, t, w, h, fill=white, stroke=LINE, radius=9):
    C.setFillColor(fill)
    C.setStrokeColor(stroke)
    C.setLineWidth(.7)
    C.roundRect(x, H-t-h, w, h, radius, fill=1, stroke=1)

def para(text, x, t, w, size=9, color=INK, bold=False, maxh=None, leading=None):
    style = ParagraphStyle('p', fontName='Segoe-Bold' if bold else 'Segoe',
                           fontSize=size, leading=leading or size*1.34,
                           textColor=color, spaceAfter=0)
    p = Paragraph(text, style)
    pw, ph = p.wrap(w, 900)
    if maxh is not None and ph > maxh+.1:
        raise ValueError(f'Paragraph overflow ({ph:.1f}>{maxh}): {text[:90]}')
    p.drawOn(C, x, H-t-ph)
    return ph

def box(x, t, w, h, title, body, accent=BLUE):
    rect(x,t,w,h)
    C.setFillColor(accent)
    C.roundRect(x, H-t-h, 4, h, 2, fill=1, stroke=0)
    para(title,x+14,t+11,w-28,11,bold=True,maxh=30)
    para(body,x+14,t+37,w-28,8.4,maxh=h-44)

def arrow(points, color=BLUE, dashed=False, both=False):
    C.setStrokeColor(color)
    C.setFillColor(color)
    C.setLineWidth(1.5)
    C.setDash(3,3) if dashed else C.setDash()
    p=C.beginPath()
    p.moveTo(points[0][0],H-points[0][1])
    for x,y in points[1:]: p.lineTo(x,H-y)
    C.drawPath(p)
    C.setDash()
    def head(a,b):
        import math
        dx,dy=b[0]-a[0],b[1]-a[1]
        length=math.hypot(dx,dy)
        ux,uy=dx/length,dy/length
        p=C.beginPath()
        p.moveTo(b[0],H-b[1])
        p.lineTo(b[0]-6*ux+3*uy,H-(b[1]-6*uy-3*ux))
        p.lineTo(b[0]-6*ux-3*uy,H-(b[1]-6*uy+3*ux))
        p.close()
        C.drawPath(p,fill=1,stroke=0)
    head(points[-2],points[-1])
    if both: head(points[1],points[0])

def header(page, kicker, title, sub):
    C.setFillColor(PALE); C.rect(0,0,W,H,fill=1,stroke=0)
    para(kicker.upper(),32,24,700,9,TEAL,bold=True)
    para(title,32,43,775,25,NAVY,bold=True,maxh=36)
    para(sub,32,83,770,10,MUTED,maxh=30)
    C.setStrokeColor(LINE); C.line(32,28,W-32,28)
    para('BLACKBOARD  /  CONF-LINE-v1  /  27 SEP 2026 UTC',32,H-23,600,7,MUTED)
    para(f'{page} / 2',W-67,H-23,40,7,MUTED)

header(1,'Target architecture - private POC', 'One room. A coordinated team.',
       'Talk with four named AI participants, share the working board, and turn decisions into assigned work.')
rect(32,119,777,44,NAVY,NAVY)
para('BLACKBOARD MOTHERBOARD',46,128,240,10,white,bold=True)
para('Policies, roles, permissions and stop controls  |  Redacted decisions, receipts and learning return',286,130,506,8.4,white,maxh=24)

box(32,189,193,115,'You + private console',
    'Verified owner sign-in<br/>Mic, speakers and screen share<br/>Agenda, choices and current speaker<br/>Working board with saved/unsaved state')
box(323,189,193,115,'LiveKit Cloud room',
    'WebRTC media + screen tracks<br/>Distinct participant identities<br/>Explicit agent dispatch<br/>LiveKit hosts the media path')
box(614,189,195,115,'Chair + agent workers',
    'Grok / Claude / Codex / Gemini<br/>Deepgram Flux + OpenAI TTS<br/>Concurrent reasoning; one AI voice<br/>Human barge-in cancels stale audio',TEAL)
arrow([(225,227),(323,227)],both=True)
para('audio / screen',236,209,78,7.3,BLUE)
arrow([(516,227),(614,227)],both=True)
para('named tracks',529,209,75,7.3,BLUE)

box(32,347,193,91,'Gateway - Cloud Run',
    'Owner identity and room authorization<br/>Short-lived, scoped join tokens<br/>Secrets remain server-side',TEAL)
box(323,347,193,91,'Broker - Cloud Run',
    'Yjs / Hocuspocus shared board<br/>Scoped source context + event IDs<br/>Reconnect, durable ack and outbox',TEAL)
box(614,347,195,91,'Durable work + return',
    'Firestore: fenced board revisions<br/>BCB / OKF: approved outcomes<br/>Existing WhatsApp outbox: notices',TEAL)
arrow([(128,347),(128,304)],TEAL)
para('admit',139,317,56,7.3,TEAL)
arrow([(225,275),(269,275),(269,379),(323,379)],TEAL,both=True)
para('shared board',235,313,72,7.2,TEAL)
arrow([(666,304),(666,324),(464,324),(464,347)],TEAL,both=True)
para('context + typed actions',470,311,166,7.2,TEAL)
arrow([(516,379),(614,379)],TEAL,both=True)
para('commit / receipt',520,360,94,7.2,TEAL)
arrow([(809,391),(824,391),(824,141),(809,141)],TEAL,dashed=True)

rect(32,455,777,75,white)
para('BUILT FOR CONTINUITY',46,465,220,8.5,TEAL,bold=True)
para('One room epoch + turn IDs; a speaking lease blocks duplicate playback. Shared-board edits have a real database receipt. The diagram is a target; provider and human-heard acceptance come from the live POC.',46,481,362,8,maxh=43)
para('EXTENSIONS WITHOUT BLOCKING THE FIRST CALL',434,465,354,8.5,TEAL,bold=True)
para('Canadian phone number via SIP after browser acceptance. Zoom + the Ubuntu meeting presenter remain separate bridges. Salesforce reads are governed; confirmed writes remain a later capability.',434,481,354,8,maxh=43)
para('Docs: <link href="https://docs.livekit.io/reference/internals/livekit-sfu/" color="#1673CF">LiveKit SFU</link> | '
     '<link href="https://docs.livekit.io/frontends/build/authentication/" color="#1673CF">Authentication</link> | '
     '<link href="https://docs.livekit.io/agents/server/agent-dispatch/" color="#1673CF">Agent dispatch</link> | '
     '<link href="https://tiptap.dev/docs/hocuspocus/guides/persistence" color="#1673CF">Yjs persistence</link> | '
     '<link href="https://docs.cloud.google.com/run/docs/triggering/websockets" color="#1673CF">Cloud Run WebSockets</link>',32,542,777,7.5,MUTED)
C.showPage()

header(2,'Ownership + release contract','Build one useful slice, then extend it.',
       'Code starts after this PDF is delivered and package owners acknowledge this version. Claude integrates and releases.')
tx, tw = 32,777
widths=[123,217,437]
tops=[119,143,178,213,248,283,318]
rect(tx,119,tw,24,NAVY,NAVY,3)
for x,label in [(tx+10,'OWNER / PACKAGE'),(tx+133,'EXCLUSIVE CODE BOUNDARY'),(tx+350,'FIRST DELIVERABLE + INDEPENDENT REVIEW')]:
    para(label,x,126,430,7.7,white,bold=True,maxh=13)
rows=[
 ('Claude / WP1 + WP2','chair/ + gateway/ + integration','Floor + two provider voices; owner tokens and private room. Review: Codex + Cursor.'),
 ('Cursor* / WP3','console/','Join, real audio, screen share, dialogue cards and live board. Review: Claude + Codex.'),
 ('Codex / WP4','broker/ + architecture contracts','Yjs sync, fenced Firestore save, context and outcome receipts. Review: Claude + Cursor.'),
 ('Claude / WP5','sip/ - after browser POC','SIP + Canadian number + authorized caller entry; separate account gate. Review: Codex + Cursor.'),
 ('Grok / WP6','personas/ - Claude commits data','Persona style cards, agenda and roundtable cases. Grok is content owner; one repository writer.'),
 ('Gemini + Copilot','Review channels; no product folders','Gemini: multimodal, latency and failure review. Copilot: PR review. Model opinion is not test evidence.'),
]
for i,(owner,folder,body) in enumerate(rows):
    top=143+i*35
    rect(tx,top,tw,35,white if i%2==0 else HexColor('#EAF1F6'),LINE,0)
    para(owner,tx+10,top+7,113,8.2,bold=True,maxh=26)
    para(folder,tx+133,top+7,202,8.2,maxh=26)
    para(body,tx+350,top+6,417,8.2,maxh=28)
para('All paths under cloud/conference-line/. One writer and worktree per package. *Cursor acceptance required; Claude records any ownership transfer before edits.',32,358,777,7.4,MUTED,maxh=22)

rect(32,389,377,145,white)
para('THE POC MUST PROVE',45,400,350,9,TEAL,bold=True)
para('20-minute owner + four-persona call; warm introduction; 10 addressed exchanges; 2 roundtables; 2 barge-ins; zero overlapping AI speech. Share a screen, change a board card, reconnect, restart the broker, and read the saved revision back in two clients. Close with approved decisions and one receipt per destination.',45,420,350,8.2,maxh=72)
para('Measure: response p50 &lt;1.5 s / p95 &lt;3 s (targets), interrupt stop time, board-save time, provider usage. End closes mic, providers and pending work.',45,497,350,7.7,maxh=30)

rect(424,389,385,145,white)
para('SEQUENCE + ACCOUNT DEPENDENCIES',437,400,358,9,TEAL,bold=True)
para('<b>0-4h:</b> same-version ACKs, access and contract freeze. <b>4-12h:</b> real two-agent room + shared board. <b>12-24h:</b> four agents, screen context and owner rehearsal. Working-hour targets after prerequisites; Claude confirms integration ETA.',437,420,358,8.2,maxh=58)
para('<b>Cost:</b> LiveKit Build starts at USD 0/month. Illustrative 20-min session subtotal: USD 0.80 for 4 agent sessions + USD 0.13 shared Flux. Excludes models/TTS, transfer, GCP and phone. Keys in Secret Manager; no paid upgrade selected.',437,478,358,7.7,maxh=51)
para('Privacy: work saves approved content; social/confidential are ephemeral. Cloud providers still process media. '
     '<link href="https://livekit.com/pricing" color="#1673CF">Pricing</link> / '
     '<link href="https://docs.livekit.io/deploy/admin/quotas-and-limits/" color="#1673CF">quotas</link> checked 27 Sep 2026 UTC.',32,542,777,7.3,MUTED,maxh=22)
C.save()
digest=hashlib.sha256(OUT.read_bytes()).hexdigest()
print(f'PDF={OUT}')
print(f'SHA256={digest}')
print(f'BYTES={OUT.stat().st_size}')
