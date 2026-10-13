#!/usr/bin/env python3
"""공주·대전·청주 편의점 포켓몬카드 재고 감시 → 휴대폰 푸시 알림(ntfy).

재고 데이터는 오픈소스 프로젝트 daiso-mcp(https://github.com/hmmhmmhm/daiso-mcp)가
운영하는 공개 REST API(https://mcp.aka.page)를 통해 GS25·CU·세븐일레븐·이마트24
앱의 재고조회 결과를 받아옵니다. 표준 라이브러리만 사용합니다.

알림은 지역마다 다른 ntfy 주제로 보냅니다:  {NTFY_TOPIC}-{지역 id}
  예) NTFY_TOPIC=gongju-poke-abc → gongju-poke-abc-gongju / -daejeon / -cheongju
휴대폰 ntfy 앱에서 받고 싶은 지역의 주제만 구독하면 됩니다.

사용법
  python checker.py                 # 한 번 확인 (GitHub Actions에서 사용)
  python checker.py --loop 1200     # 내 PC에서 20분마다 계속 확인
  python checker.py --test-notify   # 지역별 푸시 알림 테스트
  python checker.py --dry-run       # 알림 없이 결과만 출력

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

CHAIN_LABEL = {"gs25": "GS25", "cu": "CU", "seveneleven": "세븐일레븐", "emart24": "이마트24"}
CENTER_BASED = {"gs25", "cu", "emart24"}  # 좌표 기준 조회. 세븐일레븐은 지역 키워드로 1회 조회
MAX_EVENTS = 300
MAX_PUSH_PER_RUN = 8  # 지역별로 한 번에 너무 많은 푸시가 오지 않도록


# ───────────────────────── HTTP ─────────────────────────
def http_get_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "pokecard-stock/1.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_call(base: str, path: str, params: dict, retries: int = 2) -> dict:
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
            if e.code in (400, 404, 429):  # 재시도해도 소용없는 오류
                break
        except Exception as e:  # 네트워크·타임아웃·파싱
            last_err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(str(last_err))


# ───────────────────────── 체인별 조회 ─────────────────────────
def _num(v):
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


def fetch_gs25(base, keyword, center, region, cfg):
    d = api_call(base, "/api/gs25/inventory", {
        "keyword": keyword, "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"],
    })
    product = (d.get("product") or {}).get("name") or keyword
    stores = [{
        "code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
        "phone": s.get("phone", ""), "qty": _num(s.get("realStockQuantity")),
    } for s in (d.get("inventory") or {}).get("stores", [])]
    return product, stores


def fetch_cu(base, keyword, center, region, cfg):
    d = api_call(base, "/api/cu/inventory", {
        "keyword": keyword, "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"], "size": 5,
    })
    ns = d.get("nearbyStores") or {}
    if not ns.get("stockItemCode"):
        raise RuntimeError("CU에서 해당 키워드의 재고조회 가능 상품을 찾지 못했습니다")
    product = ns.get("stockItemName") or keyword
    stores = [{
        "code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
        "phone": s.get("phone", ""), "qty": _num(s.get("stock")),
    } for s in ns.get("stores", [])]
    return product, stores


def fetch_seveneleven(base, keyword, _center, region, cfg):
    d = api_call(base, "/api/seveneleven/inventory", {
        "keyword": keyword, "storeKeyword": region["storeKeyword"], "storeLimit": max(cfg["storeLimit"], 100),
    })
    if not d.get("stockAvailable", True):
        raise RuntimeError((d.get("stockError") or {}).get("message") or d.get("note") or "세븐일레븐 재고 확인 실패")
    product = (d.get("product") or {}).get("itemName") or keyword
    stores = [{
        "code": s.get("storeCode"), "name": s.get("storeName"), "address": s.get("address", ""),
        "phone": "", "qty": _num(s.get("stockQuantity")),
    } for s in (d.get("inventory") or {}).get("stores", [])]
    return product, stores


def fetch_emart24(base, keyword, center, region, cfg):
    d = api_call(base, "/api/emart24/inventory", {
        "keyword": keyword, "lat": center["lat"], "lng": center["lng"], "storeLimit": cfg["storeLimit"],
    })
    goods = d.get("goodsInfo") or {}
    product = goods.get("goodsNm") or keyword
    stores = [{
        "code": s.get("bizNo"), "name": s.get("storeName"), "address": s.get("address", ""),
        "phone": s.get("phone", ""), "qty": _num(s.get("bizQty")),
    } for s in d.get("stores", [])]
    return product, stores


FETCHERS = {"gs25": fetch_gs25, "cu": fetch_cu, "seveneleven": fetch_seveneleven, "emart24": fetch_emart24}


def in_region(store, region) -> bool:
    addr = store.get("address") or ""
    return any(tok in addr for tok in region["addressMustContain"])


def collect(cfg):
    """반환: results[region_id][chain][keyword] = {product, stores{code: store}, error, warning}"""
    base = cfg.get("apiBase", "https://mcp.aka.page")

    # 1) 호출 목록 만들기
    jobs = []
    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            for kw in cfg["keywords"]:
                for center in (region["centers"] if chain in CENTER_BASED else [None]):
                    jobs.append((region, chain, kw, center))

    # 2) 병렬 조회 (공개 API에 부담 주지 않도록 동시 4개)
    def run(job):
        region, chain, kw, center = job
        try:
            return job, FETCHERS[chain](base, kw, center, region, cfg), None
        except Exception as e:
            return job, None, f"{center['name'] if center else region['storeKeyword']}: {e}"

    with ThreadPoolExecutor(max_workers=cfg.get("concurrency", 4)) as pool:
        outputs = list(pool.map(run, jobs))

    # 3) 지역·체인·키워드별로 합치기
    results: dict = {}
    calls: dict = {}
    for (region, chain, kw, center), ok, err in outputs:
        entry = results.setdefault(region["id"], {}).setdefault(chain, {}).setdefault(
            kw, {"product": None, "stores": {}, "error": None, "errors": []})
        calls[(region["id"], chain, kw)] = calls.get((region["id"], chain, kw), 0) + 1
        if err:
            entry["errors"].append(err)
            continue
        product, stores = ok
        entry["product"] = entry["product"] or product
        for s in stores:
            if s.get("code") and in_region(s, region):
                prev = entry["stores"].get(s["code"])
                if prev is None or (s["qty"] or 0) > (prev["qty"] or 0):
                    entry["stores"][s["code"]] = s

    for region in cfg["regions"]:
        for chain in cfg["chains"]:
            for kw in cfg["keywords"]:
                entry = results[region["id"]][chain][kw]
                errs = entry.pop("errors")
                if errs and len(errs) == calls[(region["id"], chain, kw)]:
                    entry["error"] = errs[0]  # 모든 지점 조회 실패
                elif errs:
                    entry["warning"] = "; ".join(errs)  # 일부만 실패 → 결과는 사용
                n_ok = sum(1 for s in entry["stores"].values() if (s["qty"] or 0) >= cfg["minQuantity"])
                print(f"[{region['name']}][{CHAIN_LABEL[chain]}] {kw} → {entry['product']} | 매장 {len(entry['stores'])}곳"
                      f" | 재고있음 {n_ok}" + (f" | 오류: {entry['error']}" if entry["error"] else ""))
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
    """재고 0→있음(입고), 있음→0(품절) 전환을 찾는다. 조회 오류가 난 조합은 이전 상태 유지."""
    min_q = cfg["minQuantity"]
    prev_qty: dict = state.get("qty", {})
    new_qty: dict = {}
    restocks, soldouts = [], []
    for rid, by_chain in results.items():
        for chain, by_kw in by_chain.items():
            for kw, entry in by_kw.items():
                prefix = f"{rid}|{chain}|{kw}|"
                if entry["error"]:
                    new_qty.update({k: v for k, v in prev_qty.items() if k.startswith(prefix)})
                    continue
                for code, s in entry["stores"].items():
                    key = prefix + code
                    q = s["qty"] or 0
                    new_qty[key] = q
                    before = prev_qty.get(key, 0)
                    row = {"region": rid, "chain": chain, "keyword": kw, "product": entry["product"], "store": s, "before": before}
                    if q >= min_q and before < min_q:
                        restocks.append(row)
                    elif q < min_q and before >= min_q:
                        soldouts.append(row)
                # 목록에서 사라진 매장: 모든 지점 조회가 성공했으면 품절(0), 일부 실패했으면 이전 값 유지
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
        except Exception as e:
            print(f"푸시 전송 실패({topic}): {e}", file=sys.stderr)
        time.sleep(0.5)


def _dedupe(rows):
    """같은 체인·상품·매장 중복 제거 (여러 키워드가 같은 상품을 가리킬 때)."""
    seen, out = set(), []
    for r in rows:
        key = (r["chain"], r["product"], r["store"].get("code"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def store_note(region, r):
    s = r["store"]
    body = [f"📦 {r['product'] or ''}", f"📍 {s['address']}"]
    if s.get("phone"):
        body.append(f"☎️ {s['phone']}")
    return {
        "title": f"[{region['name']}·{CHAIN_LABEL[r['chain']]}] {s['name']} — {s['qty']}개 입고",
        "message": "\n".join(body), "click": map_link(s), "map": map_link(s), "phone": s.get("phone") or "",
        "priority": 5, "tags": ["black_joker"],
    }


def build_alert(region, rows):
    rows = _dedupe(sorted(rows, key=lambda r: -(r["store"]["qty"] or 0)))
    notes = [store_note(region, r) for r in rows[:MAX_PUSH_PER_RUN]]
    rest = rows[MAX_PUSH_PER_RUN:]
    if rest:
        notes.append({
            "title": f"[{region['name']}] 외 {len(rest)}곳 더 입고",
            "message": "\n".join(f"[{CHAIN_LABEL[r['chain']]}] {r['store']['name']} {r['store']['qty']}개" for r in rest[:15]),
            "priority": 4, "tags": ["black_joker"], "click": os.environ.get("DASHBOARD_URL", ""),
        })
    return notes


def build_start(region, results, cfg):
    rows = _dedupe([
        {"chain": chain, "product": entry["product"], "store": s}
        for chain, by_kw in results[region["id"]].items() for entry in by_kw.values()
        for s in sorted(entry["stores"].values(), key=lambda s: -(s["qty"] or 0))
        if (s["qty"] or 0) >= cfg["minQuantity"]
    ])
    lines = [f"감시 상품: {', '.join(cfg['keywords'])}", f"지금 재고 있는 매장: {len(rows)}곳"]
    lines += [f"· [{CHAIN_LABEL[r['chain']]}] {r['store']['name']} {r['store']['qty']}개" for r in rows[:15]]
    if not rows:
        lines.append("입고되면 바로 알려드릴게요.")
    return [{"title": f"✅ {region['name']} 포켓몬카드 재고 감시 시작", "message": "\n".join(lines),
             "priority": 3, "tags": ["white_check_mark"], "click": os.environ.get("DASHBOARD_URL", "")}]


# ───────────────────────── 대시보드 status.json ─────────────────────────
def build_status(results, cfg, now, events):
    groups = []
    for region in cfg["regions"]:
        for kw in cfg["keywords"]:
            for chain in cfg["chains"]:
                entry = results[region["id"]][chain][kw]
                stores = sorted(entry["stores"].values(), key=lambda s: (-(s["qty"] or 0), s["name"] or ""))
                groups.append({
                    "region": region["id"], "keyword": kw, "chain": chain, "chainLabel": CHAIN_LABEL[chain],
                    "product": entry["product"], "error": entry["error"], "warning": entry.get("warning"),
                    "stores": [dict(s, mapUrl=map_link(s)) for s in stores],
                })
    return {
        "regions": [{"id": r["id"], "name": r["name"], "notify": r.get("notify", True)} for r in cfg["regions"]],
        "keywords": cfg["keywords"], "minQuantity": cfg["minQuantity"],
        "checkedAt": now.isoformat(timespec="seconds"), "groups": groups, "events": events[:MAX_EVENTS],
    }


def signature(status) -> str:
    s = dict(status)
    s.pop("checkedAt", None)
    return hashlib.sha256(json.dumps(s, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# ───────────────────────── main ─────────────────────────
def run_once(cfg, dry_run=False):
    now = datetime.now(KST)
    state = load_json(STATE_PATH, {})
    if state.get("version") != 2:  # 이전(공주 전용) 형식이면 새로 시작
        state = {}
    started = set(state.get("startedRegions", []))

    results = collect(cfg)
    new_qty, restocks, soldouts = diff(results, state, cfg)

    events = state.get("events", [])
    for kind, rows in (("restock", restocks), ("soldout", soldouts)):
        for r in rows:
            if r["region"] not in started:  # 처음 감시하는 지역은 기록·알림 없이 시작 메시지만
                continue
            s = r["store"]
            events.insert(0, {"at": now.isoformat(timespec="seconds"), "type": kind, "region": r["region"],
                              "chain": r["chain"], "chainLabel": CHAIN_LABEL[r["chain"]], "keyword": r["keyword"],
                              "product": r["product"], "store": s["name"], "address": s["address"],
                              "qty": s["qty"] or 0, "before": r["before"]})
    events = events[:MAX_EVENTS]

    for region in cfg["regions"]:
        rid = region["id"]
        if not region.get("notify", True):
            continue
        if rid not in started:
            if cfg.get("notifyOnFirstRun", True):
                ntfy_send(region_topic(region), build_start(region, results, cfg), dry_run)
            continue
        rows = [r for r in restocks if r["region"] == rid]
        if rows:
            ntfy_send(region_topic(region), build_alert(region, rows), dry_run)
        else:
            print(f"[{region['name']}] 새로 입고된 매장 없음")
    started |= {r["id"] for r in cfg["regions"]}

    all_failed = all(e["error"] for by_chain in results.values() for by_kw in by_chain.values() for e in by_kw.values())
    if all_failed:
        print("⚠️ 모든 조회가 실패했습니다. API 상태: https://aka-page.betteruptime.com/", file=sys.stderr)

    status = build_status(results, cfg, now, events)
    new_sig = signature(status)
    last_write = state.get("lastWrite")
    stale = not last_write or now - datetime.fromisoformat(last_write) > timedelta(hours=cfg.get("heartbeatHours", 6))

    # 변경이 있거나 heartbeat 시간이 지났을 때만 파일 갱신 → 불필요한 커밋 방지
    if new_sig != state.get("signature") or stale or new_qty != state.get("qty") or started != set(state.get("startedRegions", [])):
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
        STATE_PATH.write_text(json.dumps({
            "version": 2, "qty": new_qty, "events": events, "signature": new_sig,
            "startedRegions": sorted(started), "lastWrite": now.isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("상태 파일 갱신")
    return 2 if all_failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SEC", help="SEC초마다 반복 실행 (최소 300)")
    ap.add_argument("--dry-run", action="store_true", help="푸시 전송 없이 출력만")
    ap.add_argument("--test-notify", action="store_true", help="지역별 푸시 테스트 알림 전송")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        print("config.json을 읽을 수 없습니다.", file=sys.stderr)
        return 1

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
    interval = max(300, args.loop)
    while True:
        try:
            run_once(load_json(CONFIG_PATH, cfg), args.dry_run)
        except Exception as e:
            print(f"실행 오류: {e}", file=sys.stderr)
        print(f"{interval}초 후 다시 확인합니다...\n")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
