"""관심 기업의 주요 공시를 DART 오픈API로 모아 docs/data/disclosures.json 에 저장한다.

- 관심 기업과 공시 종류(보고서명 키워드)는 config.json 에서 관리한다.
- 인증키는 환경변수 DART_API_KEY 에서 읽는다. (GitHub Actions 에서는 Secrets 로 넣고,
  내 PC에서는 상위 폴더 .env 를 실행 시점에 읽는다.)
- 처음 보는 기업은 keep_days(1년)치를, 이미 받은 기업은 daily_lookback_days 만큼만 다시 확인한다.
- --full 을 붙이면 모든 기업을 keep_days 치로 다시 받는다. (키워드를 추가했을 때 사용)
"""

import io
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "docs" / "data" / "disclosures.json"
LOCAL_ENV_PATH = BASE_DIR.parent / ".env"

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

STATUS_OK = "000"
STATUS_NO_DATA = "013"  # 조회된 데이터가 없음 → 오류가 아니라 0건
PAGE_COUNT = 100

KST = timezone(timedelta(hours=9))


def load_api_key():
    if not os.getenv("DART_API_KEY") and LOCAL_ENV_PATH.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(LOCAL_ENV_PATH)
        except ImportError:
            pass
    key = (os.getenv("DART_API_KEY") or "").strip()
    if not key:
        sys.exit("[오류] DART_API_KEY 가 없습니다. GitHub 에서는 Secrets 에, 내 PC 에서는 .env 에 넣어 주세요.")
    return key


def fetch_corp_list(api_key):
    resp = requests.get(CORP_CODE_URL, params={"crtfc_key": api_key}, timeout=60)
    resp.raise_for_status()
    if not zipfile.is_zipfile(io.BytesIO(resp.content)):
        sys.exit(f"[오류] 고유번호 목록을 받지 못했습니다: {resp.text[:300]}")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        root = ET.fromstring(zf.read(zf.namelist()[0]))
    return [
        {
            "corp_code": (item.findtext("corp_code") or "").strip(),
            "corp_name": (item.findtext("corp_name") or "").strip(),
            "stock_code": (item.findtext("stock_code") or "").strip(),
        }
        for item in root.iter("list")
    ]


def resolve_company(corps, company):
    """종목코드가 있으면 종목코드로, 없으면 이름이 정확히 같은 회사로 찾는다."""
    stock_code = company.get("stock_code", "").strip()
    name = company.get("name", "").strip()
    if stock_code:
        found = [c for c in corps if c["stock_code"] == stock_code]
    else:
        found = [c for c in corps if c["corp_name"] == name]
    if len(found) == 1:
        return found[0]

    print(f"[중단] config.json 의 '{name}' ({stock_code or '종목코드 없음'}) 를 하나로 찾지 못했습니다.")
    candidates = found or [c for c in corps if name and name in c["corp_name"]]
    for c in candidates[:20]:
        print(f"   - {c['corp_name']}  고유번호 {c['corp_code']}  종목코드 {c['stock_code'] or '-'}")
    sys.exit(1)


def fetch_disclosures(api_key, corp_code, begin, end):
    rows = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": begin.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "page_no": page_no,
            "page_count": PAGE_COUNT,
        }
        resp = requests.get(LIST_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status == STATUS_NO_DATA:
            break
        if status != STATUS_OK:
            sys.exit(f"[오류] 공시 목록 조회 실패 (status {status}): {data.get('message')}")

        rows.extend(data.get("list", []))
        if page_no >= int(data.get("total_page", 1)):
            break
        page_no += 1
    return rows


def normalize(text):
    return "".join((text or "").split())


def classify(report_nm, categories):
    """보고서명에 키워드가 들어 있으면 그 공시 종류를, 없으면 None 을 돌려준다."""
    name = normalize(report_nm)
    for category, keywords in categories.items():
        if any(normalize(k) in name for k in keywords):
            return category
    return None


def load_data():
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {"items": [], "fetched_corp_codes": []}


def main():
    full = "--full" in sys.argv[1:]
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    categories = config["categories"]
    api_key = load_api_key()

    now = datetime.now(KST)
    today = now.date()
    keep_from = today - timedelta(days=config["keep_days"])

    data = load_data()
    items = {i["rcept_no"]: i for i in data["items"]}
    fetched = set() if full else set(data.get("fetched_corp_codes", []))

    corps = fetch_corp_list(api_key)
    targets = [resolve_company(corps, c) for c in config["companies"]]
    target_codes = {t["corp_code"] for t in targets}

    added = 0
    for corp in targets:
        is_new_corp = corp["corp_code"] not in fetched
        begin = keep_from if is_new_corp else today - timedelta(days=config["daily_lookback_days"])
        rows = fetch_disclosures(api_key, corp["corp_code"], begin, today)

        matched = 0
        for r in rows:
            category = classify(r["report_nm"], categories)
            if not category:
                continue
            matched += 1
            if r["rcept_no"] not in items:
                added += 1
            items[r["rcept_no"]] = {
                "rcept_no": r["rcept_no"],
                "rcept_dt": f"{r['rcept_dt'][:4]}-{r['rcept_dt'][4:6]}-{r['rcept_dt'][6:]}",
                "corp_code": corp["corp_code"],
                "corp_name": r["corp_name"],
                "stock_code": corp["stock_code"],
                "report_nm": " ".join(r["report_nm"].split()),
                "category": category,
                "link": VIEWER_URL.format(rcept_no=r["rcept_no"]),
            }
        fetched.add(corp["corp_code"])
        print(f"{corp['corp_name']} ({corp['stock_code']})  고유번호 {corp['corp_code']}  "
              f"{begin:%Y-%m-%d}~  전체 {len(rows)}건 중 주요 공시 {matched}건")

    # 관심 기업에서 빠졌거나, 1년이 지났거나, 키워드에서 빠진 공시는 정리한다.
    kept = []
    for i in items.values():
        if i["corp_code"] not in target_codes or i["rcept_dt"] < keep_from.isoformat():
            continue
        category = classify(i["report_nm"], categories)
        if not category:
            continue
        i["category"] = category
        kept.append(i)
    kept.sort(key=lambda i: (i["rcept_dt"], i["rcept_no"]), reverse=True)

    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "keep_from": keep_from.isoformat(),
        "companies": [t["corp_name"] for t in targets],
        "categories": list(categories),
        "fetched_corp_codes": sorted(fetched & target_codes),
        "items": kept,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"새 공시 {added}건 추가, 전체 {len(kept)}건 저장 → {DATA_PATH.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
