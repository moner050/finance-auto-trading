# KIS domestic cash order evidence gate — 2026-08-18

**Status:** `BLOCKED_PROVIDER_EVIDENCE`

This record is a sanitized extraction from public, official Korea Investment &
Securities sources. Capture used documentation reads only. It did not read `.env`,
connect to a user database, call a live or paper broker endpoint, or place an order.
The machine-readable companion is
`domestic-cash-order-contract-2026-08-18.sanitized.json`.

## Resolved official facts

The official repository was pinned at commit
`b093e42ba32d1df5f5ddad7a71cb715cbc800832`. Its current configuration sample names
the real host `https://openapi.koreainvestment.com:9443` and paper host
`https://openapivts.koreainvestment.com:29443`.

The current cash-order sample and API portal agree on `POST
/uapi/domestic-stock/v1/trading/order-cash`. The current TR IDs are real sell
`TTTC0011U`, real buy `TTTC0012U`, paper sell `VTTC0011U`, and paper buy
`VTTC0012U`. The sample explicitly requires uppercase POST body keys. The portal
marks `CANO`, `ACNT_PRDT_CD`, `PDNO`, `ORD_DVSN`, `ORD_QTY`, and `ORD_UNPR` as
required; `SLL_TYPE`, `CNDT_PRIC`, and `EXCG_ID_DVSN_CD` are optional for the
general endpoint contract. The current repository sample is stricter: its function
requires and validates `EXCG_ID_DVSN_CD`, while only `SLL_TYPE` and `CNDT_PRIC` have
empty defaults. The evidence keeps this portal-versus-sample distinction explicit;
it does not flatten either source into a single requirement claim.

The API portal marks these headers required for this route: `content-type`
(`application/json; charset=utf-8`), `authorization` (`Bearer`), `appkey`,
`appsecret`, `tr_id`, and `custtype`. It marks `personalseckey`, `tr_cont`,
`seq_no`, `mac_address`, `phone_number`, `ip_addr`, and `gt_uid` optional, with
some becoming conditionally required for corporate clients.

The current wrapper has a separate, source-specific shape. Its base headers are
`Content-Type: application/json`, `Accept: text/plain`, standalone `charset: UTF-8`,
and a configured `User-Agent`; `_url_fetch` then sets `custtype: P` for each request.
The current `order_cash` sample passes `tr_cont` as `""`, no `appendHeaders`, and
`postFlag=True`. It omits the `hashFlag` argument, whose wrapper default is `True`, but
the hash-generation call itself is commented out. These wrapper and call facts are
not reclassified as portal-required headers. In particular, the portal's combined
content type and the wrapper's `application/json` plus standalone `charset` remain
distinct evidence.

The portal success example and property contract expose `rt_cd`, `msg_cd`, `msg1`,
and `output`. A successful output contains `KRX_FWDG_ORD_ORGNO`, `ODNO`, and
`ORD_TMD`. The current wrapper treats HTTP 200 as the response envelope accepted for
application-level decoding, while `rt_cd == "0"` is the application success signal.
The portal property contract declares `ODNO` length 10 and its success example uses a
10-digit value. The repository preview decoder now enforces that same exact 10-digit
shape and rejects the former local 8-digit assumption.

## Unresolved safety facts

The current portal route contract does not list `hashkey`. The current official
wrapper leaves `set_order_hash_key` commented out even though its function argument
defaults `hashFlag=True`. The official error list still defines `EGW00131` as an
invalid-hashkey error, and older official samples used hash generation. These facts do
not establish a durable omission guarantee for this order and account class. The
hashkey ruling remains `UNRESOLVED_LEGACY_CONFLICT`.

The submit success response has an organization number, order number, and time, but
no trading date. The official daily-order read endpoint exposes `ord_dt`, branch/order
numbers, instrument, side, quantity, price, and time. However, after a submit timeout
the caller has no confirmed `ODNO`, and the individual-client request has no documented
caller-supplied client-order ID. `gt_uid` is documented only as an optional corporate
header, not as an individual idempotency or recovery key.

The official error list says `EGW00301` and `EGW00302` require checking the immediately
preceding transaction after connection or transaction timeout. It does not document a
unique lookup from the original individual request, nor a deduplication or idempotency
guarantee. Consequently an absent or ambiguous read cannot prove that a retry is safe.
Automatic resubmission remains forbidden.

## Implementation boundary

No KIS writer, KIS write transport, or production dispatch composition is authorized
by this evidence. Existing KIS read transports remain POST-blocked. Resolution needs
official documentation or written provider confirmation for hashkey omission behavior,
a trading-date-bearing unique success identity, timeout recovery by a caller-supplied
unique request identity, and provider deduplication/idempotency semantics.

## Official sources

The three GitHub source entries in the sanitized JSON include human-facing blob URLs
and `raw.githubusercontent.com` content URLs. Their SHA-256 values explicitly hash
`RAW_FILE_BYTES`, not rendered blob pages.

- <https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/examples_llm/domestic_stock/order_cash/order_cash.py>
- <https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/examples_user/kis_auth.py>
- <https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/kis_devlp.yaml>
- <https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/order-cash>
- <https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/inquire-daily-ccld>
- <https://apiportal.koreainvestment.com/faq-error-code>
