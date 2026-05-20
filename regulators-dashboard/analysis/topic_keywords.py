"""Topic-cluster keyword dictionary for v0.4.

Topics are orthogonal to the v0.1 type tags — an item is "an enforcement
action" (type) **about** "crypto + AML" (topics). An action can match
multiple topics; multi-topic is honest.

Each phrase carries an integer weight:
    3 — highly distinctive (e.g. "etf", "ofac", "ransomware")
    2 — strong topic word ("cryptoasset", "money laundering")
    1 — soft / contextual ("token", "carbon")

Matching is case-insensitive whole-phrase (word-boundary anchored), same
mechanics as `classifier_keywords.py`. A topic fires when any phrase
matches with score ≥ 1.

To extend: add a phrase to the appropriate dict and re-run
`python3 -m analysis.topics` against the fixtures at the bottom of that
module. Avoid short common-English words; prefer multi-word forms.
"""

from __future__ import annotations

CRYPTO: dict[str, int] = {
    "crypto": 2,
    "cryptoasset": 3,
    "cryptoassets": 3,
    "cryptocurrency": 3,
    "cryptocurrencies": 3,
    "digital asset": 2,
    "digital assets": 2,
    "stablecoin": 3,
    "stablecoins": 3,
    "bitcoin": 3,
    "ethereum": 3,
    "ether": 1,
    "nft": 3,
    "nfts": 3,
    "non-fungible token": 3,
    "defi": 3,
    "decentralized finance": 3,
    "binance": 3,
    "coinbase": 3,
    "kraken": 2,
    "ftx": 3,
    "initial coin offering": 3,
    "ico": 1,
    "token offering": 2,
    "token sale": 2,
    "blockchain": 2,
}

ETF: dict[str, int] = {
    "etf": 3,
    "etfs": 3,
    "exchange-traded fund": 3,
    "exchange traded fund": 3,
    "exchange-traded funds": 3,
    "spot etf": 3,
    "spot bitcoin etf": 3,
    "spot ether etf": 3,
    "spot ethereum etf": 3,
    "spot crypto etf": 3,
}

AML: dict[str, int] = {
    "aml": 3,
    "anti-money laundering": 3,
    "anti money laundering": 3,
    "money laundering": 3,
    "kyc": 2,
    "know your customer": 3,
    "know-your-customer": 3,
    "customer due diligence": 3,
    "bank secrecy act": 3,
    "bsa": 1,
    "ofac": 3,
    "sanctions evasion": 3,
    "suspicious activity report": 3,
    "sars": 1,
    "fatf": 3,
    "beneficial ownership": 2,  # also tagged DISCLOSURE; multi-topic is fine
    "terrorist financing": 3,
    "counter-terrorist financing": 3,
}

DISCLOSURE: dict[str, int] = {
    "disclosure": 1,
    "disclosures": 1,
    "disclose": 1,
    "form 10-k": 3,
    "form 10-q": 3,
    "form 8-k": 3,
    "annual report": 1,
    "periodic reporting": 2,
    "beneficial ownership": 2,
    "climate disclosure": 3,  # also CLIMATE
    "climate-related disclosure": 3,  # also CLIMATE
    "esg disclosure": 3,  # also CLIMATE
    "sustainability reporting": 3,  # also CLIMATE
    "form pf": 2,  # also PRIVATEFUNDS
    "registration statement": 2,
    "proxy statement": 2,
}

MARKETSTRUCTURE: dict[str, int] = {
    "market structure": 3,
    "alternative trading system": 3,
    "dark pool": 3,
    "payment for order flow": 3,
    "pfof": 3,
    "high-frequency trading": 3,
    "high frequency trading": 3,
    "best execution": 3,
    "consolidated tape": 3,
    "tick size": 3,
    "order routing": 2,
    "t+1": 3,
    "t+2": 3,
    "settlement cycle": 3,
    "equity market": 2,
    "regulation nms": 3,
    "reg nms": 3,
}

PRIVATEFUNDS: dict[str, int] = {
    "private fund": 3,
    "private funds": 3,
    "hedge fund": 3,
    "hedge funds": 3,
    "private equity": 3,
    "venture capital": 3,
    "form pf": 3,
    "qualified client": 3,
    "accredited investor": 3,
    "investment adviser": 1,
    "limited partner": 1,
    "general partner": 1,
}

CYBER: dict[str, int] = {
    "cyber": 1,
    "cybersecurity": 3,
    "cyber security": 3,
    "ransomware": 3,
    "data breach": 3,
    "data security": 2,
    "cyber incident": 3,
    "incident response": 2,
    "third-party risk": 2,
    "operational resilience": 2,
    "dora": 1,  # EU Digital Operational Resilience Act — collides with English; keep weight low
}

CLIMATE: dict[str, int] = {
    "climate": 2,
    "climate-related": 3,
    "climate disclosure": 3,
    "esg": 2,
    "sustainability": 2,
    "sustainable finance": 3,
    "net zero": 3,
    "net-zero": 3,
    "carbon": 1,
    "emissions": 1,
    "greenhouse gas": 3,
    "green bond": 3,
    "transition risk": 3,
    "scope 1": 2,
    "scope 2": 2,
    "scope 3": 2,
    "mica": 1,  # EU Markets in Crypto Assets — short and ambiguous; weight low
}


TOPICS: dict[str, dict[str, int]] = {
    "crypto": CRYPTO,
    "etf": ETF,
    "aml": AML,
    "disclosure": DISCLOSURE,
    "marketstructure": MARKETSTRUCTURE,
    "privatefunds": PRIVATEFUNDS,
    "cyber": CYBER,
    "climate": CLIMATE,
}

# Display labels for the UI (lowercased keys above match the API contract).
TOPIC_LABELS: dict[str, str] = {
    "crypto": "Crypto",
    "etf": "ETF",
    "aml": "AML / sanctions",
    "disclosure": "Disclosure",
    "marketstructure": "Market structure",
    "privatefunds": "Private funds",
    "cyber": "Cyber",
    "climate": "Climate / ESG",
}
