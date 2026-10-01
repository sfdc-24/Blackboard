"""Build a review-draft PDF from frozen Markdown; never live-read APIs."""
from pathlib import Path
from hashlib import sha256
from html import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape,A4
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle

root=Path(__file__).resolve().parents[1]
src=root/'docs/ARCHITECTURE-COMPREHENSIVE-20261001.md'
out=root/'docs/ARCHITECTURE-COMPREHENSIVE-20261001-DRAFT.pdf'
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='Cell',fontName='Helvetica',fontSize=8,leading=10,spaceAfter=0))
styles['BodyText'].fontSize=10
styles['BodyText'].leading=14
story=[]
lines=src.read_text(encoding='utf-8').splitlines()
i=0
while i<len(lines):
    line=lines[i]
    if line.startswith('|'):
        rows=[]
        while i<len(lines) and lines[i].startswith('|'):
            cells=[s.strip() for s in lines[i].strip('|').split('|')]
            if not all(set(c)<=set('-: ') for c in cells):
                rows.append([Paragraph(escape(c),styles['Cell']) for c in cells])
            i+=1
        n=len(rows[0])
        widths=[(landscape(A4)[0]-80)/n]*n
        tab=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
        tab.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dbe7ef')),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#9aa9b4')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.extend([tab,Spacer(1,12)])
        continue
    if line:
        level=len(line)-len(line.lstrip('#'))
        style='Title' if level==1 else 'Heading1' if level==2 else 'Heading2' if level==3 else 'BodyText'
        story.append(Paragraph(escape(line.lstrip('#').strip()),styles[style]))
        story.append(Spacer(1,5))
    i+=1
digest=sha256(src.read_bytes()).hexdigest()
def footer(c,d):
    c.setFont('Helvetica',8)
    c.drawString(40,22,'REVIEW DRAFT - Gemini final AGREE pending | 1 Oct 2026')
    c.drawRightString(landscape(A4)[0]-40,22,str(d.page))
doc=SimpleDocTemplate(str(out),pagesize=landscape(A4),rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40,title='SFDC24 end-to-end architecture - review draft',author='Codex',subject='Source SHA256 '+digest)
doc.build(story,onFirstPage=footer,onLaterPages=footer)
out.with_suffix('.pdf.sha256').write_text(sha256(out.read_bytes()).hexdigest()+'  '+out.name+'\n',encoding='ascii')
print('Source SHA256',digest)
print('PDF SHA256',sha256(out.read_bytes()).hexdigest())
print('PDF',out)
