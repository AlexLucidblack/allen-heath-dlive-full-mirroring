#!/usr/bin/env python3
"""Documentation-only PDF generator. Requires reportlab; app runtime does not."""
import html
import pathlib
import re
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FONT = pathlib.Path("/usr/share/fonts/truetype/dejavu")
# For rebuilding on other systems, set DLIVE_DOC_FONT_DIR to DejaVu TTF files.
import os
FONT = pathlib.Path(os.environ.get("DLIVE_DOC_FONT_DIR", str(FONT)))
for name, filename in (("DV","DejaVuSans.ttf"),("DVB","DejaVuSans-Bold.ttf"),("DVM","DejaVuSansMono.ttf")):
    pdfmetrics.registerFont(TTFont(name, str(FONT / filename)))
pdfmetrics.registerFontFamily("DV",normal="DV",bold="DVB",italic="DV",boldItalic="DVB")
WIDTH, HEIGHT = A4
LEFT = RIGHT = 43
AVAILABLE = WIDTH - LEFT - RIGHT
INK = colors.HexColor("#173044")
TEAL = colors.HexColor("#126a63")
STYLES = {
    "warning": ParagraphStyle("Warning",fontName="DVB",fontSize=10,leading=15,textColor=colors.HexColor("#b91c1c"),backColor=colors.HexColor("#fff1f2"),borderColor=colors.HexColor("#dc2626"),borderWidth=1,borderPadding=10,spaceBefore=10,spaceAfter=16),
    "body": ParagraphStyle("Body",fontName="DV",fontSize=9.2,leading=14,spaceAfter=7,textColor=INK),
    "h1": ParagraphStyle("Chapter",fontName="DVB",fontSize=20,leading=25,spaceBefore=4,spaceAfter=15,textColor=INK,keepWithNext=True),
    "h2": ParagraphStyle("Section",fontName="DVB",fontSize=13,leading=18,spaceBefore=15,spaceAfter=8,textColor=TEAL,keepWithNext=True),
    "h3": ParagraphStyle("Subsection",fontName="DVB",fontSize=10.5,leading=15,spaceBefore=9,spaceAfter=5,textColor=INK,keepWithNext=True),
    "code": ParagraphStyle("Code",fontName="DVM",fontSize=7.6,leading=11,backColor=colors.HexColor("#edf3f5"),borderPadding=8,spaceBefore=6,spaceAfter=11),
    "cell": ParagraphStyle("Cell",fontName="DV",fontSize=8,leading=11,spaceAfter=0,textColor=INK),
    "headcell": ParagraphStyle("HeadCell",fontName="DVB",fontSize=8,leading=11,textColor=colors.white),
    "caption": ParagraphStyle("Caption",fontName="DV",fontSize=8,leading=12,textColor=colors.HexColor("#557080"),spaceAfter=12),
}
def inline(text):
    text = text.replace("\u2011","-").replace("\u2014","-").replace("\u2013","-")
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`",r'<font name="DVM">\1</font>',text)
    text = re.sub(r"\*\*([^*]+)\*\*",r"<b>\1</b>",text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",lambda m:'<font color="#126a63">'+m.group(1)+'</font>',text)
    return text

class GuideDoc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ("Chapter","Section"):
            key = "heading-" + str(getattr(self,"heading_count",0))
            self.heading_count = getattr(self,"heading_count",0)+1
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(),key,level=0 if flowable.style.name=="Chapter" else 1,closed=False)

def page_art(canvas, doc):
    canvas.saveState()
    canvas.setFont("DV",7.3)
    canvas.setFillColor(colors.HexColor("#607d8b"))
    if doc.page>1:
        canvas.drawString(LEFT,HEIGHT-27,"Allen&heath Dlive v2.12 Full Mirroring")
        canvas.drawRightString(WIDTH-RIGHT,HEIGHT-27,"User guide · 0.6.6" if getattr(doc,"language","ru")=="en" else "Руководство · 0.6.6")
        canvas.setStrokeColor(colors.HexColor("#cddde4"))
        canvas.line(LEFT,HEIGHT-34,WIDTH-RIGHT,HEIGHT-34)
    canvas.drawString(LEFT,25,"Windows / macOS · Independent project · firmware 2.12 Rev. 94091" if getattr(doc,"language","ru")=="en" else "Windows / macOS · Независимый проект · прошивка 2.12 Rev. 94091")
    canvas.drawRightString(WIDTH-RIGHT,25,str(doc.page))
    canvas.restoreState()

def parse_markdown(path, skip_title=False):
    lines = path.read_text(encoding="utf-8").splitlines()
    result=[]; i=0; first_heading=True
    while i<len(lines):
        line=lines[i].strip()
        if not line:
            i+=1;continue
        if line.startswith("~~~"):
            i+=1; block=[]
            while i<len(lines) and not lines[i].startswith("~~~"):
                block.append(lines[i]);i+=1
            if block:
                result.append(Paragraph("<br/>".join(html.escape(x).replace(" ","&#160;") for x in block),STYLES["code"]))
            i+=1;continue
        if line.startswith("!["):
            match=re.match(r"!\[([^\]]*)\]\(([^)]+)\)",line)
            if match:
                image_path=path.parent/match.group(2)
                img=Image(str(image_path))
                scale=min(AVAILABLE/img.imageWidth,365/img.imageHeight)
                img.drawWidth=img.imageWidth*scale;img.drawHeight=img.imageHeight*scale
                result.extend([Spacer(1,8),img,Spacer(1,6),Paragraph(inline(match.group(1)),STYLES["caption"])])
            i+=1;continue
        if line.startswith("|"):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith("|"):
                raw=[x.strip() for x in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?",x) for x in raw):rows.append(raw)
                i+=1
            if not rows:continue
            n=max(len(r) for r in rows)
            if n==3: widths=[AVAILABLE*.26,AVAILABLE*.34,AVAILABLE*.40]
            elif n==2:widths=[AVAILABLE*.31,AVAILABLE*.69]
            else:widths=[AVAILABLE/n]*n
            data=[[Paragraph(inline(x),STYLES["headcell"] if ri==0 else STYLES["cell"]) for x in row] for ri,row in enumerate(rows)]
            table=Table(data,colWidths=widths,repeatRows=1,hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#193f48")),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor("#f0f5f7"),colors.white]),
                ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),7),
                ("RIGHTPADDING",(0,0),(-1,-1),7),("TOPPADDING",(0,0),(-1,-1),7),
                ("BOTTOMPADDING",(0,0),(-1,-1),7),("LINEBELOW",(0,0),(-1,0),.5,colors.HexColor("#86aeb7")),
                ("LINEBELOW",(0,1),(-1,-1),.25,colors.HexColor("#d9e5e9"))]))
            result.extend([table,Spacer(1,10)]);continue
        if line.startswith("#"):
            level=len(line)-len(line.lstrip("#")); title=line[level:].strip()
            if skip_title and first_heading:
                first_heading=False;i+=1;continue
            first_heading=False
            style="h1" if level==1 else "h2" if level==2 else "h3"
            result.append(Paragraph(inline(title),STYLES[style]));i+=1;continue
        bullet=re.match(r"^(\d+\.|-)\s+(.*)",line)
        if bullet:
            label=bullet.group(1)
            result.append(Paragraph(inline(bullet.group(2)),ParagraphStyle("List",parent=STYLES["body"],leftIndent=17,firstLineIndent=0,bulletIndent=0),bulletText="•" if label=="-" else label))
            i+=1;continue
        if line.startswith(">"):
            if line.startswith("> [!CAUTION]"):
                warning=[];i+=1
                while i<len(lines) and lines[i].strip().startswith(">"):
                    warning.append(lines[i].strip().lstrip("> "));i+=1
                result.append(Paragraph(inline(" ".join(warning)),STYLES["warning"]))
            else:
                result.append(Paragraph(inline(line.lstrip("> ")),STYLES["caption"]));i+=1
            continue
        paragraph=[line];i+=1
        while i<len(lines) and lines[i].strip() and not re.match(r"^(#|\||!\[|~~~|\d+\.\s|-\s|>)",lines[i].strip()):
            paragraph.append(lines[i].strip());i+=1
        result.append(Paragraph(inline(" ".join(paragraph)),STYLES["body"]))
    return result

def build(language="ru"):
    en=language=="en"
    output=DOCS/("USER_GUIDE_EN.pdf" if en else "USER_GUIDE_RU.pdf")
    story=[]
    banner=Image(str(DOCS/("assets/banner-en.png" if en else "assets/banner.png")),width=AVAILABLE,height=AVAILABLE*400/1280)
    story.extend([Spacer(1,16),banner,Spacer(1,36)])
    story.append(Paragraph("User guide" if en else "Подробная инструкция",ParagraphStyle("Cover",fontName="DVB",fontSize=29,leading=36,textColor=INK,spaceAfter=14)))
    story.append(Paragraph("Windows and macOS" if en else "Windows и macOS",ParagraphStyle("CoverSub",fontName="DV",fontSize=20,leading=27,textColor=TEAL,spaceAfter=21)))
    story.append(Paragraph("Connecting two dLive systems, parameter mirroring, active/standby operation, recovery and automatic log cleanup." if en else "Подключение двух систем dLive, передача параметров, резервирование, возврат после обрыва и автоматическая очистка логов.",STYLES["body"]))
    story.append(Spacer(1,12))
    story.append(Paragraph("Application 0.6.6 · dLive 2.12 Rev. 94091<br/>English edition · 25 October 2026" if en else "Приложение 0.6.6 · dLive 2.12 Rev. 94091<br/>Редакция от 25 октября 2026 года",STYLES["caption"]))
    story.append(Spacer(1,24))
    contents=("1. Network topology and requirements","2. Complete Windows/macOS installation in section 4","3. Operation, redundancy and log cleanup","4. Feature reference and limitations","5. Troubleshooting, diagnostics and sources") if en else ("1. Схема подключения и требования","2. Полная установка Windows и macOS в разделе 4","3. Работа, резервирование и очистка логов","4. Матрица функций и ограничения","5. Ошибки, диагностика и источники")
    for text in contents:
        story.append(Paragraph(text,STYLES["body"]))
    story.append(Spacer(1,20))
    story.append(Paragraph("IMPORTANT: Before connecting the application, load the same scene on both consoles. Mirror forwards subsequent changes; it does not automatically align the initial state." if en else "ВАЖНО: Перед подключением приложения загрузите одну и ту же сцену на оба пульта. Mirror передаёт последующие изменения и не выравнивает исходное состояние автоматически.",STYLES["warning"]))
    story.append(Paragraph("Audio transport/switching, Scene/Show Store transfer and phantom 48V mirroring are excluded. The project name does not imply hardware certification of every function.",STYLES["caption"]) if en else Paragraph("Не передаёт аудио, сохранение сцен/Show и фантомное питание 48V. Название проекта не означает полной аппаратной сертификации всех функций.",STYLES["caption"]))
    story.append(PageBreak())
    sections=("USER_GUIDE_EN.md","FEATURES_EN.md","TROUBLESHOOTING_EN.md","SOURCES_EN.md") if en else ("USER_GUIDE_RU.md","FEATURES_RU.md","TROUBLESHOOTING_RU.md","SOURCES.md")
    for index,name in enumerate(sections):
        if index:story.append(PageBreak())
        story.extend(parse_markdown(DOCS/name,skip_title=False))
    doc=GuideDoc(str(output),pagesize=A4,leftMargin=LEFT,rightMargin=RIGHT,topMargin=49,bottomMargin=47,title="Allen&heath Dlive v2.12 Full Mirroring — User guide" if en else "Allen&heath Dlive v2.12 Full Mirroring — руководство",author="Independent dLive Full Mirroring project",pageCompression=1)
    doc.language=language
    doc.build(story,onFirstPage=page_art,onLaterPages=page_art)
    print(output)

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--lang",choices=("ru","en"),default="ru")
    build(parser.parse_args().lang)
