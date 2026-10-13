#!/usr/bin/env python3
"""공주·대전·청주 편의점 포켓몬카드(모든 카드팩) 재고 감시 → 휴대폰 푸시 알림(ntfy).

동작
  1) 상품 찾기: 편의점마다 '포켓몬카드' 등으로 검색해 카드팩 상품 목록을 자동으로 만든다
     (몇 시간마다 갱신 → 새 확장팩이 나오면 자동으로 감시 대상에 추가).
  2) 재고 확인: 찾은 상품 하나하나에 대해 지역 매장 재고를 조회한다.
  3) 어떤 카드팩이든 0개 → 1개 이상이 된 매장이 생기면 그 지역 주제로 푸시를 보낸다.

재고 데이터는 오픈소스 프로젝트 daiso-mcp(https://github.com/hmmhmmhm/daiso-mcp)가
운영하는 공개 REST API(https://mcp.aka.page)를 통해 받아옵니다. 표준 라이브러리만 사용합니다.

알림 주제: {NTFY_TOPIC}-{지역 id}  (예: xxx-gongju / xxx-daejeon / xxx-cheongju)

사용법
  python checker.py                 # 한 번 확인 (GitHub Actions에서 사용)
  python checker.py --loop 1800     # 내 PC에서 30분마다 계속 확인
  python checker.py --test-notify   # 지역별 푸시 알림 테스트
  python checker.py --dry-run       # 알림 없이 결과만 출력
  python checker.py --products      # 지금 감시 대상으로 잡히는 카드팩 목록만 출력

환경변수: NTFY_TOPIC (필수), NTFY_SERVER (기본 https://ntfy.sh), NTFY_TOKEN (선택), DASHBOARD_URL (선택)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
STATUS_PATH = ROOT / "docs" / "status.json"
KST = timezone(timedelta(hours=9))
STATE_VERSION = 4

CHAIN_LABEL = {"gs25": "GS25", "cu": "CU", "seveneleven": "세븐일레븐", "emart24": "이마트24"}
CENTER_BASED = {"gs25", "cu", "emart24"}  # 좌표 기준 조회. 세븐일레븐은 지역 키워드로 1회 조회
MAX_EVENTS = 300
MAX_PUSH_PER_RUN = 8  # 지역별로 한 번에 너무 많은 푸시가 오지 않도록


# ───────────────────────── HTTP ─────────────────────────
def http_get_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "pokecard-stock/1.2", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_call(base: str, path: str, params: dict, retries: int = 1) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{base.rstrip('/')}{path}?{qs}"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            body = http_get_json(url)
            if not body.get("success", False):
                err = body.get("error") or {}
                raise RuntimeError(f"{err.get('code', 'API_ERROR')}: {err.get('message', body)}")
            return body.get("data") or {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {})
                last_err = RuntimeError(f"HTTP {e.code} {detail.get('code', '')}: {detail.get('message', '')}")
            except Exception:
                last_err = RuntimeError(f"HTTP {e.code}")
            if e.code in (400, 404, 429, 503):  # 재시도해도 소용없는 오류 (503 = 상대 서비스 인증 장애 등)
                break
        except Exception as e:  # 네트워크·타임아웃·파싱
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(str(last_err))


def _num(v):
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


# ───────────────────────── 1) 카드팩 상품 찾기 ─────────────────────────
def search_products(base, chain, keyword):
    """체인별 상품 검색 → [{id, name}]"""
    if chain == "gs25":
        d = api_call(base, "/api/gs25/products", {"keyword": keyword, "limit": 50})
        return [{"id": p.get("itemCode"), "name": p.get("itemName") or p.get("shortItemName")}
                for p in d.get("products", []) if p.get("itemCode") and p.get("stockCheckEnabled", True) is not False]
    if chain == "cu":  # 검색 결과가 50개를 넘으면 다음 페이지까지(최대 150개)
        items = []
        for offset in (0, 50, 100):
            d = api_call(base, "/api/cu/inventory", {"keyword": keyword, "size": 50, "offset": offset, "storeCheck": "false"})
            inv = d.get("inventory") or {}
            items += inv.get("items", [])
            if offset + 50 >= int(inv.get("totalCount") or 0):
                break
        return [{"id": p.get("itemCode"), "name": p.get("itemName")} for p in items if p.get("itemCode")]
    if chain == "seveneleven":
        d = api_call(base, "/api/seveneleven/products", {"query": keyword, "size": 50})
        return [{"id": p.get("itemCode"), "name": p.get("itemName")}
                for p in d.get("products", []) if p.get("itemCode")]
    if chain == "emart24":
        d = api_call(base, "/api/emart24/products", {"keyword": keyword, "pageSize": 50})
        return [{"id": p.get("pluCd"), "name": p.get("goodsName")}
                for p in d.get("products", []) if p.get("pluCd")]
    return []


def is_card_pack(name: str, pf: dict, code: str = "") -> bool:
    """카드팩 판별: 제외 단어가 없고, (포켓몬코리아 카드팩 바코드 접두어) 또는 (포켓몬 + 카드 관련 단어)."""
    n = (name or "").replace(" ", "")
    if any(t in n for t in pf["exclude"]):
        return False
    if code and any(str(code).startswith(p) for p in pf.get("barcodePrefixes", [])):
        return True  # 열풍의아레나·로켓단의영광처럼 이름에 '카드'가 없는 팩도 포함
    return any(t in n for t in pf["mustContainAny"]) and any(t in n for t in pf["cardWordsAny"])


def discover(cfg, cache: dict, now, force=False):
    """체인별 감시 상품 목록. 캐시가 유효하면 재사용, 검색 실패 시 이전 목록 유지."""
    base, pf = cfg["apiBase"], cfg["productFilter"]
    hours = cfg.get("discoveryHours", 6)
    out = {}
    for chain in cfg["chains"]:
        c = cache.get(chain) or {}
        fresh = c.get("at") and now - datetime.fromisoformat(c["at"]) < timedelta(hours=hours)
        if fresh and c.get("products") and not force:
            # 캐시를 쓰더라도 지금 설정의 필터(exclude 등)는 바로 적용
            out[chain] = dict(c, products=[p for p in c["products"] if is_card_pack(p["name"], pf, p["id"])])
            continue
        found, errors = {}, []
        for kw in pf["searchKeywords"]:
            try:
                for p in search_products(base, chain, kw):
                    if p["name"] and is_card_pack(p["name"], pf, p["id"]):
                        found.setdefault(p["id"], p)
            except Exception as e:
                errors.append(str(e))
        products = list(found.values())[: cfg.get("maxProductsPerChain", 8)]
        if products:
            out[chain] = {"at": now.isoformat(timespec="seconds"), "products": products, "error": None}
        else:  # 검색 실패 → 이전 목록 유지(다음 실행에 다시 시도)
            out[chain] = {"at": c.get("at"), "products": c.get("products", []),
                          "error": errors[0] if errors else "카드팩 상품을 찾지 못했습니다"}
        names = ", ".join(p["name"] for p in out[chain]["products"]) or "-"
        print(f"[상품찾기][{CHAIN_LABEL[chain]}] {len(out[chain]['products'])}개: {names}"
              + (f" | 오류: {out[chain]['error']}" if out[chain]["error"] else ""))
    return out


# ───────────────────────── 2) 재고 조회 ─────────────────────────
def stock_gs25(base, product, center, region, cfg):
    d = api_call(base, "/api/gs25/inventory", {
        "itemCode": product["id"], "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"]})
    return [{"code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
             "phone": s.get("phone", ""), "qty": _num(s.get("realStockQuantity"))}
            for s in (d.get("inventory") or {}).get("stores", [])]


def stock_cu(base, product, center, region, cfg):
    d = api_call(base, "/api/cu/inventory", {
        "keyword": product["name"], "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"], "size": 20})
    ns = d.get("nearbyStores") or {}
    if ns.get("stockItemCode") != product["id"]:
        raise RuntimeError(f"CU 상품 매칭 불일치 ({ns.get('stockItemName')})")
    return [{"code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
             "phone": s.get("phone", ""), "qty": _num(s.get("stock"))} for s in ns.get("stores", [])]


def stock_seveneleven(base, product, _center, region, cfg):
    d = api_call(base, "/api/seveneleven/inventory", {
        "keyword": product["name"], "storeKeyword": region["storeKeyword"], "storeLimit": max(cfg["storeLimit"], 100)})
    if not d.get("stockAvailable", True):
        raise RuntimeError((d.get("stockError") or {}).get("message") or d.get("note") or "세븐일레븐 재고 확인 실패")
    if ((d.get("product") or {}).get("itemCode") or product["id"]) != product["id"]:
        raise RuntimeError("세븐일레븐 상품 매칭 불일치")
    return [{"code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
             "phone": "", "qty": _num(s.get("stockQuantity"))} for s in (d.get("inventory") or {}).get("stores", [])]


def stock_emart24(base, product, center, region, cfg):
    d = api_call(base, "/api/emart24/inventory", {
        "pluCd": product["id"], "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"]})
    return [{"code": s.get("bizNo"), "name": s.get("storeName"), "address": s.get("address", ""),
             "phone": s.get("phone", ""), "qty": _num(s.get("bizQty"))} for s in d.get("stores", [])]


STOCK = {"gs25": stock_gs25, "cu": stock_cu, "seveneleven": stock_seveneleven, "emart24": stock_emart24}


def in_region(store, region) -> bool:
    addr = store.get("address") or ""
    return any(tok in addr for tok in region["addressMustContain"])


def collect(cfg, catalog):
    """반환: results[region_id][chain][product_id] = {product, stores{code: store}, error, warning}"""
    base = cfg["apiBase"]
    jobs = []
    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            for product in catalog[chain]["products"]:
                for center in (region["centers"] if chain in CENTER_BASED else [None]):
                    jobs.append((region, chain, product, center))
    print(f"재고 조회 {len(jobs)}회 시작")

    # 체인 하나가 장애일 때 같은 오류를 수십 번 반복 호출하지 않도록: 체인별 첫 호출 결과를 먼저 본다
    down: dict = {}

    def run(job):
        region, chain, product, center = job
        if chain in down:
            return job, None, down[chain]
        try:
            return job, STOCK[chain](base, product, center, region, cfg), None
        except Exception as e:
            msg = str(e)
            if any(code in msg for code in ("UPSTREAM_UNAVAILABLE", "RELAY_FAILED", "INVENTORY_CHECK_FAILED", "DAILY_RATE_LIMIT")):
                down[chain] = msg  # 서비스 장애로 판단 → 이번 실행에서 이 체인은 건너뜀
            return job, None, msg

    with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 4)) as pool:
        outputs = list(pool.map(run, jobs))

    results: dict = {}
    calls: dict = {}
    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            results.setdefault(region["id"], {})[chain] = {
                p["id"]: {"product": p, "stores": {}, "error": None, "errors": []} for p in catalog[chain]["products"]}
    for (region, chain, product, center), stores, err in outputs:
        entry = results[region["id"]][chain][product["id"]]
        k = (region["id"], chain, product["id"])
        calls[k] = calls.get(k, 0) + 1
        if err:
            entry["errors"].append(err)
            continue
        for s in stores:
            if s.get("code") and in_region(s, region):
                prev = entry["stores"].get(s["code"])
                if prev is None or (s["qty"] or 0) > (prev["qty"] or 0):
                    entry["stores"][s["code"]] = s
    for rid, by_chain in results.items():
        for chain, by_prod in by_chain.items():
            for pid, entry in by_prod.items():
                errs = entry.pop("errors")
                if errs and len(errs) == calls.get((rid, chain, pid), 0):
                    entry["error"] = errs[0]
                elif errs:
                    entry["warning"] = errs[0]
    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            by_prod = results[region["id"]][chain]
            ok = [e for e in by_prod.values() if not e["error"]]
            hot = sum(1 for e in ok for s in e["stores"].values() if (s["qty"] or 0) >= cfg["minQuantity"])
            err = next((e["error"] for e in by_prod.values() if e["error"]), None)
            print(f"[{region['name']}][{CHAIN_LABEL[chain]}] 상품 {len(by_prod)}개 중 조회성공 {len(ok)}"
                  f" | 재고있는 매장·상품 {hot}" + (f" | 오류: {err[:100]}" if err else ""))
    return results


# ───────────────────────── 상태 비교 ─────────────────────────
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def map_link(store):
    q = urllib.parse.quote(f"{store['name']} {store['address']}".strip())
    return f"https://map.naver.com/p/search/{q}"


def diff(results, state, cfg):
    """재고 0→있음(입고), 있음→0(품절) 전환. 조회 오류가 난 조합은 이전 상태 유지."""
    min_q = cfg["minQuantity"]
    prev_qty: dict = state.get("qty", {})
    new_qty: dict = {}
    restocks, soldouts = [], []
    for rid, by_chain in results.items():
        for chain, by_prod in by_chain.items():
            for pid, entry in by_prod.items():
                prefix = f"{rid}|{chain}|{pid}|"
                if entry["error"]:
                    new_qty.update({k: v for k, v in prev_qty.items() if k.startswith(prefix)})
                    continue
                for code, s in entry["stores"].items():
                    key = prefix + code
                    q = s["qty"] or 0
                    new_qty[key] = q
                    before = prev_qty.get(key, 0)
                    row = {"region": rid, "chain": chain, "product": entry["product"]["name"], "store": s, "before": before}
                    if q >= min_q and before < min_q:
                        restocks.append(row)
                    elif q < min_q and before >= min_q:
                        soldouts.append(row)
                for key, before in prev_qty.items():
                    if key.startswith(prefix) and key not in new_qty:
                        new_qty[key] = before if entry.get("warning") else 0
    return new_qty, restocks, soldouts


# ───────────────────────── 푸시 알림 (ntfy) ─────────────────────────
def region_topic(region):
    base = os.environ.get("NTFY_TOPIC", "").strip()
    return f"{base}-{region['id']}" if base else ""


def ntfy_send(topic, notes, dry_run=False):
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if dry_run or not topic:
        for n in notes:
            print(f"── 푸시(미전송) [{topic or '주제 없음'}] ── {n['title']}\n{n['message']}\n→ {n.get('click', '')}")
        if not dry_run and not topic:
            print("※ NTFY_TOPIC 이 설정되지 않아 전송하지 않았습니다.")
        return
    for n in notes:
        payload = {"topic": topic, "title": n["title"], "message": n["message"],
                   "priority": n.get("priority", 4), "tags": n.get("tags", [])}
        if n.get("click"):
            payload["click"] = n["click"]
        actions = []
        if n.get("map"):
            actions.append({"action": "view", "label": "지도", "url": n["map"], "clear": False})
        if n.get("phone"):
            actions.append({"action": "view", "label": "전화",
                            "url": "tel:" + "".join(ch for ch in n["phone"] if ch.isdigit() or ch == "-")})
        if actions:
            payload["actions"] = actions
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            print(f"푸시 전송 완료 → {topic}: {n['title']}")
        except Exception as e:
            print(f"푸시 전송 실패({topic}): {e}", file=sys.stderr)
        time.sleep(0.5)


def build_alert(region, rows):
    """매장 단위로 묶어서(한 매장에 여러 카드팩이 들어오면 한 알림) 보낸다."""
    by_store: dict = {}
    for r in rows:
        k = (r["chain"], r["store"]["code"])
        by_store.setdefault(k, {"chain": r["chain"], "store": r["store"], "items": []})["items"].append(
            (r["product"], r["store"]["qty"]))
    groups = sorted(by_store.values(), key=lambda g: -sum(q or 0 for _, q in g["items"]))
    notes = []
    for g in groups[:MAX_PUSH_PER_RUN]:
        s = g["store"]
        total = sum(q or 0 for _, q in g["items"])
        lines = [f"📦 {name} {q}개" for name, q in g["items"]] + [f"📍 {s['address']}"]
        if s.get("phone"):
            lines.append(f"☎️ {s['phone']}")
        title_items = f"카드팩 {len(g['items'])}종" if len(g["items"]) > 1 else f"{total}개"
        notes.append({"title": f"[{region['name']}·{CHAIN_LABEL[g['chain']]}] {s['name']} — {title_items} 입고",
                      "message": "\n".join(lines), "click": map_link(s), "map": map_link(s),
                      "phone": s.get("phone") or "", "priority": 5, "tags": ["black_joker"]})
    rest = groups[MAX_PUSH_PER_RUN:]
    if rest:
        notes.append({
            "title": f"[{region['name']}] 외 {len(rest)}곳 더 입고",
            "message": "\n".join(f"[{CHAIN_LABEL[g['chain']]}] {g['store']['name']}" for g in rest[:15]),
            "priority": 4, "tags": ["black_joker"], "click": os.environ.get("DASHBOARD_URL", "")})
    return notes


def build_start(region, results, catalog, cfg):
    hot = {}
    for chain, by_prod in results[region["id"]].items():
        for e in by_prod.values():
            for s in e["stores"].values():
                if (s["qty"] or 0) >= cfg["minQuantity"]:
                    hot.setdefault((chain, s["code"]), [s["name"], 0])[1] += s["qty"]
    n_products = sum(len(catalog[c]["products"]) for c in cfg["chains"])
    lines = [f"감시 중인 카드팩: {n_products}종 (편의점별 자동 검색)", f"지금 재고 있는 매장: {len(hot)}곳"]
    lines += [f"· [{CHAIN_LABEL[c]}] {name} {q}개" for (c, _), (name, q) in list(hot.items())[:15]]
    if not hot:
        lines.append("어떤 카드팩이든 입고되면 바로 알려드릴게요.")
    return [{"title": f"✅ {region['name']} 포켓몬카드 재고 감시 시작", "message": "\n".join(lines),
             "priority": 3, "tags": ["white_check_mark"], "click": os.environ.get("DASHBOARD_URL", "")}]


# ───────────────────────── 이번 실행에서 확인할 상품 고르기 ─────────────────────────
def chain_cost(cfg, chain):
    """상품 1종을 모든 지역에서 확인하는 데 드는 API 호출 수."""
    return sum(len(r["centers"]) if chain in CENTER_BASED else 1 for r in cfg["regions"])


def plan_checks(cfg, catalog, state, now):
    """우선 상품(이름 지정·최근 재고·새로 나온 팩)은 매번, 나머지는 오래 안 본 것부터 호출 예산 안에서."""
    budget = cfg.get("maxCallsPerChainPerRun", 100)
    keywords = cfg.get("priorityKeywords", [])
    hot_until = timedelta(days=cfg.get("hotDays", 3))
    last_checked = state.get("lastChecked", {})
    last_stock = state.get("lastInStock", {})
    first_seen = state.get("firstSeen", {})
    plan, info = {}, {}
    for chain in cfg["chains"]:
        products = catalog[chain]["products"]
        cost = max(1, chain_cost(cfg, chain))
        def key(p): return f"{chain}|{p['id']}"
        def is_priority(p):
            if any(k.replace(" ", "") in p["name"].replace(" ", "") for k in keywords):
                return True
            ls = last_stock.get(key(p))
            if ls and now - datetime.fromisoformat(ls) < hot_until:
                return True
            fs = first_seen.get(key(p))
            return bool(fs and fs != "initial" and now - datetime.fromisoformat(fs) < hot_until)
        prio = [p for p in products if is_priority(p)]
        rest = sorted([p for p in products if not is_priority(p)], key=lambda p: last_checked.get(key(p), ""))
        n_slots = max(len(prio), budget // cost)
        chosen = prio + rest[: max(0, n_slots - len(prio))]
        plan[chain] = {"products": chosen}
        cycle = (len(rest) / max(1, n_slots - len(prio))) if rest and n_slots > len(prio) else None
        info[chain] = {"priority": len(prio), "rotating": len(rest), "checkedNow": len(chosen),
                       "cycleRuns": round(cycle, 1) if cycle else None}
        print(f"[계획][{CHAIN_LABEL[chain]}] 우선 {len(prio)}종 매번 + 나머지 {len(rest)}종 중 {len(chosen) - len(prio)}종 순환")
    return plan, info


# ───────────────────────── 대시보드 status.json ─────────────────────────
def build_status(snapshot, catalog, plan_info, cfg, now, events, state):
    groups = []
    last_checked = state.get("lastChecked", {})
    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            for p in catalog[chain]["products"]:
                snap = snapshot.get(f"{region['id']}|{chain}|{p['id']}")
                groups.append({
                    "region": region["id"], "chain": chain, "chainLabel": CHAIN_LABEL[chain],
                    "productId": p["id"], "product": p["name"],
                    "error": snap.get("error") if snap else None,
                    "pending": snap is None,
                    "checkedAt": last_checked.get(f"{chain}|{p['id']}"),
                    "checkedStores": snap.get("checkedStores", 0) if snap else 0,
                    "stores": snap.get("stores", []) if snap else [],
                })
    return {
        "regions": [{"id": r["id"], "name": r["name"], "notify": r.get("notify", True)} for r in cfg["regions"]],
        "chains": [dict({"id": c, "label": CHAIN_LABEL[c], "products": len(catalog[c]["products"]),
                         "error": catalog[c].get("error")}, **plan_info.get(c, {})) for c in cfg["chains"]],
        "minQuantity": cfg["minQuantity"], "checkedAt": now.isoformat(timespec="seconds"),
        "intervalMinutes": cfg.get("intervalMinutes", 15),
        "groups": groups, "events": events[:MAX_EVENTS],
    }


def signature(status) -> str:
    s = dict(status)
    s.pop("checkedAt", None)
    return hashlib.sha256(json.dumps(s, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# ───────────────────────── main ─────────────────────────
def run_once(cfg, dry_run=False):
    now = datetime.now(KST)
    stamp = now.isoformat(timespec="seconds")
    state = load_json(STATE_PATH, {})
    if state.get("version") != STATE_VERSION:  # 이전 형식이면 새로 시작
        state = {}
    started = set(state.get("startedRegions", []))
    region_ids = {r["id"] for r in cfg["regions"]}

    catalog = discover(cfg, state.get("catalog", {}), now)

    # 새로 등장한 상품 기록 (처음 만든 목록은 'initial' → 우선순위 아님)
    first_seen = dict(state.get("firstSeen", {}))
    initial = not first_seen
    for chain in cfg["chains"]:
        for p in catalog[chain]["products"]:
            first_seen.setdefault(f"{chain}|{p['id']}", "initial" if initial else stamp)
    state["firstSeen"] = first_seen

    plan, plan_info = plan_checks(cfg, catalog, state, now)
    results = collect(cfg, plan)
    new_qty, restocks, soldouts = diff(results, state, cfg)

    # 이번에 확인하지 않은 상품의 이전 재고는 그대로 유지 (빠진 지역은 버림)
    checked_prefixes = {f"{rid}|{chain}|{pid}|" for rid, bc in results.items() for chain, bp in bc.items() for pid in bp}
    for k, v in state.get("qty", {}).items():
        if k.split("|")[0] in region_ids and not any(k.startswith(px) for px in checked_prefixes):
            new_qty.setdefault(k, v)

    # 상품별 마지막 결과(스냅샷)·확인 시각·재고 발견 시각 갱신
    snapshot = {k: v for k, v in state.get("snapshot", {}).items() if k.split("|")[0] in region_ids}
    last_checked = dict(state.get("lastChecked", {}))
    last_in_stock = dict(state.get("lastInStock", {}))
    for rid, by_chain in results.items():
        for chain, by_prod in by_chain.items():
            for pid, e in by_prod.items():
                key = f"{rid}|{chain}|{pid}"
                if e["error"]:
                    prev = snapshot.get(key, {})
                    snapshot[key] = dict(prev, error=e["error"])
                    continue
                stores = sorted(e["stores"].values(), key=lambda s: (-(s["qty"] or 0), s["name"] or ""))
                hot = [dict(s, mapUrl=map_link(s)) for s in stores if (s["qty"] or 0) >= cfg["minQuantity"]]
                snapshot[key] = {"checkedStores": len(stores), "stores": hot, "error": None}
                last_checked[f"{chain}|{pid}"] = stamp
                if hot:
                    last_in_stock[f"{chain}|{pid}"] = stamp
    state["lastChecked"], state["lastInStock"] = last_checked, last_in_stock

    events = [e for e in state.get("events", []) if e.get("region") in region_ids]
    for kind, rows in (("restock", restocks), ("soldout", soldouts)):
        for r in rows:
            if r["region"] not in started:
                continue
            s = r["store"]
            events.insert(0, {"at": stamp, "type": kind, "region": r["region"],
                              "chain": r["chain"], "chainLabel": CHAIN_LABEL[r["chain"]], "product": r["product"],
                              "store": s["name"], "address": s["address"], "qty": s["qty"] or 0, "before": r["before"]})
    events = events[:MAX_EVENTS]

    for region in cfg["regions"]:
        rid = region["id"]
        if not region.get("notify", True):
            continue
        if rid not in started:
            if cfg.get("notifyOnFirstRun", True):
                ntfy_send(region_topic(region), build_start(region, results, catalog, cfg), dry_run)
            continue
        rows = [r for r in restocks if r["region"] == rid]
        if rows:
            ntfy_send(region_topic(region), build_alert(region, rows), dry_run)
        else:
            print(f"[{region['name']}] 새로 입고된 매장 없음")
    started = (started & region_ids) | region_ids

    status = build_status(snapshot, catalog, plan_info, cfg, now, events, state)
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    STATE_PATH.write_text(json.dumps({
        "version": STATE_VERSION, "qty": new_qty, "events": events, "catalog": catalog,
        "snapshot": snapshot, "lastChecked": last_checked, "lastInStock": last_in_stock, "firstSeen": first_seen,
        "startedRegions": sorted(started), "lastWrite": stamp,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("상태 파일 갱신")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SEC", help="SEC초마다 반복 실행 (최소 600)")
    ap.add_argument("--dry-run", action="store_true", help="푸시 전송 없이 출력만")
    ap.add_argument("--test-notify", action="store_true", help="지역별 푸시 테스트 알림 전송")
    ap.add_argument("--products", action="store_true", help="감시 대상 카드팩 목록만 출력")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        print("config.json을 읽을 수 없습니다.", file=sys.stderr)
        return 1
    cfg.setdefault("apiBase", "https://mcp.aka.page")

    if args.products:
        discover(cfg, {}, datetime.now(KST), force=True)
        return 0
    if args.test_notify:
        for region in cfg["regions"]:
            ntfy_send(region_topic(region), [{
                "title": f"🔔 {region['name']} 알림 연결 성공!",
                "message": f"{region['name']} 포켓몬카드 입고 알림이 이 휴대폰으로 와요."
                           + ("" if region.get("notify", True) else "\n(현재 config.json에서 이 지역 알림이 꺼져 있어요)"),
                "priority": 4, "tags": ["tada"], "click": os.environ.get("DASHBOARD_URL", ""),
            }])
        return 0
    if not args.loop:
        return run_once(cfg, args.dry_run)
    interval = max(600, args.loop)
    while True:
        try:
            run_once(load_json(CONFIG_PATH, cfg) | {"apiBase": cfg["apiBase"]}, args.dry_run)
        except Exception as e:
            print(f"실행 오류: {e}", file=sys.stderr)
        print(f"{interval}초 후 다시 확인합니다...\n")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
