# 추가 개발 및 개선 정리

## 2-1 로그 생성(엑셀 분석용)
### 목표
실제 매매/체결/포지션 변화를 CSV로 누적하고, 세션 종료 시 XLSX로 변환.

### 설계 방향
- **실시간 기록**: CSV append (잠금/손상 위험 최소화)
- **분석용 리포트**: XLSX로 변환(세션 종료 또는 1일 1회)

### 제안 스키마
- `trades.csv`: 주문/체결 이벤트
  - `ts_utc, exchange, symbol, side, order_type, price, qty, notional, fee, fee_asset, order_id, client_order_id, status, reason, strategy_id, signal_id`
- `positions.csv`: 포지션 스냅샷
  - `ts_utc, exchange, symbol, side, size, entry_price, mark_price, pnl, leverage, margin_type, liquidation_price`
- `funding.csv`: 펀딩 스냅샷
  - `ts_utc, exchange, symbol, funding_rate, funding_time, next_funding_time`
- `balances.csv`: 잔고 스냅샷
  - `ts_utc, exchange, asset, total, available, margin_used`
- `signals.csv`: 전략 판단 기록
  - `ts_utc, symbol, leg_long, leg_short, spread, projected_yield, decision, reason`

### 적용 위치
- 주문/체결: `src/trading_service.py`
- 포지션/잔고: `src/asset_manager.py`
- 시그널: `src/monitoring_service.py`

## 2-2 전략/로직 수정 필요 사항

### A. 다음 정각(다음 펀딩 이벤트) 기준 펀딩 계산 부재
**현상**  
전략 계산이 “다음 1시간 내 펀딩 이벤트”만 반영되어, GRVT(4h/8h)가 다음 정각에 펀딩이 없으면 0%로 취급되어야 하는 상황이 정확히 반영되지 않을 수 있음.

**원인 후보**
- `next_funding_time` 단위 불일치(ms vs ns) → 윈도우 판정이 틀어짐
- 고정 `ONE_HOUR_MS` 기준으로만 projected_yield 계산

**개선 방향**
1) `next_funding_time` 단위 정규화(ms)  
   - `MonitoringService._fetch_ticker_data`에서 GRVT 기준 변환 검증
2) “다음 정각” 기준 수익률 계산 로직 추가  
   - `projected_yield`를 **다음 펀딩 이벤트 시간(각 거래소의 next_funding_time)** 기준으로 계산
3) VAR 펀딩 APR 환산 로직을 `funding_interval_s` 기반으로 변경

관련 코드:
- `src/monitoring_service.py:321` (yield 계산)
- `src/shared_crypto_lib/exchanges/grvt.py:482` (GRVT next_funding_time)
- `src/shared_crypto_lib/exchanges/variational.py:390` (VAR funding_time)

### B. Variational 레버리지 설정 불일치
**현상**  
Lighter의 Max Leverage가 3배일 때, VAR이 5배로 들어간 이력 발생.

**원인**
- Variational `MarketInfo.max_leverage` 미세팅 → `_get_max_leverage()`가 `TARGET_LEVERAGE`로 fallback

**개선 방향**
1) VAR `load_markets`/`refresh_market_info`에서 `max_leverage` 채우기  
2) `set_leverage` 호출 후 `get_leverage` 재확인  
3) Taker-Taker 진입 시 `min(max_lev)`로 양쪽 동일 레버리지 강제

관련 코드:
- `src/shared_crypto_lib/exchanges/variational.py:54` (MarketInfo 초기값)
- `src/trading_service.py:533` (레버리지 계산/적용)

### C. 전략 페어 전환 시 포지션 유지 문제
**현상**  
전략 계산에서 Lighter↔GRVT가 유리한 상황이 떠도, 기존 Lighter↔VAR 포지션 유지로 전환이 없음.

**원인**
- `TradingService._process_opportunities`에서 `HEDGED` 상태면 신규 엔트리 자체를 건너뜀
- “리밸런싱” 또는 “전략 전환” 로직 부재

**개선 방향**
- 전략 시그널 변경 시 **기존 포지션 자동 청산 + 신규 진입** 옵션 추가
- 전환 비용/슬리피지 고려한 임계값 추가

관련 코드:
- `src/trading_service.py:344` (이미 포지션 있으면 스킵)

## 2-3 MODULAR_BOT_REVIEW.md 검토 요약

### 2.1 GRVT 주문 생성 후 추적 실패
**현상**  
`create_order` 응답이 `0x00`일 때 검증/재시도 없음.

**현재 상태**  
아직 동일 구조 유지됨 → 개선 필요.

**개선 방향**
- `create_order` 후 `fetch_open_orders`로 실제 주문 생성 확인
- `0x00` 응답 시 재시도 or client_order_id 기반 추적 강화

관련 코드:
- `src/shared_crypto_lib/exchanges/grvt.py:352`

### 3.1 데이터 업데이트 실패 시 방어 로직 부재
**현상**  
데이터 수집 실패해도 메인 루프 진행.

**개선 방향**
- Health check + circuit breaker 추가

관련 코드:
- `src/asset_manager.py`
- `src/monitoring_service.py`

### 4.2 타 거래소 주문 검증 로직
**현상**  
GRVT 외 거래소도 주문 생성 후 검증 없음.

**개선 방향**
- Lighter/Variational 주문 생성 후 확인 로직 추가

관련 코드:
- `src/shared_crypto_lib/exchanges/lighter.py`
- `src/shared_crypto_lib/exchanges/variational.py`

### 5.1 포지션 불일치 복구 로직 위험
**현상**  
자동 복구 로직이 복잡하고 디버깅/추적 어려움.

**개선 방향**
- 복구 실행 시 별도 알림/로그 강화
- 시뮬레이션 기반 단위 테스트 추가

관련 코드:
- `src/trading_service.py:_reconcile_state`
