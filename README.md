# Funding Arbitrage Bot

GRVT, Hyena, Variational 간 펀딩비 차이를 이용해 델타-중립 헷지 포지션을 구성하는 모듈러 봇이다.
다음 펀딩 이벤트(정각) 기준으로 수익이 가장 큰 조합을 선택하며, 불필요한 진입/청산을 줄이기 위한 히스테리시스와 복구 로직을 포함한다.

## 구조

```
funding_arbitrage_bot/
  src/                    # 메인 봇 로직
  libs/shared_crypto_lib  # 거래소 공통 라이브러리 (submodule)
  private/                # 민감 정보 (.env)
  logs/                   # 세션 로그
```

### 레포 구성
- 봇 레포: `funding-arbitrage-bot`
- 공유 라이브러리 레포: `perp-shared-lib` (submodule)

### 핵심 모듈
- `src/monitoring_service.py`: 시장 데이터/펀딩비 수집, 다음 펀딩 이벤트 기준 전략 신호 계산
- `src/trading_service.py`: 진입/청산/리밸런싱/헷지 복구
- `src/asset_manager.py`: 잔고/포지션/오픈오더 집계
- `libs/shared_crypto_lib`: 거래소 어댑터 (GRVT/Hyena/Variational)

## 동작 원리

1) 펀딩비 및 다음 펀딩 이벤트 시간 수집  
   - 각 거래소의 `next_funding_time`을 정규화(ms 기준)
   - 펀딩 주기가 다른 경우 **다음 정각/다음 펀딩 이벤트 기준**으로 비교

2) 수익 조합 산출  
   - 다음 펀딩 이벤트에 **참여하는 거래소만 펀딩비 반영**
   - 펀딩 이벤트가 없는 거래소는 0으로 취급
   - 예상 수익이 충분히 크면 `opportunity`로 판단

3) 리밸런싱/전환 억제(히스테리시스)  
   - 최소 유지시간(`STRATEGY_REBALANCE_MIN_HOLD_S`) 내에는 전환 금지
   - 전환 임계치: 다음 조합 수익이 **현재의 1.5배 이상**일 때만 전환
   - 너무 작은 수익은 `below_threshold`로 스킵

4) 주문 체결/헷지  
   - GRVT는 지정가(메이커) 중심
   - 반대편 거래소는 시장가/테이커 중심
   - GRVT 주문 `order_id=0x00`일 경우 `fetch_open_orders`로 실주문 검증
   - 부분 헷지 발생 시 복구 로직 실행

5) 부분 헷지 복구(안전장치)  
   - `PARTIAL_HEDGE` 감지 시 자동 복구 시도
   - 동일 티커 중복 헤지를 막기 위해 inflight TTL 적용
   - Hyena 주문은 체결 반영 여부를 포지션 스냅샷으로 재확인 후 재시도

## 데이터 흐름

- `MonitoringService`가 펀딩비/시장가를 수집하고 신호(`StrategySignal`)를 생성
- `AssetManager`가 3개 거래소의 잔고/포지션/오픈오더를 집계
- `TradingService`가 신호와 포지션 상태를 기반으로 진입/청산/리밸런싱/복구 수행
- 모든 이벤트는 `SessionLogManager`가 CSV와 `system.log`로 기록

## 설정(ENV)

기본 경로는 아래를 우선 탐색한다.
- `Perp_DEX/private/Funding_Arbitrage.env`
- 없으면 `funding_arbitrage_bot/private/Funding_Arbitrage.env`
- 직접 지정하려면 `BOT_ENV_PATH` 사용

필수 키(예시):
- `GRVT_API_KEY`, `GRVT_PRIVATE_KEY`, `GRVT_TRADING_ACCOUNT_ID`, `GRVT_ENV`
- `HYENA_PRIVATE_KEY` (또는 `HYPERLIQUID_PRIVATE_KEY`)
- (선택) `HYENA_WALLET_ADDRESS`, `HYENA_MAIN_ADDRESS`, `HYENA_DEX_ID=hyna`
- (선택) `HYENA_BUILDER_ADDRESS`, `HYENA_BUILDER_FEE`, `HYENA_BUILDER_MAX_FEE_RATE`
- `VARIATIONAL_WALLET_ADDRESS`, `VARIATIONAL_VR_TOKEN` (선택), `VARIATIONAL_PRIVATE_KEY`

전체 페어 자동 스캔:
```
AUTO_SYMBOLS=1
AUTO_SYMBOLS_MIN_EXCHANGES=2
AUTO_SYMBOLS_MAX=0   # 0이면 제한 없음
```

## 실행

```
cd /home/jeonguk/projects/Perp_DEX/bots/funding_arbitrage_bot
.venv/bin/python -m src.main_modular
```

UI를 켜려면:
```
UI_ENABLED=1 UI_PORT=8080 .venv/bin/python -m src.main_modular
```

## 서브모듈 동작

이 레포는 `libs/shared_crypto_lib`를 서브모듈로 포함한다.
- 로컬에서는 서브모듈 폴더의 변경이 즉시 반영된다.
- 원격에서 최신 라이브러리를 반영하려면  
  1) `perp-shared-lib` 커밋/푸시  
  2) 봇 레포에서 서브모듈 포인터 갱신 후 커밋/푸시

## 클론 방법 (서브모듈 포함)

```
git clone --recurse-submodules https://github.com/JW-Hong-0/funding-arbitrage-bot.git
```

이미 클론한 뒤라면:
```
git submodule update --init --recursive
```

## 로그

`logs/session_YYYYMMDD_HHMMSS/`에 세션별로 저장된다.
- `balances.csv`
- `positions.csv`
- `funding.csv`
- `signals.csv`
- `trades.csv`
- `system.log`

## 주의사항

- 네트워크 오류(401/timeout) 시 쿨다운 로직 적용
- 실제 주문이 들어가므로 운영 전 소액/검증 권장

## 진단 팁

- `system.log`에서 `PARTIAL_HEDGE`와 `auto_hedge` 로그로 복구 여부 확인
- `trades.csv`에서 `hedge_fill`, `auto_hedge`, `exit_maker` 이벤트 추적
- `positions.csv` 최신 스냅샷으로 실 포지션이 중립인지 확인

## SDK 설치

로컬 SDK 경로 사용 권장:
```
.venv/bin/pip install -e /path/to/grvt-pysdk --no-build-isolation
```

## HyENA 테스트

HyENA 전용 테스트 스크립트:
```
test_scripts/test_hyena_mainnet.py
```

필수 환경 변수:
```
HYENA_PRIVATE_KEY (또는 HYPERLIQUID_PRIVATE_KEY)
HYENA_SYMBOL=hyna:LIT
```

주문 테스트를 하려면:
```
HYENA_PLACE_ORDER=1
HYENA_TEST_USD=10
```
