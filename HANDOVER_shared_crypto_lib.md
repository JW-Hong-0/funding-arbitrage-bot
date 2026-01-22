# Shared Crypto Lib 인수인계

## 목적
- 여러 거래소(GRVT, Lighter, Variational)를 동일 인터페이스로 다루기 위한 공용 라이브러리.
- `AbstractExchange` 기반 CCXT 스타일 API 제공.

## 위치/구조
- 루트: `src/shared_crypto_lib`
- 핵심 파일
  - `src/shared_crypto_lib/base.py`: 공통 인터페이스 정의
  - `src/shared_crypto_lib/models.py`: 표준 모델(Ticker/Order/Position/FundingRate 등)
  - `src/shared_crypto_lib/factory.py`: 거래소 인스턴스 팩토리
  - `src/shared_crypto_lib/exchanges/grvt.py`
  - `src/shared_crypto_lib/exchanges/lighter.py`
  - `src/shared_crypto_lib/exchanges/variational.py`
  - 테스트: `src/shared_crypto_lib/tests/examples/*`

## 주요 동작 요약
- `load_markets`: 거래소별 최소 주문 수량/틱/레버리지/펀딩 주기 정보를 읽어 `MarketInfo`로 통일.
- `fetch_funding_rate`: 거래소별 펀딩비/다음 펀딩 시간 조회.
- `create_order`: 거래소별 주문 생성 및 표준 `Order`로 변환.

## 거래소별 주의사항
### GRVT
- `next_funding_time`이 **ns 단위로 내려올 가능성**이 있음.
  - 전략 로직에서는 ms 단위 기준으로 비교하므로 단위 변환 일치 필요.
- `create_order` 응답이 `id="0x00"`인 경우가 있음.
  - 현재는 client_order_id로 대체만 하고, **실제 주문 생성 확인 로직은 없음**.
  - `MODULAR_BOT_REVIEW.md`의 2.1 항목 참고(추가 검증 필요).

### Lighter
- `min_initial_margin_fraction`에서 `max_leverage` 계산 중.
- 펀딩 주기/시간은 API 값이 부정확하면 fallback으로 1h 사용.

### Variational
- `funding_rate`가 APR로 내려오는 것으로 가정되어 `MonitoringService`에서 환산 중.
- `load_markets`에서 `max_leverage`가 채워지지 않음 → 레버리지 상한값이 설정되지 않는 문제와 직결.
- `set_leverage`는 SDK의 `_set_leverage_raw` 호출만 수행하므로 **반드시 응답 검증 + get_leverage 확인이 필요**.

## 환경 변수 의존성
이 라이브러리 자체는 `.env`를 직접 읽지 않지만, 상위 봇 설정(`config.py`)에서 아래를 사용:
- GRVT: `GRVT_MAINNET_API_KEY`, `GRVT_MAINNET_SECRET_KEY`, `GRVT_MAINNET_TRADING_ACCOUNT_ID`
- Lighter: `LIGHTER_MAINNET_WALLET_ADDRESS`, `LIGHTER_MAINNET_PRIVATE_KEY`, `LIGHTER_MAINNET_PUBLIC_KEY`
- Variational: `VARIATIONAL_WALLET_ADDRESS`, `VARIATIONAL_JWT_TOKEN`, `VARIATIONAL_PRIVATE_KEY`

## 테스트 스크립트
- `src/shared_crypto_lib/tests/examples/scenario_full_mainnet.py`
- `src/shared_crypto_lib/tests/examples/grvt_leverage_probe.py`
- `src/shared_crypto_lib/tests/examples/grvt_set_initial_leverage_probe.py`

## 알려진 리스크/개선 포인트
- GRVT 주문 생성 응답 검증 부재 (`0x00` 대응).
- GRVT `next_funding_time` 단위 불일치 가능성.
- Variational `max_leverage` 미설정으로 레버리지 상한 미반영.
- Variational 펀딩 APR → 주기별 환산 로직 정교화 필요.
