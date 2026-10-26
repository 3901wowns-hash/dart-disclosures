"""공시 원문에서 핵심 항목을 뽑아 docs/data/summaries.json 에 저장한다. (AI 없이 규칙 기반, 무료)

- DART 원문 API(document.xml)로 받은 공시의 표를 읽어 "1. 항목 → 값"을 정리한다.
- 잠정실적은 실적표에서 매출 · 영업이익 · 순이익과 증감률을 뽑는다.
- 공시 종류별로 한 줄 제목을 만들고, 맞는 틀이 없으면 앞쪽 핵심 항목으로 대신한다.
- 한 번 정리한 공시는 다시 받지 않는다. (PARSER_VERSION 을 올리면 다시 정리)
"""

import io
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta
from html.parser import HTMLParser

import requests

from collect import BASE_DIR, CONFIG_PATH, DATA_PATH, KST, load_api_key

SUMMARY_PATH = BASE_DIR / "docs" / "data" / "summaries.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
PARSER_VERSION = 1
MAX_PER_RUN = 50  # 한 번 실행에 새로 정리할 최대 건수

# 카드에서 뺄 항목 (절차 · 연락처 · 참석 여부 등)
SKIP_LABEL = re.compile(
    r"이사회|참석|연락처|정보제공|위탁투자중개업자|관련공시|주문수량|보유현황|감사|"
    r"정정관련|정정사항|기타 투자판단|향후 회사구조개편|주식매수청구권|산출근거|법인가치|신설"
)
NOTE_LABEL = re.compile(r"기타 투자판단")
METRICS = ["매출액", "영업수익", "영업이익", "법인세비용차감전계속사업이익", "당기순이익", "지배기업 소유주지분 순이익"]
UNIT_TO_WON = {"조원": 10**12, "억원": 10**8, "백만원": 10**6, "천원": 10**3, "원": 1}
MAX_VALUE_LEN = 300


# ── 원문 받기 · 표 읽기 ──────────────────────────────────

def fetch_document(api_key, rcept_no):
    resp = requests.get(DOCUMENT_URL, params={"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=60)
    resp.raise_for_status()
    if not zipfile.is_zipfile(io.BytesIO(resp.content)):
        raise RuntimeError(f"원문을 받지 못함: {resp.text[:200]}")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        main = f"{rcept_no}.xml" if f"{rcept_no}.xml" in names else names[0]
        raw = zf.read(main)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="ignore")


class TableReader(HTMLParser):
    """HTML(거래소 서식)과 DART XML(주요사항보고서) 표를 모두 행(셀 문자열 목록)으로 읽는다."""

    CELL = {"td", "th", "te", "tu"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.row, self.cell, self.skip = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self.row = []
        elif tag in self.CELL and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip -= 1
        elif tag in self.CELL and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.tables and any(self.row):
                self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip:
            self.cell.append(data)


def read_tables(raw):
    reader = TableReader()
    reader.feed(raw)
    return reader.tables


# ── 값 다듬기 ─────────────────────────────────────────

def is_empty(cell):
    return cell.strip() in ("", "-", "--")


def clean_date(text):
    """'2026년 08월 24일', '2026.08.24' → '2026-08-24'"""
    to_iso = lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    text = re.sub(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", to_iso, text)
    return re.sub(r"(?<![\d.])(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})(?![\d])\.?", to_iso, text)


def to_number(text):
    t = text.replace(",", "").replace(" ", "")
    if re.fullmatch(r"\(\d+(\.\d+)?\)", t):  # (1234) 표기 = 음수
        t = "-" + t[1:-1]
    try:
        return float(t)
    except ValueError:
        return None


def fmt_won(won):
    """원 단위 금액을 '1.23조원', '515억원' 처럼 읽기 쉽게 바꾼다."""
    sign = "-" if won < 0 else ""
    w = abs(won)
    if w >= 10**12:
        return f"{sign}{w / 10**12:,.2f}".rstrip("0").rstrip(".") + "조원"
    if w >= 10**10:
        return f"{sign}{w / 10**8:,.0f}억원"
    if w >= 10**8:
        return f"{sign}{w / 10**8:,.1f}".rstrip("0").rstrip(".") + "억원"
    return f"{sign}{w:,.0f}원"


def fmt_pct(text):
    n = to_number(text)
    return f"{n:+,.1f}%" if n is not None else text


def shorten(text, limit=MAX_VALUE_LEN):
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ── 번호 붙은 항목 읽기 ──────────────────────────────────

def main_table(tables):
    """'1. ' 로 시작하는 행이 있는 첫 표. 정정 공시는 정정 내역 표를 건너뛴다."""
    for t in tables:
        first = next((r[0] for r in t if re.match(r"^1\.\s", r[0])), None)
        if first and "정정" not in first:
            return t
    return None


def read_sections(table):
    """[(항목명, [[셀...], ...]), ...] — 번호 행이 새 항목을 열고, 번호 없는 행은 앞 항목에 붙는다."""
    sections = []
    for row in table:
        head = row[0]
        m = re.match(r"^\d+\.\s*(.+)", head)
        if m:
            sections.append((m[1].strip(), [row[1:]]))
        elif head.startswith("- ") and len(head) <= 40:  # '- 취득물건명' 같은 짧은 하위 항목만 (긴 '- ...' 는 본문)
            sections.append((head[2:].strip(), [row[1:]]))
        elif sections:
            sections[-1][1].append(row)
    return sections


def section_text(rows):
    parts = []
    for row in rows:
        cells = [c for c in row if c]
        if not cells or is_empty(cells[-1]):  # 값 칸이 비어 있는 행 ('기타주식 -', '시작일 -')
            continue
        parts.append(" ".join(c for c in cells if not is_empty(c)))
    return clean_date(" · ".join(parts))


def find(sections, label_pattern, sub=None):
    """항목명이 맞는 항목에서, sub(칸 이름 앞부분 정규식)가 있으면 그 칸 다음의 첫 값을, 없으면 첫 값을 돌려준다."""
    for label, rows in sections:
        if not re.search(label_pattern, label):
            continue
        for row in rows:
            cells = [c for c in row if c]
            if sub:
                idx = next((k for k, c in enumerate(cells) if re.match(sub, c)), None)
                if idx is not None:
                    value = next((c for c in cells[idx + 1:] if not is_empty(c)), None)
                    if value:
                        return clean_date(value)
            else:
                value = next((c for c in cells if not is_empty(c)), None)
                if value:
                    return clean_date(value)
    return None


def won_of(text):
    n = to_number(text or "")
    return fmt_won(n) if n is not None else text


def funds(sections):
    """'자금조달의 목적' 표에서 금액이 있는 용도만 '시설자금 4.55조원' 처럼 돌려준다."""
    out = []
    for label, rows in sections:
        if not re.search(r"자금조달의 목적", label):
            continue
        for row in rows:
            cells = [c for c in row if c]
            if len(cells) >= 2 and not is_empty(cells[-1]) and to_number(cells[-1]) is not None:
                purpose = re.sub(r"\s*\(원\)", "", cells[0])
                out.append(f"{purpose} {won_of(cells[-1])}")
    return ", ".join(out) or None


# ── 잠정실적 ──────────────────────────────────────────

def quarter_of(start, end):
    """'2026-04-01', '2026-06-30' → '26.2Q'. 분기가 아니면 '2026-01-01~2026-06-30'."""
    try:
        s, e = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return None
    if s.year == e.year and s.day == 1 and s.month in (1, 4, 7, 10) and e.month == s.month + 2:
        return f"{s:%y}.{(s.month - 1) // 3 + 1}Q"
    return f"{start}~{end}"


def read_earnings(tables):
    unit, period, metrics = None, None, {}
    for t in tables:
        for row in t:
            for c in row:
                m = re.search(r"단위\s*:\s*([^,)\s]+)", c)
                if m and m[1] in UNIT_TO_WON:
                    unit = m[1]
            # 실적기간 '당기실적 2026-04-01 ~ 2026-06-30' 으로 분기를 정한다. (회사마다 '26.2Q', '2026년 2분기' 등 표기가 달라서)
            if not period and len(row) >= 4 and row[0] == "당기실적" and row[2] == "~":
                period = quarter_of(row[1], row[3])
            if len(row) >= 9 and row[0] in METRICS and row[1] == "당해실적":
                metrics[row[0]] = row[2:9]  # 정정 공시는 뒤에 나오는 정정 후 값이 덮어쓴다
    if not unit or not metrics:
        return None

    rows = []
    for name in METRICS:
        v = metrics.get(name)
        if not v or is_empty(v[0]):
            continue
        cur, prev, qoq, qoq_turn, base, yoy, yoy_turn = v
        n = to_number(cur)
        rows.append({
            "name": name,
            "value": fmt_won(n * UNIT_TO_WON[unit]) if n is not None else cur,
            "qoq": qoq_turn if not is_empty(qoq_turn) else (fmt_pct(qoq) if not is_empty(qoq) else "-"),
            "yoy": yoy_turn if not is_empty(yoy_turn) else (fmt_pct(yoy) if not is_empty(yoy) else "-"),
        })
    return {"period": period, "unit": unit, "rows": rows} if rows else None


def earnings_headline(earnings):
    picks = []
    for names, short in ((("매출액", "영업수익"), None), (("영업이익",), "영업이익"), (("당기순이익",), "순이익")):
        row = next((r for r in earnings["rows"] if r["name"] in names), None)
        if row:
            label = short or ("매출" if row["name"] == "매출액" else "영업수익")
            yoy = f" (YoY {row['yoy']})" if row["yoy"] != "-" else ""
            picks.append(f"{label} {row['value']}{yoy}")
    prefix = f"{earnings['period']} " if earnings["period"] else ""
    return prefix + " · ".join(picks)


# ── 종류별 한 줄 제목 ─────────────────────────────────────

def join(*parts):
    return " · ".join(p for p in parts if p)


def headline_for(category, report_nm, s):
    if category == "자기주식":
        if "처분" in report_nm:
            shares, amount, purpose = find(s, r"처분예정주식", "보통주식"), find(s, r"처분예정금액", "보통주식"), find(s, r"처분목적")
            return join(f"보통주 {shares}주 처분" if shares else None, won_of(amount) if amount else None, purpose)
        if "신탁" in report_nm:
            return join(f"신탁계약 {won_of(find(s, r'계약금액'))}" if find(s, r"계약금액") else None,
                        find(s, r"계약목적"), find(s, r"계약기간"))
        if "소각" in report_nm:
            return join(f"보통주 {find(s, r'소각할 주식', '보통주식')}주 소각" if find(s, r"소각할 주식", "보통주식") else None,
                        won_of(find(s, r"소각예정금액")) if find(s, r"소각예정금액") else None)
        shares, amount, purpose = find(s, r"취득예정주식", "보통주식"), find(s, r"취득예정금액", "보통주식"), find(s, r"취득목적")
        return join(f"보통주 {shares}주 취득" if shares else None, won_of(amount) if amount else None, purpose)

    if category == "유형자산 취득·처분":
        kind = "처분" if ("처분" in report_nm or "양도" in report_nm) else "취득"
        name = find(s, rf"{kind}물건명") or find(s, rf"{kind}물건")
        amount = find(s, rf"{kind}내역", rf"{kind}(금액|가액)") or find(s, rf"{kind}(금액|가액)")
        ratio = find(s, rf"{kind}내역", r"자산총액대비")
        purpose = find(s, rf"{kind}목적")
        if purpose and name and name in purpose:  # '공장 증설' · '패키지기판 공장 증설' 처럼 겹치면 목적만
            name = None
        return join(purpose, name, f"{won_of(amount)} {kind}" if amount else None,
                    f"자산총액 대비 {ratio}%" if ratio else None)

    if category == "타법인주식 취득·처분":
        kind = "처분" if ("처분" in report_nm or "양도" in report_nm) else "취득"
        target = find(s, r"발행회사", "회사명") or find(s, r"발행회사")
        amount = find(s, rf"{kind}내역", rf"{kind}(금액|가액)")
        return join(target, f"{won_of(amount)} {kind}" if amount else None, find(s, rf"{kind}목적"))

    if category == "합병·분할":
        partner = find(s, r"합병상대회사", "회사명")
        return join(f"{partner} 합병" if partner else find(s, r"분할방법"), find(s, r"합병목적|분할목적"),
                    f"합병비율 {find(s, r'합병비율')}" if find(s, r"합병비율") else None,
                    f"합병기일 {find(s, r'합병일정', '합병기일')}" if find(s, r"합병일정", "합병기일") else None)

    if category == "유상증자":
        shares = find(s, r"신주의 종류와 수", "보통주식")
        method = find(s, r"증자방식")
        price = find(s, r"신주 발행가액", "보통주식")
        return join(method, f"신주 {shares}주" if shares else None, f"발행가 {price}원" if price else None, funds(s))

    if category == "무상증자":
        return join(f"신주 {find(s, r'신주의 종류와 수', '보통주식')}주" if find(s, r"신주의 종류와 수", "보통주식") else None,
                    f"1주당 {find(s, r'1주당 신주배정', '보통주식')}주 배정" if find(s, r"1주당 신주배정", "보통주식") else None,
                    f"신주배정기준일 {find(s, r'신주배정기준일')}" if find(s, r"신주배정기준일") else None)

    if category == "사채 발행":
        amount = find(s, r"사채의 권면.*총액")
        kind = find(s, r"사채의 종류", r"종류$")
        rate = find(s, r"사채의 이율", r"표면이자율")
        return join(kind, f"{won_of(amount)} 발행" if amount else None, f"표면이자율 {rate}%" if rate else None, funds(s))

    if category == "영업양수도":
        return join(find(s, r"양수영업|양도영업"), won_of(find(s, r"양수가액|양도가액")) if find(s, r"양수가액|양도가액") else None,
                    find(s, r"양수목적|양도목적"))
    return None


# ── 공시 한 건 정리 ───────────────────────────────────────

def summarize(item, raw):
    tables = read_tables(raw)
    result = {"v": PARSER_VERSION, "headline": None, "fields": [], "earnings": None, "note": None}

    for t in tables[:2]:
        for row in t:
            if row and row[0] == "종속회사인" and len(row) > 1:
                result["fields"].append(["종속회사", row[1]])
            if row and row[0] == "정정일자":
                result["fields"].append(["정정일자", row[1] if len(row) > 1 else ""])
    for t in tables:
        for row in t:
            if row and re.match(r"^3\.\s*정정사유", row[0]) and len(row) > 1:
                result["fields"].append(["정정사유", shorten(row[1])])

    if item["category"] == "잠정실적":
        earnings = read_earnings(tables)
        if earnings:
            result["earnings"] = earnings
            result["headline"] = earnings_headline(earnings)
        table = next((t for t in tables if any(r and re.match(r"^4\.\s*기타 투자판단", r[0]) for r in t)), None)
        sections = read_sections(table) if table else []
    else:
        table = main_table(tables)
        sections = read_sections(table) if table else []
        result["headline"] = headline_for(item["category"], item["report_nm"], sections)

    for label, rows in sections:
        text = section_text(rows)
        if not text:
            continue
        if NOTE_LABEL.search(label):
            result["note"] = shorten(text, 600)
        elif item["category"] != "잠정실적" and not SKIP_LABEL.search(label):
            result["fields"].append([label, shorten(text)])

    if not result["headline"]:
        result["headline"] = join(*[v for k, v in result["fields"][:2] if len(v) <= 60]) or None
    return result


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    summary_days = config.get("summary_days", 90)
    api_key = load_api_key()

    items = json.loads(DATA_PATH.read_text(encoding="utf-8"))["items"]
    store = json.loads(SUMMARY_PATH.read_text(encoding="utf-8")) if SUMMARY_PATH.exists() else {"items": {}}
    summaries = store["items"]

    since = (datetime.now(KST).date() - timedelta(days=summary_days)).isoformat()
    targets = [i for i in items if i["rcept_dt"] >= since]
    todo = [i for i in targets if summaries.get(i["rcept_no"], {}).get("v") != PARSER_VERSION][:MAX_PER_RUN]

    done = failed = 0
    for item in todo:
        try:
            summaries[item["rcept_no"]] = summarize(item, fetch_document(api_key, item["rcept_no"]))
            done += 1
        except Exception as e:  # 한 건이 실패해도 나머지는 계속, 다음 실행 때 다시 시도
            failed += 1
            print(f"  [건너뜀] {item['corp_name']} {item['report_nm']} ({item['rcept_no']}): {e}")

    keep = {i["rcept_no"] for i in targets}
    store = {"version": PARSER_VERSION, "items": {k: v for k, v in summaries.items() if k in keep}}
    SUMMARY_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"요약: 대상 {len(targets)}건 (최근 {summary_days}일) 중 새로 정리 {done}건, 실패 {failed}건, 저장 {len(store['items'])}건")
    if failed and not done:
        sys.exit(1)


if __name__ == "__main__":
    main()
