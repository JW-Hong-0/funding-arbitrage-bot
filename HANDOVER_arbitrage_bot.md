# 아비트라지 봇 인수인계

## 목적
GRVT, Lighter, Variational 3개 거래소의 펀딩비 차이를 활용해 헷징/차익거래를 수행하는 모듈형 봇.

## 위치/엔트리
- 실행 모듈: `src/main_modular.py`
- 실행 예시:
  ```bash
  cd /home/jeonguk/projects/Arbitrage-Project
  /home/jeonguk/projects/Arbitrage-Project/.venv/bin/python -m src.main_modular
  ```

## 핵심 구성요소
- `MonitoringService` (`src/monitoring_service.py`)
  - 가격/펀딩 데이터 수집
  - 전략 시그널 계산 및 히스테리시스 적용
- `AssetManager` (`src/asset_manager.py`)
  - 잔고/포지션/주문 통합 상태 관리
- `TradingService` (`src/trading_service.py`)
  - 엔트리/헷징/청산 실행
  - GRVT Maker 주문 추적 및 헤징 트리거
- `dashboard.py`
  - 콘솔 요약 출력

## 전략 요약
- 펀딩비 기반 시그널 생성 → 베스트 롱/숏 조합 선택
- GRVT 포함 시 Maker-Taker, 그 외 Taker-Taker
- 히스테리시스: `STRATEGY_SWITCH_COOLDOWN_S`, `STRATEGY_SWITCH_DELTA_BPS`
- Exit: `EXIT_YIELD_THRESHOLD`, `EXIT_SPREAD_THRESHOLD` 기반

## 주요 설정
`src/config.py`
- 심볼: `SYMBOLS`, `SYMBOL_EXCLUDE`
- 레버리지/규모: `TARGET_LEVERAGE`, `MARGIN_PER_TRADE_USDT`
- GRVT Maker 유지: `GRVT_ORDER_TTL_S`, `GRVT_ORDER_MIN_HOLD_S`
- 전략 전환/청산: `STRATEGY_SWITCH_*`, `EXIT_*`

## 최근 관찰된 이슈
`/home/jeonguk/projects/Arbitrage-Project/modular_bot_30034.log` 기반
- 짧은 시간에 반복적인 `Exit trigger`가 다수 발생
- BERA 등 일부 티커에서 “엔트리 직후 곧바로 Exit” 발생
  - 원인 후보: `EXIT_*` 기준이 민감하거나 펀딩/스프레드 계산 변동이 과도함

## Known Gaps
1) 펀딩 계산이 “다음 1시간” 기준으로만 평가됨  
   → GRVT 4h/8h 주기 상황에서 다음 정각 기준 최적화가 약함
2) Variational 최대 레버리지 미반영  
   → `max_leverage` 정보가 누락되어 `TARGET_LEVERAGE`로 고정될 가능성
3) 포지션 “유지/전환” 로직 부재  
   → 전략이 바뀌더라도 기존 포지션 유지(리밸런싱 없음)
