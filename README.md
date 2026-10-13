# 공주·대전·청주 편의점 포켓몬카드 재고 알림

GS25 · CU · 세븐일레븐 · 이마트24 앱의 재고조회 결과를 15분마다 확인해서,
**매장에 재고가 새로 생기면 휴대폰 푸시로 알려주는** 봇이에요. 푸시는 무료 앱 [ntfy](https://ntfy.sh)로 받아요.
**알림은 지역별로 따로 와서, 휴대폰에서 받고 싶은 지역만 골라 구독**하면 돼요.
재고 현황은 GitHub Pages 대시보드(`docs/index.html`)에서 지역 탭으로 볼 수 있어요.

```
GitHub Actions (15분마다) ─▶ checker.py ─▶ 재고 API ─▶ 공주 / 대전 / 청주 매장만 추림
                                  │
                                  ├─ 0개 → 1개 이상으로 바뀐 매장 ─▶ 지역별 ntfy 주제로 푸시
                                  └─ state.json / docs/status.json 커밋 ─▶ 대시보드
```

## 알림 지역 고르기

알림은 지역마다 다른 주제(topic)로 보내져요. GitHub에 등록한 주제 이름 뒤에 지역 id가 붙어요.

| 지역 | ntfy 앱에서 구독할 주제 |
|---|---|
| 공주 | `내주제-gongju` |
| 대전 | `내주제-daejeon` |
| 청주 | `내주제-cheongju` |

예를 들어 GitHub Secret `NTFY_TOPIC`이 `gongju-poke-df1cb1e44a1b`이면, 대전 알림을 받으려면 ntfy 앱에서 `gongju-poke-df1cb1e44a1b-daejeon`을 구독하세요.
**받기 싫은 지역은 앱에서 구독을 해제하면 끝**이에요. (지역 자체를 아예 안 보내려면 `config.json`의 그 지역 `"notify"`를 `false`로 바꾸세요. 대시보드에는 계속 표시돼요.)

## 설정

### 1. 휴대폰에 ntfy 앱 설치 + 지역 구독
1. Play 스토어 / App Store에서 **ntfy** 설치 (가입 필요 없음).
2. 앱에서 **+** → 위 표대로 원하는 지역의 주제를 하나씩 구독.
   - 주제 이름을 아는 사람은 누구나 알림을 볼 수 있으니 추측하기 어려운 이름을 쓰세요.
3. 안드로이드는 ntfy 앱의 배터리 최적화를 꺼두면 알림이 더 빨리 와요.

### 2. GitHub 저장소 만들기
1. GitHub에서 새 저장소(Public)를 만들고 이 폴더 **안의** 파일을 모두 올리세요. `.github` 폴더도 꼭 포함하세요.
2. **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NTFY_TOPIC` / Secret: 주제 이름 (지역 id를 **빼고**, 예: `gongju-poke-df1cb1e44a1b`)
3. **Settings → Actions → General → Workflow permissions** 를 **Read and write permissions** 로 바꾸고 저장.

### 3. 테스트
1. **Actions** 탭 → `포켓몬카드 재고 확인` → **Run workflow** → `휴대폰 푸시 테스트 알림만 보내기` 체크 → 실행. 구독한 지역마다 "알림 연결 성공!" 푸시가 와요.
2. 한 번 더 체크 없이 Run workflow → 지역마다 "감시 시작" 메시지와 현재 재고 있는 매장 목록이 와요.
3. 이후로는 15분마다 자동 실행되고, **새로 입고된 매장이 있을 때만** 매장별로 푸시가 와요. 알림을 누르면 네이버 지도가 열리고 '전화' 버튼으로 바로 전화할 수 있어요.

### 4. 대시보드 켜기 (선택)
**Settings → Pages → Source: Deploy from a branch → Branch: `main` / 폴더: `/docs`** → 저장.
몇 분 뒤 `https://<아이디>.github.io/<저장소이름>/` 에서 공주·대전·청주 탭으로 재고 현황을 볼 수 있어요.

## 설정 바꾸기 — `config.json`

| 항목 | 설명 |
|---|---|
| `keywords` | 감시할 상품명 목록. **키워드마다 각 편의점에서 가장 잘 맞는 상품 1개**를 골라 재고를 봐요. 특정 확장팩을 노린다면 앱에서 검색했을 때 그 상품이 맨 위에 나오는 이름을 넣으세요. |
| `chains` | 조회할 편의점. 빼고 싶은 곳은 지우세요. |
| `regions[].notify` | `false`면 그 지역 푸시를 아예 안 보내요. |
| `regions[].centers` | 지역 안에서 매장을 찾는 기준 좌표들. 대전 7곳, 청주 5곳, 공주 3곳이 기본값이에요. 빠지는 동네가 있으면 좌표를 추가하세요. |
| `regions[].addressMustContain` | 주소에 이 글자가 있어야 그 지역 매장으로 봐요 (세종 등 주변 지역 매장은 제외됨). |
| `minQuantity` | 이 개수 이상일 때 "재고 있음"으로 봐요. |

지역을 더 넣고 싶으면 `regions`에 같은 형식으로 추가하면 돼요 (예: 세종 — `id: "sejong"`, `addressMustContain: ["세종"]`).

## 내 PC에서 돌리기 (GitHub 없이)
Python 3.9+만 있으면 돼요.
```bash
# Windows PowerShell
$env:NTFY_TOPIC="내주제이름"
python checker.py --loop 900        # 15분마다 확인
python checker.py --dry-run         # 알림 없이 결과만 출력
```

## 알아둘 점
- 재고 데이터는 오픈소스 프로젝트 [daiso-mcp](https://github.com/hmmhmmhm/daiso-mcp)가 운영하는 공개 API(`mcp.aka.page`)를 거쳐 가져와요. 편의점 공식 API가 아니라서 앱이 바뀌거나 그 서비스가 멈추면 조회가 실패할 수 있어요 ([서비스 상태](https://aka-page.betteruptime.com/)). 실패한 조합은 대시보드에 "조회 실패"로 표시되고 이전 상태를 유지해서 잘못된 알림이 가지 않아요.
- 그 API는 **IP당 하루 3,000회** 제한이 있어요. 3개 지역·카드팩 여러 종을 보느라 한 번 실행에 약 150~250회 호출해요. GitHub Actions는 실행할 때마다 보통 다른 IP를 써서 대개 괜찮지만, 대시보드에 `DAILY_RATE_LIMIT` 오류가 보이면 `.github/workflows/check.yml`의 `*/15`를 `*/30`으로 바꾸거나 maxProductsPerChain·좌표 수를 줄이세요. **내 PC에서 돌릴 때는 `--loop 1800`(30분) 이상**을 권장해요.
- GitHub 예약 실행은 몇 분씩 늦어질 수 있고, 저장소에 60일간 활동이 없으면 자동으로 꺼져요 (이 봇은 6시간마다 상태를 커밋해서 꺼지지 않게 해요).
- 앱 재고는 실제 진열 재고와 다를 수 있어요. 방문 전 매장에 전화로 확인하세요.
