#!/usr/bin/env python3
"""
FastAPI backend serving the crypto dashboard via REST + WebSocket.
Powers both the web dashboard and the iOS app.
"""

import asyncio
import json
import time
import math
import os
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import html as html_mod
import defusedxml.ElementTree as ET

import requests
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

from btc_analyzer import (
    ASSETS, WINDOW_MINUTES, WINDOW_SECONDS, HISTORY_DAYS,
    load_or_fetch, parse_klines, analyze_windows,
    compute_summary, compute_volatility, compute_per_second_velocity,
    EnsemblePredictor, generate_dashboard,
)
import database as db
import clob_trading as clob
import kalshi_trading as kalshi_auth
import long_term as lt
import indicators as ind
import derivatives as deriv
import macro
import backtest as bt
import exchanges as xch
import execution as exec_mod
import tax as tax_mod
import push as push_mod
import strategy as strat_mod
import billing as billing_mod
import digest as digest_mod

app = FastAPI(title="CryptoEdge", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # No CORS — no API
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─── Security Middleware ─────────────────────────────────────────────
_request_counts: dict[str, list[float]] = {}
RATE_LIMIT = 120  # requests per minute per IP
RATE_WINDOW = 60
_last_prune = 0.0
_PRUNE_INTERVAL = 300  # prune stale IPs every 5 minutes
_rate_lock = asyncio.Lock()


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    global _last_prune
    # Rate limiting per IP
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    async with _rate_lock:
        # Prune departed IPs periodically; hard-cap to prevent unbounded growth
        if now - _last_prune > _PRUNE_INTERVAL:
            _last_prune = now
            stale = [k for k, v in _request_counts.items() if not v or now - v[-1] > RATE_WINDOW]
            for k in stale:
                del _request_counts[k]
            if len(_request_counts) > 10000:
                _request_counts.clear()
            # _login_attempts pruning removed -- login rate limiting is handled by gateway
        reqs = _request_counts.get(ip, [])
        reqs = [t for t in reqs if now - t < RATE_WINDOW]
        if len(reqs) >= RATE_LIMIT and ip not in ("127.0.0.1", "::1"):
            return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
        reqs.append(now)
        _request_counts[ip] = reqs

    # CSRF protection: require X-Requested-With header on state-mutating requests
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        client_host = request.client.host if request.client else ""
        is_localhost = client_host in ("127.0.0.1", "::1", "localhost")
        if not is_localhost and request.headers.get("x-requested-with") != "XMLHttpRequest":
            return JSONResponse({"error": "CSRF check failed"}, status_code=403)

    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' wss:; frame-ancestors 'none'"
    if os.environ.get("GATEWAY_SSO_SECRET"):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ─── Unit System Toggle + Currency Picker ────────────────────────────
# Injected into all HTML responses. Walks the DOM and converts $1,234.56
# into the user-chosen currency, with the user-chosen number locale.
UNIT_TOGGLE_SCRIPT = """
<script>
(function() {
  if (window.__narveUnitToggleLoaded) return;
  window.__narveUnitToggleLoaded = true;
  let unitSystem = localStorage.getItem('narve_units') || 'american';
  let currencyCode = localStorage.getItem('narve_currency') || 'USD';
  let langCode = localStorage.getItem('narve_language') || 'en';
  function isMetric() { return unitSystem === 'european'; }
  function getLocale() { return isMetric() ? 'de-DE' : 'en-US'; }

  /* ----- i18n ----- */
  const LANGUAGES = [
    ['en','English'],['es','Espa\u00f1ol'],['de','Deutsch'],['fr','Fran\u00e7ais'],
    ['it','Italiano'],['pt','Portugu\u00eas'],['nl','Nederlands'],['pl','Polski'],
    ['ja','\u65e5\u672c\u8a9e'],['ko','\ud55c\uad6d\uc5b4'],['zh','\u4e2d\u6587'],['ru','\u0420\u0443\u0441\u0441\u043a\u0438\u0439'],
    ['hi','\u0939\u093f\u0928\u094d\u0926\u0940'],['ar','\u0627\u0644\u0639\u0631\u0628\u064a\u0629'],['bn','\u09ac\u09be\u0982\u09b2\u09be'],['ur','\u0627\u0631\u062f\u0648'],
    ['id','Bahasa Indonesia'],['tr','T\u00fcrk\u00e7e'],['vi','Ti\u1ebfng Vi\u1ec7t'],['th','\u0e44\u0e17\u0e22'],
  ];
  const I18N = {
    en: {'common.loading':'Loading...','common.refresh':'Refresh','common.search':'Search','common.error':'Error','nav.dashboard':'Dashboard','nav.settings':'Settings','nav.signOut':'Sign Out'},
    es: {'common.loading':'Cargando...','common.refresh':'Actualizar','common.search':'Buscar','common.error':'Error','nav.dashboard':'Panel','nav.settings':'Configuraci\u00f3n','nav.signOut':'Cerrar sesi\u00f3n'},
    de: {'common.loading':'Wird geladen...','common.refresh':'Aktualisieren','common.search':'Suchen','common.error':'Fehler','nav.dashboard':'\u00dcbersicht','nav.settings':'Einstellungen','nav.signOut':'Abmelden'},
    fr: {'common.loading':'Chargement...','common.refresh':'Actualiser','common.search':'Rechercher','common.error':'Erreur','nav.dashboard':'Tableau de bord','nav.settings':'Param\u00e8tres','nav.signOut':'D\u00e9connexion'},
    it: {'common.loading':'Caricamento...','common.refresh':'Aggiorna','common.search':'Cerca','common.error':'Errore','nav.dashboard':'Pannello','nav.settings':'Impostazioni','nav.signOut':'Esci'},
    pt: {'common.loading':'Carregando...','common.refresh':'Atualizar','common.search':'Pesquisar','common.error':'Erro','nav.dashboard':'Painel','nav.settings':'Configura\u00e7\u00f5es','nav.signOut':'Sair'},
    nl: {'common.loading':'Laden...','common.refresh':'Vernieuwen','common.search':'Zoeken','common.error':'Fout','nav.dashboard':'Dashboard','nav.settings':'Instellingen','nav.signOut':'Afmelden'},
    pl: {'common.loading':'\u0141adowanie...','common.refresh':'Od\u015bwie\u017c','common.search':'Szukaj','common.error':'B\u0142\u0105d','nav.dashboard':'Panel','nav.settings':'Ustawienia','nav.signOut':'Wyloguj'},
    ja: {'common.loading':'\u8aad\u307f\u8fbc\u307f\u4e2d...','common.refresh':'\u66f4\u65b0','common.search':'\u691c\u7d22','common.error':'\u30a8\u30e9\u30fc','nav.dashboard':'\u30c0\u30c3\u30b7\u30e5\u30dc\u30fc\u30c9','nav.settings':'\u8a2d\u5b9a','nav.signOut':'\u30b5\u30a4\u30f3\u30a2\u30a6\u30c8'},
    ko: {'common.loading':'\ub85c\ub529 \uc911...','common.refresh':'\uc0c8\ub85c \uace0\uce68','common.search':'\uac80\uc0c9','common.error':'\uc624\ub958','nav.dashboard':'\ub300\uc2dc\ubcf4\ub4dc','nav.settings':'\uc124\uc815','nav.signOut':'\ub85c\uadf8\uc544\uc6c3'},
    zh: {'common.loading':'\u52a0\u8f7d\u4e2d...','common.refresh':'\u5237\u65b0','common.search':'\u641c\u7d22','common.error':'\u9519\u8bef','nav.dashboard':'\u4eea\u8868\u677f','nav.settings':'\u8bbe\u7f6e','nav.signOut':'\u9000\u51fa'},
    ru: {'common.loading':'\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...','common.refresh':'\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c','common.search':'\u041f\u043e\u0438\u0441\u043a','common.error':'\u041e\u0448\u0438\u0431\u043a\u0430','nav.dashboard':'\u041f\u0430\u043d\u0435\u043b\u044c','nav.settings':'\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438','nav.signOut':'\u0412\u044b\u0439\u0442\u0438'},
    hi: {'common.loading':'\u0932\u094b\u0921 \u0939\u094b \u0930\u0939\u093e \u0939\u0948...','common.refresh':'\u0930\u093f\u092b\u094d\u0930\u0947\u0936','common.search':'\u0916\u094b\u091c\u0947\u0902','common.error':'\u0924\u094d\u0930\u0941\u091f\u093f','nav.dashboard':'\u0921\u0948\u0936\u092c\u094b\u0930\u094d\u0921','nav.settings':'\u0938\u0947\u091f\u093f\u0902\u0917\u094d\u0938','nav.signOut':'\u0938\u093e\u0907\u0928 \u0906\u0909\u091f'},
    ar: {'common.loading':'\u062c\u0627\u0631\u064a \u0627\u0644\u062a\u062d\u0645\u064a\u0644...','common.refresh':'\u062a\u062d\u062f\u064a\u062b','common.search':'\u0628\u062d\u062b','common.error':'\u062e\u0637\u0623','nav.dashboard':'\u0644\u0648\u062d\u0629 \u0627\u0644\u0645\u0639\u0644\u0648\u0645\u0627\u062a','nav.settings':'\u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a','nav.signOut':'\u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u062e\u0631\u0648\u062c'},
    bn: {'common.loading':'\u09b2\u09cb\u09a1 \u09b9\u099a\u09cd\u099b\u09c7...','common.refresh':'\u09b0\u09bf\u09ab\u09cd\u09b0\u09c7\u09b6','common.search':'\u0985\u09a8\u09c1\u09b8\u09a8\u09cd\u09a7\u09be\u09a8','common.error':'\u09a4\u09cd\u09b0\u09c1\u099f\u09bf','nav.dashboard':'\u09a1\u09cd\u09af\u09be\u09b6\u09ac\u09cb\u09b0\u09cd\u09a1','nav.settings':'\u09b8\u09c7\u099f\u09bf\u0982\u09b8','nav.signOut':'\u09b8\u09be\u0987\u09a8 \u0986\u0989\u099f'},
    ur: {'common.loading':'\u0644\u0648\u0688 \u06c1\u0648 \u0631\u06c1\u0627 \u06c1\u06d2...','common.refresh':'\u0631\u06cc\u0641\u0631\u06cc\u0634','common.search':'\u062a\u0644\u0627\u0634','common.error':'\u062e\u0631\u0627\u0628\u06cc','nav.dashboard':'\u0688\u06cc\u0634 \u0628\u0648\u0631\u0688','nav.settings':'\u0633\u06cc\u0679\u0646\u06af\u0632','nav.signOut':'\u0633\u0627\u0626\u0646 \u0622\u0624\u0679'},
    id: {'common.loading':'Memuat...','common.refresh':'Segarkan','common.search':'Cari','common.error':'Kesalahan','nav.dashboard':'Dasbor','nav.settings':'Pengaturan','nav.signOut':'Keluar'},
    tr: {'common.loading':'Y\u00fckleniyor...','common.refresh':'Yenile','common.search':'Ara','common.error':'Hata','nav.dashboard':'G\u00f6sterge Paneli','nav.settings':'Ayarlar','nav.signOut':'\u00c7\u0131k\u0131\u015f Yap'},
    vi: {'common.loading':'\u0110ang t\u1ea3i...','common.refresh':'L\u00e0m m\u1edbi','common.search':'T\u00ecm ki\u1ebfm','common.error':'L\u1ed7i','nav.dashboard':'B\u1ea3ng \u0111i\u1ec1u khi\u1ec3n','nav.settings':'C\u00e0i \u0111\u1eb7t','nav.signOut':'\u0110\u0103ng xu\u1ea5t'},
    th: {'common.loading':'\u0e01\u0e33\u0e25\u0e31\u0e07\u0e42\u0e2b\u0e25\u0e14...','common.refresh':'\u0e23\u0e35\u0e40\u0e1f\u0e23\u0e0a','common.search':'\u0e04\u0e49\u0e19\u0e2b\u0e32','common.error':'\u0e02\u0e49\u0e2d\u0e1c\u0e34\u0e14\u0e1e\u0e25\u0e32\u0e14','nav.dashboard':'\u0e41\u0e14\u0e0a\u0e1a\u0e2d\u0e23\u0e4c\u0e14','nav.settings':'\u0e01\u0e32\u0e23\u0e15\u0e31\u0e49\u0e07\u0e04\u0e48\u0e32','nav.signOut':'\u0e2d\u0e2d\u0e01\u0e08\u0e32\u0e01\u0e23\u0e30\u0e1a\u0e1a'},
  };
  function t(key) {
    const dict = I18N[langCode] || I18N.en;
    return dict[key] || I18N.en[key] || key;
  }
  function applyTranslations(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(el => {
      const v = t(el.getAttribute('data-i18n'));
      if (v) el.textContent = v;
    });
    scope.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const v = t(el.getAttribute('data-i18n-placeholder'));
      if (v) el.placeholder = v;
    });
    scope.querySelectorAll('[data-i18n-title]').forEach(el => {
      const v = t(el.getAttribute('data-i18n-title'));
      if (v) el.title = v;
    });
  }
  window.t = t;
  window.applyNarveTranslations = applyTranslations;

  const CURRENCIES = [
    ['USD','US Dollar'],['EUR','Euro'],['GBP','British Pound'],['JPY','Japanese Yen'],
    ['AUD','Australian Dollar'],['CAD','Canadian Dollar'],['CHF','Swiss Franc'],['CNY','Chinese Yuan'],
    ['HKD','Hong Kong Dollar'],['NZD','New Zealand Dollar'],['SEK','Swedish Krona'],['KRW','South Korean Won'],
    ['SGD','Singapore Dollar'],['NOK','Norwegian Krone'],['MXN','Mexican Peso'],['INR','Indian Rupee'],
    ['ZAR','South African Rand'],['TRY','Turkish Lira'],['BRL','Brazilian Real'],['DKK','Danish Krone'],
    ['PLN','Polish Zloty'],['THB','Thai Baht'],['IDR','Indonesian Rupiah'],['HUF','Hungarian Forint'],
    ['CZK','Czech Koruna'],['ILS','Israeli Shekel'],['PHP','Philippine Peso'],['MYR','Malaysian Ringgit'],
    ['RON','Romanian Leu'],['ISK','Icelandic Krona'],
  ];
  const FX_FALLBACK = {
    USD:1.0, EUR:0.92, GBP:0.79, JPY:150, AUD:1.52, CAD:1.36, CHF:0.88, CNY:7.20,
    HKD:7.83, NZD:1.65, SEK:10.5, KRW:1340, SGD:1.34, NOK:10.6, MXN:17.0,
    INR:83.0, ZAR:18.5, TRY:32.0, BRL:5.0, DKK:6.85, PLN:3.95, THB:35.0,
    IDR:15700, HUF:360, CZK:23.0, ILS:3.7, PHP:56.0, MYR:4.7, RON:4.6, ISK:137,
  };
  let _fxRates = FX_FALLBACK;

  function _readFxCache() {
    try { return JSON.parse(localStorage.getItem('narve_fx_rates') || 'null'); } catch { return null; }
  }
  function _writeFxCache(rates) {
    try { localStorage.setItem('narve_fx_rates', JSON.stringify({ rates: rates, fetched_at: Date.now() })); } catch {}
  }
  async function ensureFxRates() {
    const cached = _readFxCache();
    if (cached && cached.rates && Date.now() - cached.fetched_at < 3600000) {
      _fxRates = cached.rates;
      return _fxRates;
    }
    try {
      const r = await fetch('/api/fx-rates', { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      if (r.ok) {
        const data = await r.json();
        _fxRates = data.rates || FX_FALLBACK;
        _fxRates.USD = 1.0;
        _writeFxCache(_fxRates);
        return _fxRates;
      }
    } catch {}
    if (cached && cached.rates) { _fxRates = cached.rates; }
    return _fxRates;
  }
  function getRate(code) {
    if (!code || code === 'USD') return 1;
    return (_fxRates && _fxRates[code]) || FX_FALLBACK[code] || 1;
  }
  function getSymbol(code, locale) {
    try {
      const parts = new Intl.NumberFormat(locale || getLocale(), { style: 'currency', currency: code }).formatToParts(0);
      const sym = parts.find(p => p.type === 'currency');
      if (sym) return sym.value;
    } catch {}
    return code;
  }
  function symbolFirst(code, locale) {
    try {
      const parts = new Intl.NumberFormat(locale || getLocale(), { style: 'currency', currency: code }).formatToParts(0);
      const cIdx = parts.findIndex(p => p.type === 'currency');
      const nIdx = parts.findIndex(p => p.type === 'integer');
      return cIdx < nIdx;
    } catch { return true; }
  }

  // Convert any "$1,234.56" in a text node to the chosen currency / locale.
  function convertCurrencyText(text) {
    if (!text) return text;
    if (currencyCode === 'USD' && !isMetric()) return text;
    const loc = getLocale();
    const rate = getRate(currencyCode);
    const sym = getSymbol(currencyCode, loc);
    const symFirst = symbolFirst(currencyCode, loc);
    return text.replace(/\\$([+-]?)([\\d,]+(?:\\.\\d+)?)([KMBT]?)/g, function(match, sign, num, suffix) {
      const value = parseFloat(num.replace(/,/g, ''));
      if (isNaN(value)) return match;
      const decPart = num.includes('.') ? num.split('.')[1] : '';
      const decimals = decPart.length;
      const converted = value * rate;
      const formatted = converted.toLocaleString(loc, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: Math.max(decimals, suffix ? 1 : 0),
      });
      return symFirst
        ? sym + sign + formatted + suffix
        : sign + formatted + suffix + ' ' + sym;
    });
  }

  function walk(node) {
    if (node.nodeType === 3) {
      const newText = convertCurrencyText(node.nodeValue);
      if (newText !== node.nodeValue) node.nodeValue = newText;
    } else if (node.nodeType === 1) {
      const tag = node.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (node.classList && node.classList.contains('no-unit-convert')) return;
      for (let i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
    }
  }

  function applyUnits() {
    // Only walk the DOM if we're actually changing something.
    if (currencyCode !== 'USD' || isMetric()) walk(document.body);
    document.querySelectorAll('.narve-unit-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.unit === unitSystem);
    });
    const sel = document.getElementById('narve-currency-select');
    if (sel) sel.value = currencyCode;
  }

  window.setNarveUnits = function(sys) {
    if (sys === unitSystem) return;
    unitSystem = sys;
    localStorage.setItem('narve_units', sys);
    location.reload();
  };
  window.setNarveCurrency = function(code) {
    if (code === currencyCode) return;
    currencyCode = code;
    localStorage.setItem('narve_currency', code);
    location.reload();
  };
  window.setNarveLanguage = function(code) {
    if (!I18N[code] || code === langCode) return;
    langCode = code;
    localStorage.setItem('narve_language', code);
    document.documentElement.lang = code;
    applyTranslations();
    const sel = document.getElementById('narve-language-select');
    if (sel) sel.value = code;
  };

  function injectToggle() {
    if (document.getElementById('narve-unit-wrap')) return;
    const wrap = document.createElement('div');
    wrap.id = 'narve-unit-wrap';
    wrap.className = 'no-unit-convert';
    wrap.style.cssText = 'position:fixed;top:12px;right:12px;display:flex;gap:4px;z-index:9999;background:rgba(22,27,34,0.9);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:4px;backdrop-filter:blur(8px);align-items:center;';
    const usBtn = document.createElement('button');
    usBtn.className = 'narve-unit-btn';
    usBtn.dataset.unit = 'american';
    usBtn.title = 'American ($, MM/DD)';
    usBtn.textContent = '\U0001F1FA\U0001F1F8';
    usBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:4px 8px;font-size:14px;border-radius:4px;color:#8b949e;';
    usBtn.onclick = function() { window.setNarveUnits('american'); };
    const euBtn = document.createElement('button');
    euBtn.className = 'narve-unit-btn';
    euBtn.dataset.unit = 'european';
    euBtn.title = 'European (\u20ac, DD.MM)';
    euBtn.textContent = '\U0001F1EA\U0001F1FA';
    euBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:4px 8px;font-size:14px;border-radius:4px;color:#8b949e;';
    euBtn.onclick = function() { window.setNarveUnits('european'); };
    const langSel = document.createElement('select');
    langSel.id = 'narve-language-select';
    langSel.title = 'Language';
    langSel.style.cssText = 'background:rgba(0,0,0,0.4);color:#e6edf3;border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:3px 6px;font-size:11px;cursor:pointer;font-family:inherit;max-width:90px;';
    langSel.innerHTML = LANGUAGES.map(function(l) {
      return '<option value="' + l[0] + '"' + (l[0] === langCode ? ' selected' : '') + '>' + l[1] + '</option>';
    }).join('');
    langSel.onchange = function(e) { window.setNarveLanguage(e.target.value); };
    const sel = document.createElement('select');
    sel.id = 'narve-currency-select';
    sel.title = 'Display currency';
    sel.style.cssText = 'background:rgba(0,0,0,0.4);color:#e6edf3;border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:3px 6px;font-size:11px;cursor:pointer;font-family:inherit;';
    sel.innerHTML = CURRENCIES.map(function(c) {
      return '<option value="' + c[0] + '"' + (c[0] === currencyCode ? ' selected' : '') + '>' + c[0] + '</option>';
    }).join('');
    sel.onchange = function(e) { window.setNarveCurrency(e.target.value); };
    wrap.appendChild(usBtn);
    wrap.appendChild(euBtn);
    wrap.appendChild(langSel);
    wrap.appendChild(sel);
    document.body.appendChild(wrap);
    const style = document.createElement('style');
    style.textContent = '.narve-unit-btn.active { background: #58a6ff !important; color: #fff !important; }';
    document.head.appendChild(style);
  }

  function init() {
    document.documentElement.lang = langCode;
    injectToggle();
    applyTranslations();
    // Apply immediately with cached/fallback rates so the page never flashes
    // raw USD when the user prefers a different currency.
    applyUnits();
    // Then refresh from server in the background and re-apply.
    ensureFxRates().then(function() {
      if (currencyCode !== 'USD' || isMetric()) {
        // We already converted text nodes in applyUnits(); a second pass would
        // double-convert (e.g. €0,92 → €0,85). Reload instead so the text
        // starts fresh from server-rendered USD.
        const cached = _readFxCache();
        if (cached && Date.now() - cached.fetched_at < 60000) {
          // Rates were just updated — reload once to apply fresh values.
          if (!sessionStorage.getItem('narve_fx_reloaded')) {
            sessionStorage.setItem('narve_fx_reloaded', '1');
            location.reload();
            return;
          }
        }
      }
      sessionStorage.removeItem('narve_fx_reloaded');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>
"""


@app.middleware("http")
async def unit_toggle_middleware(request: Request, call_next):
    """Inject the unit-system toggle script into all HTML responses."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "text/html" not in ct:
        return response
    body_bytes = b""
    async for chunk in response.body_iterator:
        body_bytes += chunk
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return Response(content=body_bytes, status_code=response.status_code,
                        headers=dict(response.headers), media_type=ct)
    if "</body>" in body and "__narveUnitToggleLoaded" not in body:
        body = body.replace("</body>", UNIT_TOGGLE_SCRIPT + "</body>", 1)
    new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code,
                    headers=new_headers, media_type=ct)


# ─── Authentication ──────────────────────────────────────────────────
# Auth is handled by the gateway. These helpers extract user info from
# gateway SSO headers or allow localhost bypass for trading bots.


def _get_session_user(request: Request) -> dict | None:
    """Get the authenticated user from gateway SSO headers. Resolves the
    effective billing tier (free / pro / wealth / admin) via billing.get_tier()
    so feature-gating queries can consult `user['tier']` directly."""
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret and hmac.compare_digest(request.headers.get("x-gateway-secret", ""), _sso_secret):
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        gw_role = request.headers.get("x-gateway-user-role", "")
        if gw_id and gw_email:
            gateway_tier = "admin" if gw_role == "admin" else None
            return {
                "id": gw_id,
                "email": gw_email,
                "tier": billing_mod.get_tier(gw_id, gateway_tier=gateway_tier),
                "display_name": gw_email.split("@")[0],
            }

    # Localhost bypass for trading bots — only when explicitly enabled via env var.
    if os.environ.get("DEV_LOCALHOST_BYPASS", "").strip() == "1":
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return {"id": "00000000-0000-0000-0000-000000000000",
                    "email": "localhost", "tier": "admin",
                    "display_name": "System"}
    return None


def _require_feature(user: dict, feature: str) -> None:
    """Raise 402 (Payment Required) if the user's tier doesn't unlock the
    feature. Distinct from 401 (not authed) and 403 (authed but forbidden
    for non-tier reasons)."""
    if not billing_mod.feature_allowed(user["tier"], feature):
        min_tier = billing_mod.FEATURE_TIERS.get(feature, "free")
        raise HTTPException(
            status_code=402,
            detail=f"Feature '{feature}' requires {min_tier} tier (current: {user['tier']})",
        )


def _check_auth(request: Request) -> bool:
    return _get_session_user(request) is not None


def _is_premium(request: Request) -> bool:
    user = _get_session_user(request)
    return user is not None and user["tier"] in ("premium", "admin")


async def require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ─── In-memory state ─────────────────────────────────────────────────
asset_state = {}       # ticker -> full result dict
ensembles = {}         # ticker -> trained EnsemblePredictor
live_prices = {}       # ticker -> latest price
connected_ws = set()   # active WebSocket connections
_bg_tasks: set = set() # prevent GC of background tasks
_ws_lock = asyncio.Lock()
last_refresh = {}      # ticker -> timestamp of last full refresh
REFRESH_INTERVAL = 300 # re-analyze every 5 min (1 window)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"


# ─── Startup: load cached data + train models ────────────────────────
@app.on_event("startup")
async def startup():
    db.init_db()
    # Start background tasks immediately so dashboards work
    _bg_tasks.add(asyncio.create_task(price_updater()))
    _bg_tasks.add(asyncio.create_task(window_refresher()))
    _bg_tasks.add(asyncio.create_task(news_trade_monitor()))
    _bg_tasks.add(asyncio.create_task(long_term_refresher()))
    _bg_tasks.add(asyncio.create_task(derivatives_refresher()))
    _bg_tasks.add(asyncio.create_task(macro_refresher()))
    _bg_tasks.add(asyncio.create_task(execution_ticker()))
    _bg_tasks.add(asyncio.create_task(fill_poller_task()))
    _bg_tasks.add(asyncio.create_task(digest_cron_task()))
    _bg_tasks.add(asyncio.create_task(strategy_subscription_ticker()))
    # Load data in background so server is available immediately
    _bg_tasks.add(asyncio.create_task(load_all_assets()))
    print("Server started. Loading data in background...")


async def long_term_refresher():
    """Refresh daily bars + on-chain metrics on startup, then every 6h.
    The first pass runs after a short delay so the short-term pipeline gets
    its head start; subsequent passes are cheap (incremental upserts).
    After each refresh we also rerun the indicator backtests so the UI
    surfaces fresh stats."""
    await asyncio.sleep(30)
    while True:
        try:
            result = await asyncio.to_thread(lt.refresh_all)
            print(f"[long-term] refresh: {result}")
            await asyncio.to_thread(_evaluate_long_term_alerts)
            # Recompute backtests after fresh data. Cheap (~10s).
            bt_summary = await asyncio.to_thread(bt.run_all)
            print(f"[long-term] backtest: {bt_summary}")
        except Exception as e:
            print(f"[long-term] refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(6 * 3600)


async def derivatives_refresher():
    """Derivatives change fast. Refresh hourly."""
    await asyncio.sleep(60)
    while True:
        try:
            result = await asyncio.to_thread(deriv.refresh_all_derivatives)
            print(f"[derivatives] refresh: {result}")
        except Exception as e:
            print(f"[derivatives] refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(3600)


async def macro_refresher():
    """Macro series update daily at most. Refresh every 12h."""
    await asyncio.sleep(90)
    while True:
        try:
            result = await asyncio.to_thread(macro.refresh_all_macro)
            print(f"[macro] refresh: {result}")
        except Exception as e:
            print(f"[macro] refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(12 * 3600)


async def digest_cron_task():
    """Hourly tick that sends weekly digests to users whose preferred
    day-of-week is today and who haven't received one in the last 6 days
    (debounce). Reuses the existing SMTP transport in email_alerts.py."""
    await asyncio.sleep(300)  # let other tasks warm up
    while True:
        try:
            result = await asyncio.to_thread(digest_mod.run_digest_tick)
            if result["sent"] or result["failed"]:
                print(f"[digest] {result}")
        except Exception as e:
            print(f"[digest] tick error: {type(e).__name__}: {e}")
        await asyncio.sleep(3600)


async def strategy_subscription_ticker():
    """Drive live strategy subscriptions. Runs every 5 minutes; each due
    subscription's `evaluate_today` output flows through the same safety
    gauntlet the manual DCA executor uses."""
    await asyncio.sleep(240)  # let everything else warm up first
    while True:
        try:
            summary = await asyncio.to_thread(exec_mod.tick_subscriptions)
            if summary["checked"]:
                print(f"[strat-sub] {summary}")
            # Push notification for each placed/blocked outcome.
            for a in summary.get("actions", []):
                if a.get("action") in ("placed", "blocked", "dry_run"):
                    try:
                        push_mod.notify_user(
                            a["user_id"],
                            title=f"Strategy run · {a.get('ticker', '')}",
                            body=f"{a['action']}: {a.get('reason', '')}",
                            url="/long-term#strategies",
                            tag=f"strat-{a['strategy_id']}",
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"[strat-sub] tick error: {type(e).__name__}: {e}")
        await asyncio.sleep(300)


async def fill_poller_task():
    """Poll open orders every 60s. Updates execution rows from placed → filled
    and (for sells) creates a tax disposition automatically. Idempotent."""
    await asyncio.sleep(180)  # let the executor place at least one order first
    while True:
        try:
            with db._conn() as c:
                user_ids = [r["user_id"] for r in c.execute(
                    "SELECT DISTINCT user_id FROM crypto_executions "
                    "WHERE status='open' AND order_id IS NOT NULL"
                ).fetchall()]
            for uid in user_ids:
                summary = await asyncio.to_thread(exec_mod.poll_fills, uid)
                if summary["filled"] or summary["dispositions"]:
                    print(f"[fill-poller] {uid}: {summary}")
                    # Notify the user of every newly-filled order.
                    if summary["filled"]:
                        try:
                            push_mod.notify_user(
                                uid, "Order filled",
                                f"{summary['filled']} order(s) filled, "
                                f"{summary['dispositions']} disposition(s) recorded.",
                                "/long-term#execution", tag="fill",
                            )
                        except Exception:
                            pass
        except Exception as e:
            print(f"[fill-poller] error: {type(e).__name__}: {e}")
        await asyncio.sleep(60)


async def execution_ticker():
    """Process due DCA schedules + reconcile stale orders every 5 minutes.
    All execution still passes through the safety gauntlet — dry-run by
    default, per-order and per-day caps, circuit breaker. We rely on
    those rails; this task just decides *when* to evaluate."""
    await asyncio.sleep(120)  # let the price/onchain data load first
    while True:
        try:
            summary = await asyncio.to_thread(exec_mod.tick_due_schedules)
            if summary["checked"]:
                print(f"[exec] {summary}")
            # Fire push for every actionable outcome.
            for a in summary.get("actions", []):
                if a.get("action") in ("placed", "blocked", "filled"):
                    try:
                        push_mod.notify_execution(
                            a["user_id"], a["ticker"], a["action"], a.get("reason", ""),
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"[exec] tick error: {type(e).__name__}: {e}")
        try:
            # Reconcile stale orders for every user that has at least one open.
            with db._conn() as c:
                user_ids = [r["user_id"] for r in c.execute(
                    "SELECT DISTINCT user_id FROM crypto_executions "
                    "WHERE status='open' AND order_id IS NOT NULL"
                ).fetchall()]
            for uid in user_ids:
                n = await asyncio.to_thread(exec_mod.reconcile_open_orders, uid)
                if n:
                    print(f"[exec] reconciled {n} stale orders for {uid}")
        except Exception as e:
            print(f"[exec] reconcile error: {type(e).__name__}: {e}")
        await asyncio.sleep(300)


def _evaluate_long_term_alerts():
    """Check all active alerts against current snapshots and mark fired ones.
    This is a placeholder for a full notification pipeline — for now it just
    records last_fired_at so the UI can display 'recently fired' state."""
    try:
        alerts = db.get_long_term_alerts()
    except Exception:
        return
    snaps = {s["ticker"]: s for s in lt.all_snapshots()}
    for a in alerts:
        snap = snaps.get(a["ticker"])
        if not snap or not snap.get("ready"):
            continue
        atype = a["alert_type"]
        thresh = a["threshold"]
        fired = False
        if atype == "drawdown" and thresh is not None:
            fired = (snap.get("current_dd") or 0) <= -abs(float(thresh))
        elif atype == "mvrv_high" and thresh is not None:
            mvrv = snap.get("mvrv")
            fired = mvrv is not None and mvrv >= float(thresh)
        elif atype == "mvrv_low" and thresh is not None:
            mvrv = snap.get("mvrv")
            fired = mvrv is not None and mvrv <= float(thresh)
        elif atype == "vol_regime":
            fired = snap.get("vol_regime") in ("elevated", "extreme")
        elif atype == "risk_off" and thresh is not None:
            fired = (snap.get("risk_off") or {}).get("score", 0) >= float(thresh)
        if fired:
            try:
                db.mark_long_term_alert_fired(a["id"])
                db.log_alert(a["user_id"], a["ticker"], f"long_term:{atype}",
                             f"{atype} threshold reached", confidence=0.0)
                push_mod.notify_long_term_alert(
                    a["user_id"], a["ticker"], atype,
                    f"{atype.replace('_',' ').title()} threshold reached for {a['ticker']}.",
                )
            except Exception:
                pass


async def load_all_assets():
    """Load all assets in background so server can serve pages immediately."""
    print(f"Loading {HISTORY_DAYS}d data and training models...")

    # Phase 1: Load all windows FIRST so all tabs appear on dashboard
    all_windows = {}
    for ticker, info in ASSETS.items():
        try:
            await asyncio.to_thread(load_asset_windows, ticker, info["symbol"])
            print(f"  {ticker} windows ready.")
        except Exception as e:
            print(f"  {ticker} failed to load: {e}")
    print("All asset windows loaded — dashboard ready.")

    # Phase 2: Train models for each asset (slow part, dashboard already usable)
    for ticker, info in ASSETS.items():
        if ticker not in asset_state:
            continue
        try:
            await asyncio.to_thread(train_asset_models, ticker)
            print(f"  {ticker} models trained.")
        except Exception as e:
            print(f"  {ticker} training failed: {e}")
    print("All models trained.")


def load_asset_windows(ticker, symbol):
    """Load cached data and analyze windows (fast part)."""
    import gc
    raw, start_dt, end_dt = load_or_fetch(symbol, days=HISTORY_DAYS)
    data = parse_klines(raw)
    del raw  # free ~400MB JSON
    gc.collect()

    windows = analyze_windows(data)
    summary = compute_summary(windows)
    volatility = compute_volatility(windows, lookback_hours=24)
    velocity = compute_per_second_velocity(data, windows)

    # Only keep last 2h of raw data for live updates (not all 30d)
    recent_data = data[-14400:] if len(data) > 14400 else data  # last 4h
    # Only keep last 1000 windows
    recent_windows = windows[-1000:] if len(windows) > 1000 else windows

    asset_state[ticker] = {
        "windows": recent_windows,
        "summary": summary,
        "volatility": volatility,
        "velocity": velocity,
        "backtest": None,
        "predictions": None,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "data": recent_data,
        "_all_windows": windows,  # keep for training phase
    }
    last_refresh[ticker] = time.time()

    del data
    gc.collect()


MODEL_CACHE_DIR = Path(__file__).parent / "cache" / "models"

def train_asset_models(ticker):
    """Train ensemble models for an asset (slow GPU part). Uses cached models if available."""
    import gc
    windows = asset_state[ticker].pop("_all_windows", None)
    if windows is None:
        return

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_CACHE_DIR / f"{ticker}_ensemble.json"

    # Try loading cached model first
    ensemble = EnsemblePredictor.load_from_file(model_path)
    if ensemble:
        print(f"  {ticker} loaded cached model ({len(ensemble.models)} models)")
    else:
        # Train from scratch
        ensemble = EnsemblePredictor()
        ensemble.train_all(windows, verbose=False)
        # Save for next time
        try:
            ensemble.save_to_file(model_path)
            print(f"  {ticker} model saved to cache")
        except Exception as e:
            print(f"  {ticker} model save failed: {e}")

    bt = ensemble.backtest(windows)
    preds = ensemble.predict_current_and_recent(windows)

    ensembles[ticker] = ensemble
    asset_state[ticker]["backtest"] = bt
    asset_state[ticker]["predictions"] = preds
    asset_state[ticker]["model_info"] = ensemble.model_info

    del windows
    gc.collect()


async def price_updater():
    """Connect to Binance WebSocket stream for real-time prices, with REST fallback."""
    import aiohttp

    # Build combined stream URL for all assets
    # e.g. wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/...
    symbols_lower = [info["symbol"].lower() for info in ASSETS.values()]
    symbol_to_ticker = {info["symbol"]: ticker for ticker, info in ASSETS.items()}
    streams = "/".join(f"{s}@miniTicker" for s in symbols_lower)
    ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    last_push = 0
    pending_update: dict = {}

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, heartbeat=20) as ws:
                    print("[WS] Connected to Binance stream")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            payload = data.get("data", {})
                            symbol = payload.get("s", "")  # e.g. "BTCUSDT"
                            price = float(payload.get("c", 0))  # close price

                            if symbol in symbol_to_ticker and price > 0:
                                ticker = symbol_to_ticker[symbol]
                                live_prices[ticker] = price
                                pending_update[ticker] = {
                                    "price": price,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }

                            # Batch push to clients every 1 second (avoid flooding)
                            now = time.time()
                            if pending_update and now - last_push >= 1.0:
                                ws_msg = json.dumps({"type": "price_update", "data": pending_update})
                                dead = set()
                                async with _ws_lock:
                                    for client_ws in list(connected_ws):
                                        try:
                                            await client_ws.send_text(ws_msg)
                                        except Exception:
                                            dead.add(client_ws)
                                    for d in dead:
                                        connected_ws.discard(d)
                                pending_update = {}
                                last_push = now

                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
        except Exception as e:
            print(f"[WS] Binance stream error: {e}, reconnecting in 5s...")

        # Fallback: fetch REST prices while reconnecting
        try:
            resp = await asyncio.to_thread(requests.get, BINANCE_TICKER_URL, params={}, timeout=5)
            if resp.ok:
                all_prices = {p["symbol"]: float(p["price"]) for p in resp.json()}
                for ticker, info in ASSETS.items():
                    if info["symbol"] in all_prices:
                        live_prices[ticker] = all_prices[info["symbol"]]
        except Exception:
            pass
        await asyncio.sleep(5)


async def window_refresher():
    """Re-analyze windows and update predictions every 5 minutes."""
    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.time()
        for ticker, info in ASSETS.items():
            if ticker not in asset_state:
                continue
            if now - last_refresh.get(ticker, 0) < REFRESH_INTERVAL:
                continue

            try:
                # Fetch latest 10 minutes of data to append
                symbol = info["symbol"]
                end_ms = int(now * 1000)
                start_ms = end_ms - (600 * 1000)  # last 10 min
                params = {
                    "symbol": symbol, "interval": "1s",
                    "startTime": start_ms, "endTime": end_ms, "limit": 1000,
                }
                resp = await asyncio.to_thread(
                    requests.get, BINANCE_KLINE_URL, params=params, timeout=15
                )
                if not resp.ok:
                    continue

                new_klines = resp.json()
                new_data = [(k[0], float(k[4])) for k in new_klines]

                # Merge with existing data (dedup by timestamp)
                existing = asset_state[ticker]["data"]
                existing_ts = {ts for ts, _ in existing}
                for ts, price in new_data:
                    if ts not in existing_ts:
                        existing.append((ts, price))
                existing.sort(key=lambda x: x[0])
                # Keep bounded (slice assignment to mutate the original list in-place)
                if len(existing) > 20000:
                    existing[:] = existing[-20000:]

                # Re-analyze the recent data to get latest windows
                new_windows = analyze_windows(existing)

                # Merge new windows into stored windows
                old_windows = asset_state[ticker]["windows"]
                # Find latest stored window time
                if old_windows:
                    last_stored = old_windows[-1]["start"]
                    for w in new_windows:
                        if w["start"] > last_stored:
                            old_windows.append(w)
                    # Keep bounded (slice assignment to mutate the original list in-place)
                    if len(old_windows) > 1000:
                        old_windows[:] = old_windows[-1000:]
                else:
                    asset_state[ticker]["windows"] = new_windows[-500:]
                    old_windows = asset_state[ticker]["windows"]

                # Update predictions using the full window history
                if ticker not in ensembles:
                    # Still save the windows even if models aren't trained yet
                    asset_state[ticker].update({"windows": old_windows})
                    continue
                preds = ensembles[ticker].predict_current_and_recent(old_windows)

                # Log predictions to DB for accuracy tracking
                for p in preds:
                    ws = p.get("window_start")
                    if ws and hasattr(ws, "isoformat"):
                        ws_str = ws.isoformat()
                    else:
                        ws_str = str(ws) if ws else ""
                    if p.get("is_current"):
                        db.log_prediction(
                            ticker=ticker, window_start=ws_str,
                            pred_direction=p["pred_direction"],
                            pred_delta=p["pred_end_delta"],
                            pred_prob=p["pred_prob_positive"],
                            confidence=p.get("confidence", 0),
                            ensemble_agreement=p.get("ensemble_agreement", ""),
                        )
                    elif p.get("actual_direction"):
                        db.resolve_prediction(
                            ticker=ticker, window_start=ws_str,
                            actual_direction=p["actual_direction"],
                            actual_delta=p.get("actual_end_delta", 0) or 0,
                        )

                asset_state[ticker].update({
                    "windows": old_windows,
                    "predictions": preds,
                    "data": existing,
                })
                last_refresh[ticker] = now

                # Push update to WebSocket clients
                msg = json.dumps({
                    "type": "window_update",
                    "ticker": ticker,
                    "data": serialize_asset(ticker),
                })
                dead = set()
                async with _ws_lock:
                    for ws in list(connected_ws):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.add(ws)
                    for d in dead:
                        connected_ws.discard(d)

                # Push high-confidence signal alerts (browser + email)
                if preds:
                    for p in preds:
                        if p.get("is_current") and p.get("confidence", 0) >= 0.6:
                            conf = int(p["confidence"] * 100)
                            delta_str = f'{p["pred_end_delta"]:+,.2f}'

                            # Browser push
                            alert_msg = json.dumps({
                                "type": "alert",
                                "data": {
                                    "ticker": ticker,
                                    "direction": p["pred_direction"],
                                    "confidence": conf,
                                    "delta": delta_str,
                                    "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                                },
                            })
                            dead = []
                            async with _ws_lock:
                                for ws in list(connected_ws):
                                    try:
                                        await ws.send_text(alert_msg)
                                    except Exception:
                                        dead.append(ws)
                                for ws in dead:
                                    connected_ws.discard(ws)

                            # Email alerts to users who opted in
                            try:
                                from email_alerts import send_alert_email, is_configured
                                if is_configured():
                                    # Get all users with email alerts enabled for this ticker
                                    prefs = db.get_alert_prefs_for_ticker(ticker) if hasattr(db, 'get_alert_prefs_for_ticker') else []
                                    for pref in prefs:
                                        if pref.get("alert_email") and p["confidence"] >= pref.get("min_confidence", 0.6):
                                            user = db.get_user(pref["user_id"])
                                            if user:
                                                await asyncio.to_thread(
                                                    send_alert_email,
                                                    user["email"],
                                                    f"CryptoEdge: {ticker} {p['pred_direction'].upper()} ({conf}%)",
                                                    ticker, p["pred_direction"], conf, delta_str,
                                                )
                                                db.log_alert(user["id"], ticker, "email", f"{p['pred_direction']} {conf}%", p["confidence"])
                            except Exception as e:
                                print(f"  Email alert error: {e}")

                print(f"  Refreshed {ticker} windows at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
            except Exception as e:
                print(f"  Refresh error {ticker}: {e}")


# ─── News-Trade Correlation Monitor ─────────────────────────────────

last_news_trade_scan: dict = {}
last_news_trade_time: float = 0


async def news_trade_monitor():
    """Scan news for insider-trading stories every 20 minutes.
    Cross-references with Polymarket, persists to DB, pushes WebSocket
    alerts for high-score items, and sends email to watchlist subscribers.
    """
    global last_news_trade_scan, last_news_trade_time

    # Wait for server to be ready
    await asyncio.sleep(45)

    while True:
        try:
            from news_trade_scanner import run_news_trade_scan
            result = await asyncio.to_thread(run_news_trade_scan)

            if result and result.get("alerts"):
                last_news_trade_scan = result
                last_news_trade_time = time.time()

                # Persist alerts to DB
                for alert in result["alerts"]:
                    try:
                        db.upsert_news_alert(alert)
                    except Exception as e:
                        print(f"  [NEWS-TRADE] DB error: {e}")

                # Find new high-score alerts to push
                new_alerts = db.get_unnotified_alerts(min_score=30)
                for alert in new_alerts:
                    # Push WebSocket notification
                    ws_msg = json.dumps({
                        "type": "alert",
                        "data": {
                            "ticker": "NEWS",
                            "direction": "news_trade",
                            "confidence": alert["score"],
                            "delta": f'[{alert["source"]}] {alert["title"][:60]}',
                            "time": alert.get("scanned_at", ""),
                            "alert_id": alert["id"],
                            "link": alert.get("link", ""),
                            "related_markets": alert.get("related_markets", []),
                        },
                    })
                    async with _ws_lock:
                        for ws in list(connected_ws):
                            try:
                                await ws.send_text(ws_msg)
                            except Exception:
                                connected_ws.discard(ws)

                    # Email watchlist subscribers
                    watchers = db.get_watchlist_users_for_alert(alert["id"])
                    for w in watchers:
                        if w.get("notify_email") and w.get("email"):
                            try:
                                from email_alerts import send_news_trade_alert
                                await asyncio.to_thread(
                                    send_news_trade_alert, w["email"], dict(alert)
                                )
                            except Exception:
                                pass

                    db.mark_alert_notified(alert["id"])

                print(f"  [NEWS-TRADE] Scan complete: {len(result['alerts'])} alerts, {len(new_alerts)} new pushes")
        except Exception as e:
            print(f"  [NEWS-TRADE] Monitor error: {e}")

        await asyncio.sleep(1200)  # re-scan every 20 minutes


def serialize_asset(ticker):
    """Convert asset state to JSON-safe dict."""
    if ticker not in asset_state:
        return {}
    st = asset_state[ticker]
    s = st["summary"]
    vol = st["volatility"]
    vel = st["velocity"]
    bt = st["backtest"]

    preds_out = []
    for p in (st["predictions"] or []):
        preds_out.append({
            "window_start": p["window_start"].isoformat() if hasattr(p["window_start"], "isoformat") else str(p["window_start"]),
            "pred_direction": p["pred_direction"],
            "pred_end_delta": p["pred_end_delta"],
            "pred_prob_positive": p["pred_prob_positive"],
            "confidence": p["confidence"],
            "is_current": p["is_current"],
            "actual_end_delta": p["actual_end_delta"],
        })

    # Last 20 windows for the API (not all 8600+)
    recent_windows = []
    for w in st["windows"][-20:]:
        recent_windows.append({
            "start": w["start"].isoformat(),
            "baseline": w["baseline"],
            "end_delta": w["end_delta"],
            "max_positive": w["max_positive"],
            "max_negative": w["max_negative"],
            "last_cross_sec": w["last_cross_sec"],
            "last_cross_direction": w["last_cross_direction"],
            "rsi": w["rsi"],
            "crossings": w["crossings"],
            "avg_pos_magnitude": w["avg_pos_magnitude"],
            "avg_neg_magnitude": w["avg_neg_magnitude"],
        })

    return {
        "ticker": ticker,
        "name": ASSETS[ticker]["name"],
        "price": live_prices.get(ticker, 0),
        "summary": s,
        "volatility": vol,
        "velocity": vel,
        "backtest": {
            "dir_acc": bt.get("dir_acc", 0),
            "hc_acc": bt.get("hc_acc", 0),
            "hc_count": bt.get("hc_count", 0),
            "mae": bt.get("mae", 0),
            "total": bt.get("total", 0),
        } if bt else None,
        "predictions": preds_out,
        "recent_windows": recent_windows,
    }


# ─── Auth Redirects ──────────────────────────────────────────────────
# All auth is handled by the gateway. These just redirect.


@app.get("/login")
async def login_page():
    return RedirectResponse("https://narve.ai/login", status_code=302)


@app.get("/signup")
async def signup_page():
    return RedirectResponse("https://narve.ai/signup", status_code=302)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("https://narve.ai/logout", status_code=302)
    resp.delete_cookie("session")
    return resp


# ─── REST Endpoints ──────────────────────────────────────────────────

_FAVICON_PATH = Path(__file__).parent / "favicon.png"


@app.get("/favicon.png")
async def favicon_png():
    if _FAVICON_PATH.exists():
        return FileResponse(str(_FAVICON_PATH), media_type="image/png")
    return Response(status_code=404)


@app.get("/favicon.ico")
async def favicon_ico():
    # Browsers auto-request /favicon.ico — serve the PNG so it still works.
    if _FAVICON_PATH.exists():
        return FileResponse(str(_FAVICON_PATH), media_type="image/png")
    return Response(status_code=404)


@app.get("/")
async def root(request: Request):
    """Serve the live crypto dashboard."""
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    if not asset_state:
        return HTMLResponse("<html><body style='background:#0d1117;color:#e6edf3;font-family:system-ui'><h1>Loading... refresh in 30s</h1></body></html>")
    # Generate and serve the dashboard with live JS injected
    all_results = {}
    for ticker in asset_state:
        st = asset_state[ticker]
        # Generate chart data from raw kline data
        raw_data = st.get("data", [])
        chart_24h = []
        chart_7d = []
        if raw_data:
            now_ms = raw_data[-1][0]
            day_ms = 24 * 3600 * 1000
            week_ms = 7 * day_ms
            for ts, price in raw_data:
                if ts >= now_ms - day_ms:
                    chart_24h.append({"t": ts // 1000, "v": round(price, 2)})
            if len(chart_24h) > 3000:
                step = len(chart_24h) // 2880
                chart_24h = chart_24h[::step]
            for ts, price in raw_data:
                if ts >= now_ms - week_ms:
                    chart_7d.append({"t": ts // 1000, "v": round(price, 2)})
            if len(chart_7d) > 2016:
                step = len(chart_7d) // 2016
                chart_7d = chart_7d[::step]
        all_results[ticker] = {
            "windows": st["windows"],
            "summary": st["summary"],
            "volatility": st["volatility"],
            "velocity": st["velocity"],
            "backtest": st["backtest"],
            "predictions": st["predictions"],
            "model_info": st.get("model_info"),
            "start_dt": st["start_dt"],
            "end_dt": st["end_dt"],
            "chart_24h": chart_24h,
            "chart_7d": chart_7d,
        }
    html = generate_dashboard(all_results)

    # Inject nav bar with user info
    user = _get_session_user(request)
    user_name = html_mod.escape((user.get("display_name") or user.get("email", "")) if user else "")
    tier_label = html_mod.escape(user.get("tier", "free").upper() if user else "FREE")
    tier_color = "var(--green)" if tier_label in ("PREMIUM","ADMIN") else "var(--muted)"
    has_creds = db.has_clob_credentials(user["id"]) if user else False
    wallet_indicator = (
        '<span style="color:#a371f7;font-size:0.7em;" title="Polymarket wallet connected">&#9679; WALLET</span>'
        if has_creds else
        '<a href="/settings#polymarket" style="color:var(--yellow);font-size:0.7em;text-decoration:none;" title="Connect Polymarket wallet">&#9888; CONNECT WALLET</a>'
    )
    nav_html = f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding:8px 0;border-bottom:1px solid var(--border);">
  <div style="display:flex;gap:12px;align-items:center;font-size:0.8em;">
    <span style="color:var(--muted);">{user_name}</span>
    <span style="color:{tier_color};font-weight:600;">{tier_label}</span>
    {wallet_indicator}
  </div>
  <div style="display:flex;gap:12px;align-items:center;font-size:0.8em;">
    <button onclick="dashTradePrompt()" style="background:#a371f7;color:#000;border:none;padding:5px 12px;border-radius:6px;font-weight:700;cursor:pointer;font-size:0.85em;">Trade Polymarket</button>
    <a href="/kalshi" style="color:var(--muted);text-decoration:none;">Kalshi</a>
    <a href="/trade" style="color:var(--blue);text-decoration:none;font-weight:600;">All Markets</a>
    <a href="/polybot" style="color:var(--muted);text-decoration:none;">Bot</a>
    <a href="/accuracy" style="color:var(--muted);text-decoration:none;">Accuracy</a>
    <a href="/settings" style="color:var(--muted);text-decoration:none;">Settings</a>
    <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600;">Logout</a>
  </div>
</div>
"""
    html = html.replace("<body>", "<body>" + nav_html, 1)

    # Inject live-update WebSocket script + browser notifications
    ws_script = """
<script>
(function() {
  // Request notification permission
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  function notify(title, body, tag) {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(title, { body: body, icon: '/favicon.ico', tag: tag, renotify: true });
    }
  }

  // ── Incremental DOM update helpers ──

  function renderPredCard(p) {
    var isCur = p.is_current;
    var border = isCur ? 'var(--green)' : 'var(--border)';
    var label = isCur ? 'UPCOMING' : 'COMPLETED';
    var labelColor = isCur ? 'var(--green)' : 'var(--muted)';
    var dirClass = p.pred_direction === 'positive' ? 'positive' : 'negative';
    var prob = p.pred_prob_positive;
    var probStr = prob >= 0.5 ? (prob * 100).toFixed(0) + '%' : ((1 - prob) * 100).toFixed(0) + '%';
    var conf = (p.confidence * 100).toFixed(0) + '%';
    var delta = (p.pred_end_delta >= 0 ? '+' : '') + p.pred_end_delta.toFixed(2);
    var timeStr = p.window_start ? p.window_start.replace(/T/, ' ').substring(11, 16) : '\\u2014';

    var actualStr = '';
    if (p.actual_end_delta !== null && p.actual_end_delta !== undefined) {
      var ac = p.actual_end_delta;
      var acClass = ac >= 0 ? 'positive' : 'negative';
      var acStr = (ac >= 0 ? '+' : '') + ac.toFixed(2);
      actualStr = '<div class="detail">Actual: <span class="' + acClass + '">$' + acStr + '</span></div>';
    }

    return '<div class="pred-card" style="border-color:' + border + ';">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
        '<span style="font-size:1.1em;font-weight:700;">' + timeStr + ' UTC</span>' +
        '<span style="font-size:0.7em;color:' + labelColor + ';font-weight:600;letter-spacing:0.05em;">' + label + '</span>' +
      '</div>' +
      '<div style="display:flex;gap:16px;flex-wrap:wrap;">' +
        '<div><span class="mini-label">Direction</span><br><span class="value-sm ' + dirClass + '">' + p.pred_direction.toUpperCase() + '</span></div>' +
        '<div><span class="mini-label">Delta</span><br><span class="value-sm">$' + delta + '</span></div>' +
        '<div><span class="mini-label">Prob</span><br><span class="value-sm">' + probStr + '</span></div>' +
        '<div><span class="mini-label">Confidence</span><br><span class="value-sm">' + conf + '</span></div>' +
      '</div>' +
      actualStr +
    '</div>';
  }

  function updatePredictions(ticker, predictions) {
    var grid = document.getElementById('pred-grid-' + ticker);
    if (!grid || !predictions) return;
    if (predictions.length === 0) {
      grid.innerHTML = '<div class="pred-card" style="border-color:var(--yellow);"><div style="padding:12px;text-align:center;color:var(--yellow);font-weight:600;">Models training on GPU... predictions will appear shortly.</div></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < predictions.length; i++) {
      html += renderPredCard(predictions[i]);
    }
    grid.innerHTML = html;
  }

  function updateWindowRows(ticker, windows) {
    // Update the window table if the details element is open
    var tab = document.getElementById('tab-' + ticker);
    if (!tab) return;
    var tbody = tab.querySelector('details tbody');
    if (!tbody) return;
    // Only update if details is open (user is viewing)
    var details = tab.querySelector('details');
    if (!details || !details.open) return;

    var html = '';
    for (var i = 0; i < windows.length; i++) {
      var w = windows[i];
      var ec = w.end_delta >= 0 ? 'positive' : 'negative';
      var crossS = w.last_cross_sec ? (w.last_cross_sec.toFixed(0) + 's\\u2192' + (w.last_cross_direction || '').substring(0, 3)) : '\\u2014';
      var rsiClass = w.rsi > 70 ? 'negative' : (w.rsi < 30 ? 'positive' : '');
      var startStr = w.start ? w.start.substring(5, 16).replace('T', ' ') : '';
      html += '<tr>' +
        '<td>' + startStr + '</td>' +
        '<td>$' + w.baseline.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) + '</td>' +
        '<td class="' + ec + '">$' + (w.end_delta >= 0 ? '+' : '') + w.end_delta.toFixed(2) + '</td>' +
        '<td class="positive">$+' + w.max_positive.toFixed(2) + '</td>' +
        '<td class="negative">$' + w.max_negative.toFixed(2) + '</td>' +
        '<td>$' + (w.avg_pos_magnitude >= 0 ? '+' : '') + w.avg_pos_magnitude.toFixed(2) + ' / $' + w.avg_neg_magnitude.toFixed(2) + '</td>' +
        '<td>' + crossS + '</td>' +
        '<td class="' + rsiClass + '">' + w.rsi.toFixed(0) + '</td>' +
        '<td>' + w.crossings + '</td>' +
      '</tr>';
    }
    tbody.innerHTML = html;
  }

  function applyAssetUpdate(ticker, data) {
    // Update live price
    var priceEl = document.getElementById('live-price-' + ticker);
    if (priceEl && data.price) {
      priceEl.textContent = '$' + data.price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
    }
    // Update predictions
    if (data.predictions) {
      updatePredictions(ticker, data.predictions);
    }
    // Update recent window rows
    if (data.recent_windows) {
      updateWindowRows(ticker, data.recent_windows);
    }
    // Update timestamp
    var ts = document.getElementById('last-update');
    if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
  }

  // ── WebSocket with auto-reconnect (no page reload) ──

  var wsReconnectDelay = 1000;
  var wsMaxDelay = 30000;

  function connectWS() {
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws = new WebSocket(proto + '//' + window.location.host + '/ws');

    ws.onopen = function() {
      wsReconnectDelay = 1000;  // reset on successful connect
      var ts = document.getElementById('last-update');
      if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
    };

    ws.onmessage = function(e) {
      var msg = JSON.parse(e.data);

      if (msg.type === 'price_update') {
        for (var ticker in msg.data) {
          var d = msg.data[ticker];
          var el = document.getElementById('live-price-' + ticker);
          if (el) el.textContent = '$' + d.price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
        }
        var ts = document.getElementById('last-update');
        if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
      }

      if (msg.type === 'window_update') {
        // Incremental update: apply new data to DOM without page reload
        if (msg.ticker && msg.data) {
          applyAssetUpdate(msg.ticker, msg.data);
          showToast(msg.ticker + ': new window data received');
        }
      }

      if (msg.type === 'init') {
        // Apply initial state from WS connection
        if (msg.data) {
          for (var t in msg.data) {
            applyAssetUpdate(t, msg.data[t]);
          }
        }
      }

      if (msg.type === 'alert') {
        var a = msg.data;
        notify('CryptoEdge Alert: ' + a.ticker,
               a.direction.toUpperCase() + ' signal (' + a.confidence + '% confidence) | Delta: $' + a.delta,
               'signal-' + a.ticker);
        showToast(a.ticker + ': ' + a.direction.toUpperCase() + ' (' + a.confidence + '% conf)');
      }
    };

    ws.onclose = function() {
      // Reconnect with exponential backoff instead of full page reload
      var ts = document.getElementById('last-update');
      if (ts) ts.textContent = 'Reconnecting...';
      setTimeout(function() {
        connectWS();
      }, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 2, wsMaxDelay);
    };

    ws.onerror = function() {
      ws.close();
    };
  }

  connectWS();

  // ── Periodic data refresh via fetch (no page reload) ──

  setInterval(function() {
    fetch('/api/state', { credentials: 'same-origin' })
      .then(function(r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function(data) {
        if (!data) return;
        for (var ticker in data) {
          applyAssetUpdate(ticker, data[ticker]);
        }
      })
      .catch(function() {
        // Silently ignore fetch errors; WS is the primary channel
      });
  }, 60000);

  // In-page toast notifications
  function showToast(msg) {
    var toast = document.createElement('div');
    toast.style.cssText = 'position:fixed;top:16px;right:16px;background:#1c2333;border:1px solid #58a6ff;' +
      'color:#e6edf3;padding:12px 20px;border-radius:8px;font-size:0.9em;z-index:9999;animation:fadeIn 0.3s;' +
      'box-shadow:0 4px 12px rgba(0,0,0,0.4);max-width:400px;';
    toast.textContent = '\\u26A1 ' + msg;
    document.body.appendChild(toast);
    setTimeout(function() { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.5s'; }, 4000);
    setTimeout(function() { toast.remove(); }, 4500);
  }
})();
</script>
"""
    html = html.replace("</body>", ws_script + "</body>")

    # Inject Polymarket trade widget — adds openTradeWidget/openTradeWidgetSearch
    # globals plus a per-ticker trade button injector
    trade_widget = _trade_widget_html(has_creds)
    crypto_names = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
        "DOGE": "Dogecoin", "XRP": "XRP", "BNB": "BNB",
    }
    crypto_names_js = json.dumps(crypto_names)
    dash_trade_script = f"""
<script>
(function() {{
  const COIN_NAMES = {crypto_names_js};

  // Global helper used by the nav button
  window.dashTradePrompt = function() {{
    const q = prompt('Search Polymarket markets:', 'Bitcoin');
    if (q) openTradeWidgetSearch(q);
  }};

  // Inject a "Trade on Polymarket" button next to each crypto's live price
  function injectTradeButtons() {{
    document.querySelectorAll('[id^="live-price-"]').forEach(function(el) {{
      if (el.dataset.tradeBtn === '1') return;
      const ticker = el.id.replace('live-price-', '').toUpperCase();
      const name = COIN_NAMES[ticker] || ticker;
      const btn = document.createElement('button');
      btn.textContent = 'Trade ' + ticker + ' \u2192';
      btn.style.cssText = 'background:#a371f7;color:#000;border:none;padding:6px 14px;border-radius:6px;font-weight:700;cursor:pointer;font-size:0.78em;margin-left:10px;';
      btn.title = 'Find a Polymarket market for ' + name + ' and trade it';
      btn.onclick = function() {{ openTradeWidgetSearch(name); }};
      el.parentNode.insertBefore(btn, el.nextSibling);
      el.dataset.tradeBtn = '1';
    }});
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', injectTradeButtons);
  }} else {{
    injectTradeButtons();
  }}
  // Re-inject if tabs are toggled or DOM is updated
  setInterval(injectTradeButtons, 2000);
}})();
</script>
"""
    html = html.replace("</body>", trade_widget + dash_trade_script + "</body>")

    return HTMLResponse(html)


# ─── Internal data endpoints (used by dashboard JS only, not public API) ───

def _get_bot_signals():
    """Compute trading signals for internal use by trading bots on localhost."""
    signals = {}
    for ticker in asset_state:
        st = asset_state[ticker]
        vel = st.get("velocity", {})
        vol = st.get("volatility", {})
        windows = st.get("windows", [])
        last_window = windows[-1] if windows else None

        recent_wins = windows[-200:] if len(windows) >= 200 else windows
        pos_windows = [w for w in recent_wins if w["end_delta"] >= 0]
        neg_windows = [w for w in recent_wins if w["end_delta"] < 0]

        avg_pos_delta = float(np.mean([w["end_delta"] for w in pos_windows])) if pos_windows else 0
        avg_neg_delta = float(np.mean([w["end_delta"] for w in neg_windows])) if neg_windows else 0
        avg_max_up = float(np.mean([w["max_positive"] for w in recent_wins])) if recent_wins else 0
        avg_max_down = float(np.mean([w["max_negative"] for w in recent_wins])) if recent_wins else 0
        win_rate = len(pos_windows) / len(recent_wins) * 100 if recent_wins else 50
        avg_rsi_when_up = float(np.mean([w["rsi"] for w in pos_windows])) if pos_windows else 50
        avg_rsi_when_down = float(np.mean([w["rsi"] for w in neg_windows])) if neg_windows else 50
        avg_crossings_winners = float(np.mean([w["crossings"] for w in pos_windows])) if pos_windows else 3
        avg_crossings_losers = float(np.mean([w["crossings"] for w in neg_windows])) if neg_windows else 3

        signal = {
            "ticker": ticker, "price": live_prices.get(ticker, 0),
            "volatility_label": vol.get("label", "UNKNOWN"),
            "gain_loss_ratio": vel.get("gain_loss_ratio", 0),
            "momentum_decay": vel.get("momentum_decay_ratio", 1),
            "avg_velocity_after_cross_pos": vel.get("avg_velocity_after_cross_pos", 0),
            "avg_velocity_after_cross_neg": vel.get("avg_velocity_after_cross_neg", 0),
            "best_entry_sec": vel.get("best_entry_sec", 0),
            "pct_seconds_gaining": vel.get("pct_seconds_gaining", 50),
            "avg_time_to_peak": vel.get("avg_time_to_peak_sec", 150),
            "avg_time_to_trough": vel.get("avg_time_to_trough_sec", 150),
            "avg_gain_per_sec": vel.get("avg_gain_per_sec", 0),
            "avg_loss_per_sec": vel.get("avg_loss_per_sec", 0),
            "hist_avg_pos_delta": avg_pos_delta, "hist_avg_neg_delta": avg_neg_delta,
            "hist_avg_max_up": avg_max_up, "hist_avg_max_down": avg_max_down,
            "hist_win_rate": win_rate,
            "hist_avg_rsi_when_up": avg_rsi_when_up, "hist_avg_rsi_when_down": avg_rsi_when_down,
            "hist_avg_crossings_winners": avg_crossings_winners, "hist_avg_crossings_losers": avg_crossings_losers,
        }
        if last_window:
            signal.update({
                "last_cross_sec": last_window["last_cross_sec"],
                "last_cross_direction": last_window["last_cross_direction"],
                "rsi": last_window["rsi"], "crossings": last_window["crossings"],
                "current_delta": last_window["end_delta"],
                "current_max_up": last_window["max_positive"],
                "current_max_down": last_window["max_negative"],
                "current_avg_delta": last_window["avg_delta"],
                "current_positive_pct": last_window["positive_pct"],
                "window_baseline": last_window["baseline"],
            })
        signals[ticker] = signal
    return signals


# ─── FX rates proxy (frankfurter.dev) ─────────────────────────────────
_fx_cache: dict = {"data": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "base": "USD",
    "date": "fallback",
    "rates": {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
        "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
        "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
        "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
        "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
        "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
    },
}


@app.get("/api/fx-rates")
async def get_fx_rates():
    """USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    cached = _fx_cache["data"]
    if cached and (now - _fx_cache["fetched_at"]) < _FX_TTL:
        return cached
    try:
        def _fetch():
            r = requests.get(
                "https://api.frankfurter.dev/v1/latest?base=USD",
                timeout=5,
                headers={"User-Agent": "narve-crypto/1.0"},
            )
            return r.json() if r.status_code == 200 else None
        data = await asyncio.to_thread(_fetch)
        if data:
            data.setdefault("rates", {})
            data["rates"]["USD"] = 1.0
            _fx_cache["data"] = data
            _fx_cache["fetched_at"] = now
            return data
    except Exception as e:
        print(f"FX rate fetch failed: {e}")
    if cached:
        return cached
    return _FX_FALLBACK


@app.get("/api/state")
async def api_state(request: Request):
    """Return current asset state for incremental dashboard refresh (no page reload)."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {ticker: serialize_asset(ticker) for ticker in asset_state}


@app.get("/_internal/bot/signals")
async def internal_bot_signals(request: Request):
    """Localhost-only signals endpoint for trading bots."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return _get_bot_signals()


# ─── Bot Dashboard ────────────────────────────────────────────────────

@app.get("/_internal/bot/status")
async def get_bot_status(request: Request):
    """Internal: bot state for the bot dashboard page. Auth checked via session."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trade_file = Path(__file__).parent / "trades.json"
    log_file = Path(__file__).parent / "bot_activity.log"
    result = {"running": False, "balance": 0, "total_pnl": 0, "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "peak_balance": 0, "max_drawdown": 0, "consecutive_losses": 0, "trades": [], "log": [], "positions": []}
    if trade_file.exists():
        try:
            with open(trade_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return result
        result["running"] = True
        result["balance"] = data.get("balance", 0)
        result["total_trades"] = data.get("total_trades", 0)
        result["winning_trades"] = data.get("winning_trades", 0)
        result["losing_trades"] = data.get("losing_trades", 0)
        result["total_pnl"] = data.get("total_pnl", 0)
        result["peak_balance"] = data.get("peak_balance", 0)
        result["max_drawdown"] = data.get("max_drawdown", 0)
        result["consecutive_losses"] = data.get("consecutive_losses", 0)
        result["positions"] = data.get("positions", [])
        result["trades"] = data.get("closed_trades", [])[-50:]  # last 50
    if log_file.exists():
        with open(log_file) as f:
            lines = f.readlines()
        result["log"] = [l.strip() for l in lines[-100:]]  # last 100 lines
    return result


@app.get("/bot", response_class=HTMLResponse)
async def bot_dashboard(request: Request):
    """Self-contained bot monitoring dashboard."""
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Monitor</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a1a; color:#e0e0e0; font-family:'SF Mono',Monaco,monospace; padding:16px; }
  h1 { color:#00d4aa; font-size:1.5em; margin-bottom:8px; }
  .subtitle { color:#888; font-size:0.8em; margin-bottom:16px; }
  .live-dot { display:inline-block; width:8px; height:8px; background:#00ff88; border-radius:50%;
    animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#141428; border:1px solid #2a2a4a; border-radius:10px; padding:14px; }
  .card .label { color:#888; font-size:0.7em; text-transform:uppercase; }
  .card .value { font-size:1.4em; font-weight:700; margin-top:4px; }
  .positive { color:#00ff88; }
  .negative { color:#ff4466; }
  .neutral { color:#ffaa00; }
  .section { margin-bottom:20px; }
  .section h2 { color:#aaa; font-size:0.9em; margin-bottom:8px; border-bottom:1px solid #2a2a4a; padding-bottom:4px; }
  table { width:100%; border-collapse:collapse; font-size:0.8em; }
  th { background:#1a1a2e; color:#888; text-align:left; padding:8px; }
  td { padding:8px; border-bottom:1px solid #1a1a2e; }
  .log-box { background:#0d0d1a; border:1px solid #2a2a4a; border-radius:8px; padding:12px;
    max-height:400px; overflow-y:auto; font-size:0.75em; line-height:1.6; }
  .log-line { border-bottom:1px solid #111; padding:2px 0; }
  .log-line.trade { color:#00d4aa; font-weight:600; }
  .log-line.loss { color:#ff4466; }
  .log-line.warn { color:#ffaa00; }
  .positions-empty { color:#666; font-style:italic; padding:12px; }
  @media(max-width:600px) { .grid { grid-template-columns:1fr 1fr; } }
</style>
</head><body>
<h1>Trading Bot Monitor</h1>
<p class="subtitle"><span class="live-dot"></span> <span id="status">Loading...</span> &middot; Auto-refresh 5s</p>

<div class="grid" id="stats"></div>

<div class="section">
  <h2>Open Positions</h2>
  <div id="positions"><p class="positions-empty">No open positions</p></div>
</div>

<div class="section">
  <h2>Recent Trades</h2>
  <div id="trades"></div>
</div>

<div class="section">
  <h2>Activity Log</h2>
  <div class="log-box" id="log"></div>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/_internal/bot/status');
    const d = await r.json();

    const wr = d.total_trades > 0 ? (d.winning_trades / d.total_trades * 100).toFixed(1) : '0.0';
    const pf = d.losing_trades > 0 && d.winning_trades > 0
      ? (d.trades.filter(t=>t.pnl>0).reduce((s,t)=>s+t.pnl,0) /
         Math.abs(d.trades.filter(t=>t.pnl<=0).reduce((s,t)=>s+t.pnl,0))).toFixed(2)
      : '∞';
    const dd = d.peak_balance > 0 ? ((d.peak_balance - d.balance) / d.peak_balance * 100).toFixed(2) : '0.00';

    document.getElementById('status').textContent = d.running
      ? 'Running · Balance: $' + d.balance.toLocaleString(undefined,{minimumFractionDigits:2})
      : 'Bot offline';

    document.getElementById('stats').innerHTML = `
      <div class="card"><div class="label">Balance</div><div class="value">$${d.balance.toLocaleString(undefined,{minimumFractionDigits:2})}</div></div>
      <div class="card"><div class="label">Total PnL</div><div class="value ${d.total_pnl>=0?'positive':'negative'}">$${d.total_pnl>=0?'+':''}${d.total_pnl.toFixed(2)}</div></div>
      <div class="card"><div class="label">Trades</div><div class="value">${d.total_trades}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value ${parseFloat(wr)>=50?'positive':'negative'}">${wr}%</div></div>
      <div class="card"><div class="label">W / L</div><div class="value"><span class="positive">${d.winning_trades}</span> / <span class="negative">${d.losing_trades}</span></div></div>
      <div class="card"><div class="label">Profit Factor</div><div class="value">${pf}</div></div>
      <div class="card"><div class="label">Drawdown</div><div class="value ${parseFloat(dd)>3?'negative':'neutral'}">${dd}%</div></div>
      <div class="card"><div class="label">Consec Losses</div><div class="value ${d.consecutive_losses>=3?'negative':''}">${d.consecutive_losses}</div></div>
    `;

    // Positions
    if (d.positions && d.positions.length > 0) {
      let ph = '<table><tr><th>Asset</th><th>Dir</th><th>Entry</th><th>Size</th><th>Stop</th><th>Score</th></tr>';
      d.positions.forEach(p => {
        ph += '<tr><td>'+p.ticker+'</td><td>'+p.direction.toUpperCase()+'</td><td>$'+parseFloat(p.entry_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>$'+parseFloat(p.bet_amount).toFixed(2)+'</td><td>$'+parseFloat(p.trailing_stop_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>'+p.score+'</td></tr>';
      });
      ph += '</table>';
      document.getElementById('positions').innerHTML = ph;
    } else {
      document.getElementById('positions').innerHTML = '<p class="positions-empty">No open positions</p>';
    }

    // Trades (newest first)
    const trades = (d.trades || []).reverse().slice(0, 20);
    if (trades.length > 0) {
      let th = '<table><tr><th>Asset</th><th>Dir</th><th>PnL</th><th>%</th><th>Entry</th><th>Exit</th><th>Reason</th></tr>';
      trades.forEach(t => {
        const cls = t.pnl >= 0 ? 'positive' : 'negative';
        th += '<tr><td>'+t.ticker+'</td><td>'+t.direction.toUpperCase()+'</td><td class="'+cls+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td><td class="'+cls+'">'+(t.pnl_pct>=0?'+':'')+t.pnl_pct.toFixed(2)+'%</td><td>$'+parseFloat(t.entry_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>$'+parseFloat(t.exit_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>'+t.exit_reason+'</td></tr>';
      });
      th += '</table>';
      document.getElementById('trades').innerHTML = th;
    } else {
      document.getElementById('trades').innerHTML = '<p class="positions-empty">No trades yet</p>';
    }

    // Log (newest first)
    function escapeHtml(s) {
      const d = document.createElement('div');
      d.appendChild(document.createTextNode(s));
      return d.innerHTML;
    }
    const lines = (d.log || []).reverse();
    document.getElementById('log').innerHTML = lines.map(l => {
      let cls = 'log-line';
      if (l.includes('OPEN') || l.includes('WIN')) cls += ' trade';
      if (l.includes('LOSS')) cls += ' loss';
      if (l.includes('COOLDOWN') || l.includes('paused')) cls += ' warn';
      return '<div class="'+cls+'">'+escapeHtml(l)+'</div>';
    }).join('');
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Polymarket Bot Dashboard ─────────────────────────────────────────

@app.get("/_internal/polybot/status")
async def get_polybot_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trade_file = Path(__file__).parent / "poly_trades.json"
    log_file = Path(__file__).parent / "poly_bot_activity.log"
    result = {"running": False, "balance": 0, "total_pnl": 0, "total_trades": 0,
              "wins": 0, "losses": 0, "peak_balance": 0, "pending": None,
              "trades": [], "log": []}
    if trade_file.exists():
        try:
            with open(trade_file) as f:
                data = json.load(f)
            result["running"] = True
            result["balance"] = data.get("balance", 0)
            result["total_trades"] = data.get("total_trades", 0)
            result["wins"] = data.get("wins", 0)
            result["losses"] = data.get("losses", 0)
            result["total_pnl"] = data.get("total_pnl", 0)
            result["peak_balance"] = data.get("peak_balance", 0)
            result["pending"] = data.get("pending")
            result["trades"] = data.get("trades", [])[-50:]
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            print(f"  [BOT STATUS] Error reading state: {e}")
    if log_file.exists():
        try:
            with open(log_file) as f:
                result["log"] = [l.strip() for l in f.readlines()[-100:]]
        except OSError as e:
            print(f"  [BOT STATUS] Error reading log: {e}")
    return result


@app.get("/polybot", response_class=HTMLResponse)
async def polybot_dashboard(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    user = _get_session_user(request)
    has_creds = db.has_clob_credentials(user["id"]) if user else False
    trade_widget = _trade_widget_html(has_creds)
    creds_banner = "" if has_creds else (
        '<div style="background:#1a1a2e;border:1px solid #d29922;color:#d29922;'
        'padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:0.78em;">'
        'Connect your Polymarket wallet on <a href="/settings#polymarket" style="color:#58a6ff;">Settings</a> '
        'to trade these markets one-click from this page.</div>'
    )
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Multi-Coin Bot</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a1a; color:#e0e0e0; font-family:'SF Mono',Monaco,monospace; padding:16px; }
  h1 { color:#f7931a; font-size:1.5em; margin-bottom:8px; }
  .nav { display:flex; gap:16px; font-size:0.8em; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #2a2a4a; flex-wrap:wrap; }
  .nav a { color:#8b949e; text-decoration:none; }
  .nav a.active { color:#f7931a; font-weight:600; }
  .nav a:hover { color:#e6edf3; }
  .subtitle { color:#888; font-size:0.8em; margin-bottom:16px; }
  .live-dot { display:inline-block; width:8px; height:8px; background:#f7931a; border-radius:50%; animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#141428; border:1px solid #2a2a4a; border-radius:10px; padding:14px; }
  .card .label { color:#888; font-size:0.7em; text-transform:uppercase; }
  .card .value { font-size:1.3em; font-weight:700; margin-top:4px; }
  .positive { color:#00ff88; }
  .negative { color:#ff4466; }
  .pending-box { background:#1a1a2e; border:2px solid #f7931a; border-radius:10px; padding:16px; margin-bottom:20px; }
  .pending-box h3 { color:#f7931a; margin-bottom:8px; }
  .pending-box .actions { margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }
  .section { margin-bottom:20px; }
  .section h2 { color:#aaa; font-size:0.9em; margin-bottom:8px; border-bottom:1px solid #2a2a4a; padding-bottom:4px; }
  table { width:100%; border-collapse:collapse; font-size:0.8em; }
  th { background:#1a1a2e; color:#888; text-align:left; padding:8px; }
  td { padding:8px; border-bottom:1px solid #1a1a2e; }
  .log-box { background:#0d0d1a; border:1px solid #2a2a4a; border-radius:8px; padding:12px; max-height:400px; overflow-y:auto; font-size:0.75em; line-height:1.6; }
  .log-line { border-bottom:1px solid #111; padding:2px 0; }
  .log-line.win { color:#00ff88; font-weight:600; }
  .log-line.loss { color:#ff4466; }
  .log-line.bet { color:#f7931a; }
  .empty { color:#666; font-style:italic; padding:12px; }
</style>
</head><body>
<div class="nav">
  <a href="/">Dashboard</a>
  <a href="/kalshi">Kalshi</a>
  <a href="/trade">Trade</a>
  <a href="/polybot" class="active">Polymarket Bot</a>
  <a href="/settings">Settings</a>
</div>
<h1>Polymarket Multi-Coin 5-Min Bot</h1>
<p class="subtitle"><span class="live-dot"></span> <span id="status">Loading...</span> &middot; $100 per trade &middot; BTC ETH SOL DOGE XRP BNB &middot; Auto-refresh 5s</p>
__CREDS_BANNER__
<div class="grid" id="stats"></div>
<div id="pending"></div>
<div class="section"><h2>Recent Trades</h2><div id="trades"></div></div>
<div class="section"><h2>Activity Log</h2><div class="log-box" id="log"></div></div>
<script>
function escAttr(s) {
  return String(s||'').replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'").replace(/"/g,'&quot;');
}
function tradeBtn(query, label) {
  return '<button class="trade-btn poly" onclick="openTradeWidgetSearch(\\''+escAttr(query)+'\\')">'+(label||'Trade on Polymarket')+'</button>';
}
async function refresh() {
  try {
    const r = await fetch('/_internal/polybot/status');
    const d = await r.json();
    const wr = d.total_trades > 0 ? (d.wins/d.total_trades*100).toFixed(1) : '0.0';
    const avgWin = d.trades.filter(t=>t.pnl>0);
    const avgLoss = d.trades.filter(t=>t.pnl<=0);
    const avgW = avgWin.length > 0 ? (avgWin.reduce((s,t)=>s+t.pnl,0)/avgWin.length).toFixed(2) : '0';
    const avgL = avgLoss.length > 0 ? (avgLoss.reduce((s,t)=>s+t.pnl,0)/avgLoss.length).toFixed(2) : '0';

    // pending can be a dict of coins or a single object (old format)
    const pending = d.pending || {};
    const pendingEntries = (typeof pending === 'object' && !pending.side)
      ? Object.entries(pending).filter(([k,v]) => v !== null)
      : (pending && pending.side ? [['btc', pending]] : []);
    const liveBets = pendingEntries.length;

    document.getElementById('status').textContent = d.running
      ? (liveBets > 0 ? liveBets + ' LIVE BET' + (liveBets>1?'S':'') + ' — waiting for resolution' : 'Scanning for edge...')
      : 'Bot offline';
    document.getElementById('stats').innerHTML = `
      <div class="card"><div class="label">Balance</div><div class="value">$${(d.balance||0).toLocaleString(undefined,{minimumFractionDigits:2})}</div></div>
      <div class="card"><div class="label">Total PnL</div><div class="value ${(d.total_pnl||0)>=0?'positive':'negative'}">$${(d.total_pnl||0)>=0?'+':''}${(d.total_pnl||0).toFixed(2)}</div></div>
      <div class="card"><div class="label">Trades</div><div class="value">${d.total_trades||0}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value ${parseFloat(wr)>=50?'positive':'negative'}">${wr}%</div></div>
      <div class="card"><div class="label">W / L</div><div class="value"><span class="positive">${d.wins||0}</span> / <span class="negative">${d.losses||0}</span></div></div>
      <div class="card"><div class="label">Avg Win</div><div class="value positive">$${avgW}</div></div>
      <div class="card"><div class="label">Avg Loss</div><div class="value negative">$${avgL}</div></div>
      <div class="card"><div class="label">Active Bets</div><div class="value" style="color:#f7931a">${liveBets} / 6</div></div>
    `;
    if (liveBets > 0) {
      let ph = '';
      pendingEntries.forEach(([coin, p]) => {
        const potWin = (p.shares * 1.0 - p.amount).toFixed(2);
        const titleEsc = escAttr(p.title || (coin.toUpperCase()+' price'));
        const condId = p.condition_id || p.market_id || '';
        let tradeAction;
        if (condId) {
          tradeAction = '<button class="trade-btn poly" onclick="openTradeWidget(\\''+escAttr(condId)+'\\')">Trade this market</button>';
        } else {
          tradeAction = '<button class="trade-btn poly" onclick="openTradeWidgetSearch(\\''+titleEsc+'\\')">Find &amp; trade on Polymarket</button>';
        }
        ph += `<div class="pending-box">
          <h3>LIVE BET — ${coin.toUpperCase()}</h3>
          <p><strong>${p.side.toUpperCase()}</strong> @ $${p.buy_price.toFixed(3)} | ${p.shares.toFixed(1)} shares | Edge: ${(p.edge*100).toFixed(1)}%</p>
          <p>Potential: <span class="positive">+$${potWin}</span> / <span class="negative">-$${p.amount}</span></p>
          <p style="color:#888;font-size:0.8em">${p.title||''}</p>
          <div class="actions">${tradeAction}</div>
        </div>`;
      });
      document.getElementById('pending').innerHTML = ph;
    } else {
      document.getElementById('pending').innerHTML = '';
    }
    const trades = (d.trades||[]).reverse().slice(0,30);
    if (trades.length > 0) {
      let h = '<table><tr><th>Coin</th><th>Side</th><th>Price</th><th>Edge</th><th>Result</th><th>PnL</th><th>Trade</th></tr>';
      trades.forEach(t => {
        const cls = t.pnl >= 0 ? 'positive' : 'negative';
        const coin = (t.coin || 'btc').toUpperCase();
        const searchQ = (t.title || (coin + ' price')).slice(0,60);
        const btn = tradeBtn(searchQ, 'Trade');
        h += '<tr><td>'+coin+'</td><td>'+t.side.toUpperCase()+'</td><td>$'+t.buy_price.toFixed(3)+'</td><td>'+(t.edge*100).toFixed(1)+'%</td><td class="'+cls+'">'+t.result+'</td><td class="'+cls+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td><td>'+btn+'</td></tr>';
      });
      h += '</table>';
      document.getElementById('trades').innerHTML = h;
    } else {
      document.getElementById('trades').innerHTML = '<p class="empty">No trades yet. Bot is waiting for mispriced markets.</p>';
    }
    function escapeHtml(s) {
      const d = document.createElement('div');
      d.appendChild(document.createTextNode(s));
      return d.innerHTML;
    }
    const lines = (d.log||[]).reverse();
    document.getElementById('log').innerHTML = lines.map(l => {
      let cls = 'log-line';
      if (l.includes('WIN')) cls += ' win';
      if (l.includes('LOSS')) cls += ' loss';
      if (l.includes('BET')) cls += ' bet';
      return '<div class="'+cls+'">'+escapeHtml(l)+'</div>';
    }).join('');
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
__TRADE_WIDGET__
</body></html>"""
    html = html.replace("__CREDS_BANNER__", creds_banner).replace("__TRADE_WIDGET__", trade_widget)
    return HTMLResponse(html)


# ─── Arbitrage Dashboard ──────────────────────────────────────────────

@app.get("/_internal/arbitrage/status")
async def get_arbitrage_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    signals_file = Path(__file__).parent / "signals.json"
    result = {"running": False, "total_signals": 0, "signals": [], "last_scan": ""}
    if signals_file.exists():
        try:
            with open(signals_file) as f:
                signals = json.load(f)
            result["running"] = True
            result["total_signals"] = len(signals)
            result["signals"] = signals[-100:]  # last 100
            if signals:
                result["last_scan"] = signals[-1].get("timestamp", "")
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            print(f"  [SIGNALS] Error reading signals: {e}")
    return result


@app.get("/arbitrage")
async def arbitrage_dashboard(request: Request):
    """Redirect to standalone Sports Dashboard on port 8888."""
    return RedirectResponse("/", status_code=302)


# ─── Weather Dashboard ────────────────────────────────────────────────

@app.get("/_internal/weather/status")
async def get_weather_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db_path = Path(__file__).parent.parent / "polymarket_weather_bot" / "trades.db"
    result = {"running": False, "signals": [], "trades": [], "total_signals": 0, "total_trades": 0}
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                result["running"] = True
                # Recent signals
                rows = conn.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 50").fetchall()
                result["signals"] = [dict(r) for r in rows]
                result["total_signals"] = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                # Recent trades
                try:
                    rows = conn.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 50").fetchall()
                    result["trades"] = [dict(r) for r in rows]
                    result["total_trades"] = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                except Exception:
                    pass
            finally:
                conn.close()
        except Exception as e:
            result["error"] = str(e)
    return result


@app.get("/weather")
async def weather_dashboard(request: Request):
    """Redirect to standalone Weather Dashboard on port 5050."""
    return RedirectResponse("http://localhost:5050", status_code=302)


# ═══════════════════════════════════════════════════════════════════════
# SHARED TRADE WIDGET — Embeddable Polymarket trading modal for any page
# ═══════════════════════════════════════════════════════════════════════

def _trade_widget_html(has_creds: bool) -> str:
    """Return the HTML/CSS/JS for an embeddable Polymarket quick-trade modal.

    Any page that includes this can call:
      openTradeWidget(conditionId)          — open by condition ID
      openTradeWidgetSearch(query)          — search + show first result

    Simplified UX: shows YES/NO prices + amount field + 4 direct action buttons
    (BUY YES, BUY NO, SELL YES, SELL NO). One click = one order. No confirm dialog.
    """
    template = """
<!-- ── Quick Trade Widget CSS ── -->
<style>
  .qt-overlay { display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.78);z-index:5000;justify-content:center;align-items:flex-start;padding:60px 12px;overflow-y:auto; }
  .qt-overlay.open { display:flex; }
  .qt-modal { background:#0d1117;border:1px solid #30363d;border-radius:14px;width:100%;max-width:440px;box-shadow:0 12px 48px rgba(0,0,0,0.6); }
  .qt-header { display:flex;justify-content:space-between;align-items:flex-start;padding:16px 18px 12px;border-bottom:1px solid #21262d;gap:10px; }
  .qt-header h2 { font-size:0.95em;flex:1;line-height:1.35;color:#e6edf3;font-weight:600; }
  .qt-meta { font-size:0.65em;color:#8b949e;margin-top:4px; }
  .qt-close { background:none;border:none;color:#8b949e;font-size:1.5em;cursor:pointer;padding:0 4px;line-height:1; }
  .qt-close:hover { color:#e6edf3; }
  .qt-body { padding:16px 18px 18px; }

  /* Big YES/NO price tiles */
  .qt-prices { display:flex;gap:10px;margin-bottom:14px; }
  .qt-tile { flex:1;text-align:center;padding:12px 8px;border-radius:10px;border:1px solid; }
  .qt-tile.yes { background:rgba(63,185,80,0.08);border-color:rgba(63,185,80,0.3); }
  .qt-tile.no  { background:rgba(248,81,73,0.08);border-color:rgba(248,81,73,0.3); }
  .qt-tile .lbl { font-size:0.65em;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px; }
  .qt-tile .val { font-size:1.6em;font-weight:800;margin-top:2px; }
  .qt-tile.yes .val { color:#3fb950; }
  .qt-tile.no  .val { color:#f85149; }

  /* Amount input + presets */
  .qt-amt-row { display:flex;align-items:center;gap:8px;margin-bottom:8px; }
  .qt-amt-row label { font-size:0.7em;color:#8b949e;text-transform:uppercase;flex-shrink:0; }
  .qt-amt-row input { flex:1;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:9px 10px;border-radius:8px;font-size:1em;font-weight:700;text-align:right; }
  .qt-amt-row input:focus { outline:none;border-color:#58a6ff; }
  .qt-amt-row .unit { color:#8b949e;font-size:0.78em;font-weight:600; }
  .qt-presets { display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap; }
  .qt-presets button { flex:1;min-width:50px;background:#161b22;border:1px solid #30363d;color:#8b949e;padding:6px 4px;border-radius:6px;font-size:0.74em;cursor:pointer;font-weight:600; }
  .qt-presets button:hover { border-color:#58a6ff;color:#e6edf3; }

  /* Action buttons grid */
  .qt-actions { display:grid;grid-template-columns:1fr 1fr;gap:8px; }
  .qt-btn { padding:12px 8px;border:none;border-radius:10px;font-weight:800;cursor:pointer;font-size:0.85em;transition:all 0.15s;letter-spacing:0.3px; }
  .qt-btn:hover:not(:disabled) { transform:translateY(-1px);filter:brightness(1.1); }
  .qt-btn:disabled { opacity:0.4;cursor:not-allowed; }
  .qt-btn.buy-yes  { background:#3fb950;color:#000; }
  .qt-btn.buy-no   { background:#f85149;color:#fff; }
  .qt-btn.sell-yes { background:rgba(63,185,80,0.18);color:#3fb950;border:1px solid #3fb950; }
  .qt-btn.sell-no  { background:rgba(248,81,73,0.18);color:#f85149;border:1px solid #f85149; }

  .qt-foot { margin-top:12px;padding-top:10px;border-top:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;font-size:0.7em;color:#8b949e; }
  .qt-foot a { color:#58a6ff;text-decoration:none; }
  .qt-foot a:hover { text-decoration:underline; }
  .qt-connect { background:rgba(210,153,34,0.12);border:1px solid rgba(210,153,34,0.4);border-radius:8px;padding:10px;margin-bottom:12px;font-size:0.78em;color:#d29922;text-align:center; }
  .qt-connect a { color:#d29922;font-weight:700;text-decoration:underline; }

  /* Inline trade button (used by callers across all dashboards) */
  .trade-btn { background:none;border:1px solid #58a6ff;color:#58a6ff;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.75em;font-weight:600;transition:all 0.15s;white-space:nowrap; }
  .trade-btn:hover { background:#58a6ff;color:#000; }
  .trade-btn.poly { border-color:#a371f7;color:#a371f7; }
  .trade-btn.poly:hover { background:#a371f7;color:#000; }

  /* Toast (shared with Kalshi widget) */
  #tw-toasts { position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none; }
  .tw-toast { pointer-events:auto;background:#161b22;border:1px solid #3fb950;border-radius:8px;padding:12px 16px;max-width:360px;box-shadow:0 4px 24px rgba(0,0,0,0.5);animation:twSlide 0.4s ease-out;position:relative; }
  .tw-toast.err { border-color:#f85149; }
  .tw-toast .tw-tt { font-size:0.82em;font-weight:700;margin-bottom:3px;color:#e6edf3; }
  .tw-toast .tw-tb { font-size:0.72em;color:#8b949e; }
  .tw-toast .tw-tx { position:absolute;top:6px;right:10px;background:none;border:none;color:#8b949e;cursor:pointer;font-size:1em; }
  @keyframes twSlide { from{opacity:0;transform:translateX(40px)} to{opacity:1;transform:translateX(0)} }
</style>

<!-- ── Quick Trade Widget HTML ── -->
<div class="qt-overlay" id="qt-overlay">
  <div class="qt-modal">
    <div class="qt-header">
      <div style="flex:1;">
        <h2 id="qt-title">Loading...</h2>
        <div class="qt-meta" id="qt-meta"></div>
      </div>
      <button class="qt-close" onclick="closeTW()">&times;</button>
    </div>
    <div class="qt-body">
      <div class="qt-prices">
        <div class="qt-tile yes"><div class="lbl">YES</div><div class="val" id="qt-yes">&mdash;</div></div>
        <div class="qt-tile no"><div class="lbl">NO</div><div class="val" id="qt-no">&mdash;</div></div>
      </div>
      <div id="qt-connect-block" style="display:__CONNECT_DISPLAY__;">
        <div class="qt-connect">
          Polymarket wallet not connected.<br>
          <a href="/settings#polymarket">Connect in Settings &rarr;</a>
        </div>
      </div>
      <div class="qt-amt-row">
        <label>Amount</label>
        <input type="number" id="qt-amt" placeholder="10" min="1" step="1" value="10">
        <span class="unit">USDC</span>
      </div>
      <div class="qt-presets">
        <button onclick="qtSet(5)">$5</button>
        <button onclick="qtSet(10)">$10</button>
        <button onclick="qtSet(25)">$25</button>
        <button onclick="qtSet(50)">$50</button>
        <button onclick="qtSet(100)">$100</button>
      </div>
      <div class="qt-actions">
        <button class="qt-btn buy-yes"  id="qt-buy-yes"  onclick="qtFire('buy','yes')"  __DISABLED__>BUY YES</button>
        <button class="qt-btn buy-no"   id="qt-buy-no"   onclick="qtFire('buy','no')"   __DISABLED__>BUY NO</button>
        <button class="qt-btn sell-yes" id="qt-sell-yes" onclick="qtFire('sell','yes')" __DISABLED__>SELL YES</button>
        <button class="qt-btn sell-no"  id="qt-sell-no"  onclick="qtFire('sell','no')"  __DISABLED__>SELL NO</button>
      </div>
      <div class="qt-foot">
        <span id="qt-vol">&mdash;</span>
        <a href="#" id="qt-poly" target="_blank" rel="noopener">View on Polymarket &#8599;</a>
      </div>
    </div>
  </div>
</div>
<div id="tw-toasts"></div>

<!-- ── Quick Trade Widget JS ── -->
<script>
(function(){
  let _tw={};
  const hasCreds=__HAS_CREDS__;

  function esc(s){ const d=document.createElement('div');d.textContent=s||'';return d.innerHTML; }
  function twToast(t,b,err){
    const c=document.getElementById('tw-toasts'),e=document.createElement('div');
    e.className='tw-toast'+(err?' err':'');e.style.position='relative';
    e.innerHTML='<button class="tw-tx" onclick="this.parentElement.remove()">&times;</button><div class="tw-tt">'+esc(t)+'</div><div class="tw-tb">'+esc(b)+'</div>';
    c.appendChild(e);setTimeout(()=>e.remove(),7000);
  }

  window.openTradeWidget=async function(conditionId){
    const ov=document.getElementById('qt-overlay');ov.classList.add('open');
    document.getElementById('qt-title').textContent='Loading...';
    document.getElementById('qt-yes').textContent='—';
    document.getElementById('qt-no').textContent='—';
    document.getElementById('qt-meta').textContent='';
    document.getElementById('qt-vol').textContent='—';
    let m=null;
    try{const r=await fetch('/api/clob/market/'+conditionId);if(r.ok) m=await r.json();}catch(e){}
    if(!m){
      try{const r=await fetch('/api/clob/markets?q=&limit=50');if(r.ok){const ms=await r.json();m=ms.find(x=>(x.conditionId||x.condition_id)===conditionId);}}catch(e){}
    }
    if(!m){document.getElementById('qt-title').textContent='Market not found';return;}
    let yT=null,nT=null;
    const tks=m.tokens||[];
    if(tks.length>=2){yT=tks.find(t=>(t.outcome||'').toLowerCase()==='yes')||tks[0];nT=tks.find(t=>(t.outcome||'').toLowerCase()==='no')||tks[1];}
    else if(m.clobTokenIds){const ids=typeof m.clobTokenIds==='string'?JSON.parse(m.clobTokenIds):m.clobTokenIds;yT={token_id:ids[0],outcome:'Yes'};nT={token_id:ids[1],outcome:'No'};}
    let yPrice=0.5,nPrice=0.5;
    const op=m.outcomePrices?(typeof m.outcomePrices==='string'?JSON.parse(m.outcomePrices):m.outcomePrices):null;
    if(op&&op.length>=2){yPrice=parseFloat(op[0]);nPrice=parseFloat(op[1]);}
    _tw={market:m,yesToken:yT,noToken:nT,conditionId:conditionId,yesPrice:yPrice,noPrice:nPrice};
    document.getElementById('qt-title').textContent=m.question||m.title||'?';
    document.getElementById('qt-yes').textContent=(yPrice*100).toFixed(0)+'¢';
    document.getElementById('qt-no').textContent=(nPrice*100).toFixed(0)+'¢';
    const vol=m.volume?'Vol $'+Number(m.volume).toLocaleString():'';
    const liq=m.liquidity?'Liq $'+Number(m.liquidity).toLocaleString():'';
    document.getElementById('qt-vol').textContent=[vol,liq].filter(Boolean).join(' • ')||'—';
    const slug=m.slug||conditionId;
    document.getElementById('qt-poly').href='https://polymarket.com/event/'+slug;
  };

  window.openTradeWidgetSearch=async function(query){
    const ov=document.getElementById('qt-overlay');ov.classList.add('open');
    document.getElementById('qt-title').textContent='Searching: '+query+'...';
    document.getElementById('qt-yes').textContent='—';
    document.getElementById('qt-no').textContent='—';
    document.getElementById('qt-meta').textContent='';
    document.getElementById('qt-vol').textContent='—';
    try{
      const r=await fetch('/api/clob/markets?q='+encodeURIComponent(query)+'&limit=5');
      if(r.ok){const ms=await r.json();if(ms.length>0){const cid=ms[0].conditionId||ms[0].condition_id;openTradeWidget(cid);return;}}
    }catch(e){}
    document.getElementById('qt-title').textContent='No matching Polymarket found for: '+query;
  };

  window.closeTW=function(){document.getElementById('qt-overlay').classList.remove('open');_tw={};};
  document.getElementById('qt-overlay').addEventListener('click',function(e){if(e.target===this)closeTW();});

  window.qtSet=function(v){document.getElementById('qt-amt').value=v;};

  window.qtFire=async function(side,outcome){
    if(!hasCreds){twToast('Wallet Not Connected','Open Settings to add your Polymarket API keys.',true);
      setTimeout(function(){window.location.href='/settings#polymarket';},1200);return;}
    if(!_tw.market){twToast('No market loaded','Try reopening the popup',true);return;}
    const amt=parseFloat(document.getElementById('qt-amt').value)||0;
    if(amt<=0){twToast('Invalid amount','Enter an amount > 0',true);return;}
    const tk=outcome==='yes'?_tw.yesToken:_tw.noToken;
    const tokenId=tk?(tk.token_id||tk):'';
    if(!tokenId){twToast('Token unavailable','Could not resolve '+outcome.toUpperCase()+' token',true);return;}
    // For SELL, the API expects shares (size). Convert USDC -> shares using displayed price.
    const px=outcome==='yes'?_tw.yesPrice:_tw.noPrice;
    let payload={token_id:tokenId,condition_id:_tw.conditionId,market_question:_tw.market.question||'',outcome:outcome.toUpperCase(),side:side,order_type:'market'};
    if(side==='buy'){payload.amount=amt;}
    else{const shares=px>0?Math.floor(amt/px):0;if(shares<=0){twToast('Invalid','Amount too small for current price',true);return;}payload.size=shares;payload.amount=0;payload.price=0;}
    // Disable all buttons while submitting
    const btns=['qt-buy-yes','qt-buy-no','qt-sell-yes','qt-sell-no'].map(function(id){return document.getElementById(id);});
    btns.forEach(function(b){b.disabled=true;});
    const fired=document.getElementById('qt-'+side+'-'+outcome);
    const orig=fired.textContent;fired.textContent='...';
    try{
      const r=await fetch('/api/clob/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const d=await r.json();
      if(r.ok&&!d.error){twToast('Order Placed',side.toUpperCase()+' '+outcome.toUpperCase()+' • $'+amt.toFixed(2),false);}
      else twToast('Order Failed',d.error||'Unknown error',true);
    }catch(e){twToast('Error',e.message,true);}
    btns.forEach(function(b){b.disabled=false;});
    fired.textContent=orig;
  };
})();
</script>"""
    return (template
        .replace("__HAS_CREDS__", "true" if has_creds else "false")
        .replace("__DISABLED__", "" if has_creds else "disabled")
        .replace("__CONNECT_DISPLAY__", "none" if has_creds else "block"))


def _kalshi_widget_html(has_kalshi_creds: bool) -> str:
    """Return the HTML/CSS/JS for an embeddable Kalshi quick-trade modal.

    Any page that includes this can call:
      openKalshiWidget(ticker, title, yesPrice, noPrice)

    Prices can be 0-1 floats or 0-100 cents — both are normalized to cents.
    Simplified UX: shows YES/NO prices + contracts field + 4 direct action buttons
    (BUY YES, BUY NO, SELL YES, SELL NO). One click = one market order.
    """
    template = """
<!-- ── Kalshi Quick Widget CSS ── -->
<style>
  .kq-overlay { display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.78);z-index:5100;justify-content:center;align-items:flex-start;padding:60px 12px;overflow-y:auto; }
  .kq-overlay.open { display:flex; }
  .kq-modal { background:#0d1117;border:1px solid #30363d;border-radius:14px;width:100%;max-width:440px;box-shadow:0 12px 48px rgba(0,0,0,0.6); }
  .kq-header { display:flex;justify-content:space-between;align-items:flex-start;padding:16px 18px 12px;border-bottom:1px solid #21262d;gap:10px; }
  .kq-header h2 { font-size:0.95em;flex:1;line-height:1.35;color:#e6edf3;font-weight:600; }
  .kq-tkr { font-size:0.65em;color:#00b4d8;font-family:monospace;margin-top:4px;letter-spacing:0.5px; }
  .kq-close { background:none;border:none;color:#8b949e;font-size:1.5em;cursor:pointer;padding:0 4px;line-height:1; }
  .kq-close:hover { color:#e6edf3; }
  .kq-body { padding:16px 18px 18px; }

  .kq-prices { display:flex;gap:10px;margin-bottom:14px; }
  .kq-tile { flex:1;text-align:center;padding:12px 8px;border-radius:10px;border:1px solid; }
  .kq-tile.yes { background:rgba(63,185,80,0.08);border-color:rgba(63,185,80,0.3); }
  .kq-tile.no  { background:rgba(248,81,73,0.08);border-color:rgba(248,81,73,0.3); }
  .kq-tile .lbl { font-size:0.65em;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px; }
  .kq-tile .val { font-size:1.6em;font-weight:800;margin-top:2px; }
  .kq-tile.yes .val { color:#3fb950; }
  .kq-tile.no  .val { color:#f85149; }

  .kq-amt-row { display:flex;align-items:center;gap:8px;margin-bottom:8px; }
  .kq-amt-row label { font-size:0.7em;color:#8b949e;text-transform:uppercase;flex-shrink:0; }
  .kq-amt-row input { flex:1;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:9px 10px;border-radius:8px;font-size:1em;font-weight:700;text-align:right; }
  .kq-amt-row input:focus { outline:none;border-color:#58a6ff; }
  .kq-amt-row .unit { color:#8b949e;font-size:0.78em;font-weight:600; }
  .kq-presets { display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap; }
  .kq-presets button { flex:1;min-width:50px;background:#161b22;border:1px solid #30363d;color:#8b949e;padding:6px 4px;border-radius:6px;font-size:0.74em;cursor:pointer;font-weight:600; }
  .kq-presets button:hover { border-color:#00b4d8;color:#e6edf3; }

  .kq-actions { display:grid;grid-template-columns:1fr 1fr;gap:8px; }
  .kq-btn { padding:12px 8px;border:none;border-radius:10px;font-weight:800;cursor:pointer;font-size:0.85em;transition:all 0.15s;letter-spacing:0.3px; }
  .kq-btn:hover:not(:disabled) { transform:translateY(-1px);filter:brightness(1.1); }
  .kq-btn:disabled { opacity:0.4;cursor:not-allowed; }
  .kq-btn.buy-yes  { background:#3fb950;color:#000; }
  .kq-btn.buy-no   { background:#f85149;color:#fff; }
  .kq-btn.sell-yes { background:rgba(63,185,80,0.18);color:#3fb950;border:1px solid #3fb950; }
  .kq-btn.sell-no  { background:rgba(248,81,73,0.18);color:#f85149;border:1px solid #f85149; }

  .kq-foot { margin-top:12px;padding-top:10px;border-top:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;font-size:0.7em;color:#8b949e; }
  .kq-foot a { color:#00b4d8;text-decoration:none; }
  .kq-foot a:hover { text-decoration:underline; }
  .kq-connect { background:rgba(210,153,34,0.12);border:1px solid rgba(210,153,34,0.4);border-radius:8px;padding:10px;margin-bottom:12px;font-size:0.78em;color:#d29922;text-align:center; }
  .kq-connect a { color:#d29922;font-weight:700;text-decoration:underline; }

  .trade-btn.kalshi { border-color:#00b4d8;color:#00b4d8; }
  .trade-btn.kalshi:hover { background:#00b4d8;color:#000; }
</style>

<!-- ── Kalshi Quick Widget HTML ── -->
<div class="kq-overlay" id="kq-overlay">
  <div class="kq-modal">
    <div class="kq-header">
      <div style="flex:1;">
        <h2 id="kq-title">Loading...</h2>
        <div class="kq-tkr" id="kq-ticker"></div>
      </div>
      <button class="kq-close" onclick="closeKW()">&times;</button>
    </div>
    <div class="kq-body">
      <div class="kq-prices">
        <div class="kq-tile yes"><div class="lbl">YES</div><div class="val" id="kq-yes">&mdash;</div></div>
        <div class="kq-tile no"><div class="lbl">NO</div><div class="val" id="kq-no">&mdash;</div></div>
      </div>
      <div id="kq-connect-block" style="display:__CONNECT_DISPLAY__;">
        <div class="kq-connect">
          Kalshi account not connected.<br>
          <a href="/settings#kalshi">Connect in Settings &rarr;</a>
        </div>
      </div>
      <div class="kq-amt-row">
        <label>Contracts</label>
        <input type="number" id="kq-count" placeholder="10" min="1" step="1" value="10">
        <span class="unit">qty</span>
      </div>
      <div class="kq-presets">
        <button onclick="kqSet(1)">1</button>
        <button onclick="kqSet(5)">5</button>
        <button onclick="kqSet(10)">10</button>
        <button onclick="kqSet(25)">25</button>
        <button onclick="kqSet(100)">100</button>
      </div>
      <div class="kq-actions">
        <button class="kq-btn buy-yes"  id="kq-buy-yes"  onclick="kqFire('buy','yes')"  __DISABLED__>BUY YES</button>
        <button class="kq-btn buy-no"   id="kq-buy-no"   onclick="kqFire('buy','no')"   __DISABLED__>BUY NO</button>
        <button class="kq-btn sell-yes" id="kq-sell-yes" onclick="kqFire('sell','yes')" __DISABLED__>SELL YES</button>
        <button class="kq-btn sell-no"  id="kq-sell-no"  onclick="kqFire('sell','no')"  __DISABLED__>SELL NO</button>
      </div>
      <div class="kq-foot">
        <span id="kq-cost">Cost: &mdash;</span>
        <a href="#" id="kq-link" target="_blank" rel="noopener">View on Kalshi &#8599;</a>
      </div>
    </div>
  </div>
</div>

<!-- ── Kalshi Quick Widget JS ── -->
<script>
(function(){
  let _kq={};
  const hasKCreds=__HAS_CREDS__;

  function esc(s){ const d=document.createElement('div');d.textContent=s||'';return d.innerHTML; }
  function kqToast(t,b,err){
    // Reuse Polymarket widget's toast container if present; else fallback to alert
    const host=document.getElementById('tw-toasts');
    if(!host){ alert(t+': '+b); return; }
    const e=document.createElement('div');
    e.className='tw-toast'+(err?' err':'');e.style.position='relative';
    e.innerHTML='<button class="tw-tx" onclick="this.parentElement.remove()">&times;</button><div class="tw-tt">'+esc(t)+'</div><div class="tw-tb">'+esc(b)+'</div>';
    host.appendChild(e);setTimeout(function(){e.remove();},7000);
  }

  // Prices come in as 0-1 floats (probability) OR 0-100 cents. Normalize to cents.
  function toCents(p){
    if(p==null||p==='') return null;
    const n=parseFloat(p);
    if(isNaN(n)) return null;
    return n<=1 ? Math.round(n*100) : Math.round(n);
  }

  function recalcCost(){
    const c=parseInt(document.getElementById('kq-count').value)||0;
    const yC=_kq.yesCents||0;
    const nC=_kq.noCents||0;
    const yCost=((yC*c)/100).toFixed(2);
    const nCost=((nC*c)/100).toFixed(2);
    document.getElementById('kq-cost').textContent='YES $'+yCost+' • NO $'+nCost;
  }

  window.openKalshiWidget=function(ticker, title, yesPrice, noPrice){
    const ov=document.getElementById('kq-overlay');ov.classList.add('open');
    document.getElementById('kq-title').textContent=title||ticker||'?';
    document.getElementById('kq-ticker').textContent=ticker||'';
    const yC=toCents(yesPrice);
    const nC=toCents(noPrice) != null ? toCents(noPrice) : (yC!=null ? (100-yC) : null);
    _kq={ticker:ticker,title:title,yesCents:yC,noCents:nC};
    document.getElementById('kq-yes').textContent=(yC!=null ? yC : '—')+(yC!=null?'\u00A2':'');
    document.getElementById('kq-no').textContent =(nC!=null ? nC : '—')+(nC!=null?'\u00A2':'');
    const link=document.getElementById('kq-link');
    if(ticker) link.href='https://kalshi.com/markets/'+ticker.toLowerCase().split('-')[0]+'/'+ticker;
    recalcCost();
  };

  window.closeKW=function(){document.getElementById('kq-overlay').classList.remove('open');_kq={};};
  document.getElementById('kq-overlay').addEventListener('click',function(e){if(e.target===this)closeKW();});
  document.getElementById('kq-count').addEventListener('input',recalcCost);

  window.kqSet=function(v){document.getElementById('kq-count').value=v;recalcCost();};

  window.kqFire=async function(action,side){
    if(!hasKCreds){
      kqToast('Kalshi Not Connected','Open Settings to add your Kalshi API key.',true);
      setTimeout(function(){window.location.href='/settings#kalshi';},1200);return;
    }
    if(!_kq.ticker){kqToast('No market','Ticker missing.',true);return;}
    const count=parseInt(document.getElementById('kq-count').value)||0;
    if(count<=0){kqToast('Invalid','Enter contracts > 0',true);return;}
    const payload={ticker:_kq.ticker,side:side,action:action,count:count,order_type:'market'};
    // Disable all buttons while submitting
    const btns=['kq-buy-yes','kq-buy-no','kq-sell-yes','kq-sell-no'].map(function(id){return document.getElementById(id);});
    btns.forEach(function(b){b.disabled=true;});
    const fired=document.getElementById('kq-'+action+'-'+side);
    const orig=fired.textContent;fired.textContent='...';
    try{
      const r=await fetch('/api/kalshi/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const d=await r.json();
      if(r.ok&&!d.error){kqToast('Kalshi Order Placed',action.toUpperCase()+' '+count+' x '+side.toUpperCase(),false);}
      else kqToast('Order Failed',typeof d.error==='string'?d.error:JSON.stringify(d.error||'Unknown'),true);
    }catch(e){kqToast('Error',e.message,true);}
    btns.forEach(function(b){b.disabled=false;});
    fired.textContent=orig;
  };
})();
</script>"""
    return (template
        .replace("__HAS_CREDS__", "true" if has_kalshi_creds else "false")
        .replace("__DISABLED__", "" if has_kalshi_creds else "disabled")
        .replace("__CONNECT_DISPLAY__", "none" if has_kalshi_creds else "block"))


# ─── Dashboard Hub ───────────────────────────────────────────────────

@app.get("/hub", response_class=HTMLResponse)
async def dashboard_hub(request: Request):
    """Central hub linking to all 4 dashboards on their dedicated ports."""
    host = html_mod.escape(request.headers.get("host", "localhost:8000").split(":")[0])
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard Hub</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0a0e17; color:#e1e5ee; font-family:'SF Mono','Fira Code',monospace; display:flex; justify-content:center; align-items:center; min-height:100vh; }}
  .hub {{ max-width:600px; width:100%; padding:40px; }}
  h1 {{ font-size:1.6rem; margin-bottom:8px; background:linear-gradient(90deg,#60a5fa,#a78bfa); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
  .subtitle {{ color:#64748b; font-size:0.85rem; margin-bottom:32px; }}
  .cards {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .card {{ background:#111827; border:1px solid #1e2940; border-radius:12px; padding:24px; text-decoration:none; color:#e1e5ee; transition:border-color 0.2s, transform 0.2s; }}
  .card:hover {{ border-color:#60a5fa; transform:translateY(-2px); }}
  .card h2 {{ font-size:1rem; margin-bottom:6px; }}
  .card .port {{ color:#64748b; font-size:0.75rem; margin-bottom:8px; }}
  .card .desc {{ color:#94a3b8; font-size:0.8rem; line-height:1.4; }}
  .card.crypto h2 {{ color:#f7931a; }}
  .card.stock h2 {{ color:#60a5fa; }}
  .card.weather h2 {{ color:#4da6ff; }}
  .card.sports h2 {{ color:#ffaa00; }}
</style>
</head><body>
<div class="hub">
  <h1>Polymarket Dashboards</h1>
  <p class="subtitle">4 dashboards, each on its own port</p>
  <div class="cards">
    <a href="http://{host}:8000" class="card crypto">
      <h2>Crypto Dashboard</h2>
      <div class="port">Port 8000</div>
      <div class="desc">BTC/ETH analysis, trading bot, and crypto signals</div>
    </a>
    <a href="http://{host}:8050" class="card stock">
      <h2>Stock Prediction</h2>
      <div class="port">Port 8050</div>
      <div class="desc">Stock prediction bot with P/L tracking</div>
    </a>
    <a href="http://{host}:5050" class="card weather">
      <h2>Weather Trading</h2>
      <div class="port">Port 5050</div>
      <div class="desc">Weather forecast vs Polymarket odds</div>
    </a>
    <a href="http://{host}:8888" class="card sports">
      <h2>Sports Betting</h2>
      <div class="port">Port 8888</div>
      <div class="desc">Bookmaker vs Polymarket odds comparison</div>
    </a>
  </div>
</div>
</body></html>"""
    return HTMLResponse(html)


# ─── Kalshi Markets Dashboard ────────────────────────────────────────

@app.get("/kalshi", response_class=HTMLResponse)
async def kalshi_dashboard(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    user = _get_session_user(request)
    has_creds = db.has_clob_credentials(user["id"])
    has_kalshi_creds = db.has_kalshi_credentials(user["id"])

    try:
        from kalshi_scanner import run_scanner as kalshi_scan
        data = await asyncio.to_thread(kalshi_scan)
    except Exception as e:
        data = {"total_markets": 0, "trending": [], "close_calls": [], "top_events": [], "categories": {}}

    # Build market rows — each row has a direct Kalshi trade button AND a "Find on Polymarket" button
    def _make_row(m: dict, show_24h: bool = False) -> str:
        title = html_mod.escape(m['title'][:70])
        ticker_raw = m.get('ticker', '')
        ticker = html_mod.escape(ticker_raw, quote=True)
        kalshi_url = f"https://kalshi.com/markets/{ticker_raw.lower().split('-')[0]}/{ticker_raw.lower()}" if ticker_raw else "https://kalshi.com"
        yes_cls = "positive" if m["yes_price"] >= 0.5 else "negative"
        vol_24h = f"<td>{m.get('volume_24h', 0):,}</td>" if show_24h else ""
        # Use json.dumps for JS string literals — handles backslashes, quotes,
        # </script> sequences, U+2028/U+2029, and embedded newlines correctly.
        # Then HTML-escape (with quote=True) so the resulting JS literal can be
        # safely interpolated into a double-quoted onclick attribute.
        def _js_attr(s: str) -> str:
            return html_mod.escape(json.dumps(s), quote=True)
        title_js = _js_attr(m['title'][:80])
        title_short_js = _js_attr(m['title'][:60])
        ticker_js = _js_attr(ticker_raw)
        yes_val = float(m['yes_price'])
        no_val = 1 - yes_val
        kalshi_btn = (
            f'<button class="trade-btn kalshi" '
            f'onclick="openKalshiWidget({ticker_js}, {title_js}, {yes_val:.4f}, {no_val:.4f})"'
            f'>Kalshi</button>'
        )
        poly_btn = (
            f'<button class="trade-btn poly" '
            f'onclick="openTradeWidgetSearch({title_short_js})"'
            f'>Polymarket</button>'
        )
        return f"""<tr>
          <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;">{title}</td>
          <td class="{yes_cls}" style="font-weight:700;">{yes_val:.0%}</td>
          <td>{no_val:.0%}</td>
          {vol_24h}
          <td>{m.get('volume',0):,}</td>
          <td style="color:var(--muted);font-size:0.75em;">{html_mod.escape(m.get('category',''))}</td>
          <td style="white-space:nowrap;">
            {kalshi_btn}
            {poly_btn}
            <a href="{kalshi_url}" target="_blank" class="trade-btn" style="text-decoration:none;display:inline-block;font-size:0.7em;opacity:0.7;">site&#8599;</a>
          </td>
        </tr>"""

    trending_rows = "".join(_make_row(m, show_24h=True) for m in (data.get("trending") or [])[:25])
    close_rows = "".join(_make_row(m, show_24h=False) for m in (data.get("close_calls") or [])[:20])

    cat_cards = ""
    for cat, info in list((data.get("categories") or {}).items())[:12]:
        cat_cards += f'<div class="card"><div class="label">{html_mod.escape(cat)}</div><div class="value">{info["count"]}</div><div class="detail">Vol: {info["total_volume"]:,}</div></div>'

    trade_widget = _trade_widget_html(has_creds)
    kalshi_widget = _kalshi_widget_html(has_kalshi_creds)

    # Status banner showing which accounts are connected
    poly_status = '<span style="color:var(--green);">&#9679; Polymarket</span>' if has_creds else '<a href="/settings#polymarket" style="color:var(--yellow);">&#9888; Connect Polymarket</a>'
    kalshi_status = '<span style="color:#00b4d8;">&#9679; Kalshi</span>' if has_kalshi_creds else '<a href="/settings#kalshi" style="color:var(--yellow);">&#9888; Connect Kalshi</a>'

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Kalshi Markets</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:8px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.3em;font-weight:700;margin-top:2px; }}
  .card .detail {{ color:var(--muted);font-size:0.7em;margin-top:2px; }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  .section {{ margin-bottom:24px; }}
  .section h2 {{ font-size:1em;color:var(--blue);margin-bottom:10px; }}
</style></head><body>
<div class="nav">
  <div class="nav-links">
    <a href="/">Dashboard</a>
    <a href="/kalshi" class="active">Kalshi</a>
    <a href="/trade">Trade</a>
    <a href="/polybot">Polymarket Bot</a>
    <a href="/settings">Settings</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Kalshi Prediction Markets</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:8px;">{data.get('total_markets',0):,} active markets &bull; Updated {datetime.now(timezone.utc).strftime('%H:%M UTC')}</p>
<p style="font-size:0.78em;margin-bottom:14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;">
  <span style="color:var(--muted);">Trading:</span>
  {kalshi_status}
  {poly_status}
</p>
<p style="color:var(--muted);font-size:0.75em;margin-bottom:16px;">
  Click <span class="trade-btn kalshi" style="display:inline-block;cursor:default;">Kalshi</span> to place a direct Kalshi order, or
  <span class="trade-btn poly" style="display:inline-block;cursor:default;">Polymarket</span> to find &amp; trade the equivalent Polymarket market.
</p>

<div class="section">
  <h2>Categories</h2>
  <div class="cards">{cat_cards}</div>
</div>

<div class="section">
  <h2>Trending (24h Volume)</h2>
  <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;">
    <table>
      <thead><tr><th>Market</th><th>Yes</th><th>No</th><th>24h Vol</th><th>Total Vol</th><th>Category</th><th>Trade</th></tr></thead>
      <tbody>{trending_rows}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>Close Calls (35-65% odds)</h2>
  <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;">
    <table>
      <thead><tr><th>Market</th><th>Yes</th><th>No</th><th>Volume</th><th>Category</th><th>Trade</th></tr></thead>
      <tbody>{close_rows}</tbody>
    </table>
  </div>
</div>

{trade_widget}
{kalshi_widget}

<script>setInterval(()=>location.reload(),300000);</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Trade Page (Polymarket) ─────────────────────────────────────────

@app.get("/trade", response_class=HTMLResponse)
async def trade_page(request: Request):
    """Polymarket CLOB trading page with order book, trading panel, and positions."""
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    user = _get_session_user(request)
    has_creds = db.has_clob_credentials(user["id"])

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Trade on Polymarket</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922;--purple:#a371f7; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:4px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}

  /* Tabs */
  .tabs {{ display:flex;gap:0;margin-bottom:16px;border-bottom:2px solid var(--border); }}
  .tab {{ padding:10px 20px;cursor:pointer;font-size:0.85em;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-2px;transition:all 0.2s;background:none;border-top:none;border-left:none;border-right:none; }}
  .tab:hover {{ color:var(--text); }}
  .tab.active {{ color:var(--blue);border-bottom-color:var(--blue);font-weight:600; }}
  .tab-content {{ display:none; }}
  .tab-content.active {{ display:block; }}

  /* Search */
  .search-bar {{ display:flex;gap:10px;margin-bottom:16px; }}
  .search-bar input {{ flex:1;background:var(--card);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:0.9em; }}
  .search-bar input::placeholder {{ color:var(--muted); }}
  .search-bar input:focus {{ outline:none;border-color:var(--blue); }}

  /* Market cards */
  .market-grid {{ display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px; }}
  .market-card {{ background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:all 0.2s; }}
  .market-card:hover {{ border-color:var(--blue);transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,0.3); }}
  .market-card .mc-question {{ font-size:0.9em;font-weight:600;margin-bottom:10px;line-height:1.3; }}
  .market-card .mc-outcomes {{ display:flex;gap:8px;margin-bottom:10px; }}
  .mc-outcome {{ flex:1;padding:8px;border-radius:6px;text-align:center;font-size:0.8em; }}
  .mc-outcome.yes {{ background:rgba(63,185,80,0.1);border:1px solid rgba(63,185,80,0.3); }}
  .mc-outcome.no {{ background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.3); }}
  .mc-outcome .price {{ font-size:1.3em;font-weight:700; }}
  .mc-outcome .label {{ font-size:0.7em;color:var(--muted);text-transform:uppercase; }}
  .market-card .mc-meta {{ display:flex;gap:12px;font-size:0.7em;color:var(--muted); }}
  .mc-fav {{ position:absolute;top:10px;right:12px;background:none;border:none;font-size:1.2em;cursor:pointer;color:var(--muted);transition:color 0.2s; }}
  .mc-fav.active {{ color:var(--yellow); }}
  .mc-fav:hover {{ color:var(--yellow); }}

  /* Trading modal */
  .modal-overlay {{ display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:1000;justify-content:center;align-items:flex-start;padding:40px 16px;overflow-y:auto; }}
  .modal-overlay.open {{ display:flex; }}
  .modal {{ background:var(--bg);border:1px solid var(--border);border-radius:12px;width:100%;max-width:800px;max-height:90vh;overflow-y:auto; }}
  .modal-header {{ display:flex;justify-content:space-between;align-items:flex-start;padding:20px;border-bottom:1px solid var(--border); }}
  .modal-header h2 {{ font-size:1.1em;flex:1;line-height:1.3; }}
  .modal-close {{ background:none;border:none;color:var(--muted);font-size:1.5em;cursor:pointer;padding:0 4px; }}
  .modal-close:hover {{ color:var(--text); }}
  .modal-body {{ padding:20px;display:grid;grid-template-columns:1fr 320px;gap:20px; }}
  @media (max-width:720px) {{ .modal-body {{ grid-template-columns:1fr; }} }}

  /* Order book */
  .ob-container {{ border:1px solid var(--border);border-radius:8px;overflow:hidden; }}
  .ob-header {{ background:var(--card);padding:8px 12px;font-size:0.75em;font-weight:600;color:var(--muted);text-transform:uppercase;display:flex;justify-content:space-between; }}
  .ob-side {{ max-height:200px;overflow-y:auto; }}
  .ob-row {{ display:flex;justify-content:space-between;padding:3px 12px;font-size:0.78em;position:relative; }}
  .ob-row .ob-fill {{ position:absolute;top:0;bottom:0;right:0;opacity:0.08; }}
  .ob-row.ask .ob-fill {{ background:var(--red); }}
  .ob-row.bid .ob-fill {{ background:var(--green); }}
  .ob-spread {{ text-align:center;padding:6px;font-size:0.72em;color:var(--muted);background:var(--card);border-top:1px solid var(--border);border-bottom:1px solid var(--border); }}

  /* Trade panel */
  .trade-panel {{ background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px; }}
  .tp-toggle {{ display:flex;gap:0;margin-bottom:14px;border-radius:6px;overflow:hidden;border:1px solid var(--border); }}
  .tp-toggle button {{ flex:1;padding:8px;background:var(--bg);border:none;color:var(--muted);font-weight:600;cursor:pointer;font-size:0.85em;transition:all 0.2s; }}
  .tp-toggle button.active-buy {{ background:var(--green);color:#000; }}
  .tp-toggle button.active-sell {{ background:var(--red);color:#fff; }}
  .tp-field {{ margin-bottom:12px; }}
  .tp-field label {{ display:block;font-size:0.72em;color:var(--muted);margin-bottom:4px;text-transform:uppercase; }}
  .tp-field input, .tp-field select {{ width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-size:0.9em; }}
  .tp-field input:focus, .tp-field select:focus {{ outline:none;border-color:var(--blue); }}
  .tp-estimate {{ background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:14px;font-size:0.8em; }}
  .tp-estimate .row {{ display:flex;justify-content:space-between;margin-bottom:4px; }}
  .tp-estimate .row:last-child {{ margin-bottom:0; }}
  .tp-submit {{ width:100%;padding:12px;border:none;border-radius:8px;font-size:0.95em;font-weight:700;cursor:pointer;transition:all 0.2s; }}
  .tp-submit.buy {{ background:var(--green);color:#000; }}
  .tp-submit.sell {{ background:var(--red);color:#fff; }}
  .tp-submit:hover {{ opacity:0.9;transform:translateY(-1px); }}
  .tp-submit:disabled {{ opacity:0.4;cursor:not-allowed;transform:none; }}

  /* Positions / history table */
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}

  /* Status bar */
  .status-bar {{ display:flex;gap:16px;align-items:center;margin-bottom:16px;padding:10px 16px;background:var(--card);border:1px solid var(--border);border-radius:8px;font-size:0.82em;flex-wrap:wrap; }}
  .sb-item {{ display:flex;align-items:center;gap:6px; }}
  .sb-dot {{ width:8px;height:8px;border-radius:50%;animation:pulse 1.5s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}

  /* Loading */
  .loading {{ text-align:center;padding:40px;color:var(--muted); }}
  .spinner {{ display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--blue);border-radius:50%;animation:spin 0.8s linear infinite; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}

  /* Warning banner */
  .warn-banner {{ background:rgba(210,153,34,0.1);border:1px solid rgba(210,153,34,0.3);border-radius:8px;padding:10px 16px;margin-bottom:16px;font-size:0.75em;color:var(--yellow); }}

  /* Confirm dialog */
  .confirm-overlay {{ display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);z-index:2000;justify-content:center;align-items:center; }}
  .confirm-overlay.open {{ display:flex; }}
  .confirm-box {{ background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:420px;width:90%; }}
  .confirm-box h3 {{ margin-bottom:12px;font-size:1.1em; }}
  .confirm-box .details {{ background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:16px;font-size:0.85em; }}
  .confirm-box .btn-row {{ display:flex;gap:10px;justify-content:flex-end; }}
  .confirm-box button {{ padding:8px 20px;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.85em; }}
  .confirm-box .btn-cancel {{ background:var(--border);color:var(--text); }}
  .confirm-box .btn-confirm {{ background:var(--green);color:#000; }}
  .confirm-box .btn-confirm.sell {{ background:var(--red);color:#fff; }}

  /* Toast */
  #toast-container {{ position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none; }}
  .toast {{ pointer-events:auto;background:var(--card);border:1px solid var(--green);border-radius:8px;padding:12px 16px;max-width:380px;box-shadow:0 4px 24px rgba(0,0,0,0.5);animation:slideIn 0.4s ease-out;position:relative; }}
  .toast.error {{ border-color:var(--red); }}
  .toast-title {{ font-size:0.85em;font-weight:700;margin-bottom:4px; }}
  .toast-body {{ font-size:0.75em;color:var(--muted); }}
  .toast-close {{ position:absolute;top:8px;right:12px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:1em; }}
  @keyframes slideIn {{ from{{opacity:0;transform:translateX(50px)}} to{{opacity:1;transform:translateX(0)}} }}
</style></head><body>

<div class="nav">
  <div class="nav-links">
    <a href="/">Dashboard</a>
    <a href="/kalshi">Kalshi</a>
    <a href="/trade" class="active">Trade</a>
    <a href="/accuracy">Accuracy</a>
    <a href="/settings">Settings</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Polymarket Trading</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:12px;">Direct CLOB integration &bull; Live order books &bull; Place trades</p>

<div class="warn-banner">
  &#9888; <strong>Not financial advice.</strong> Trading prediction markets involves real money and real risk. Never bet more than you can afford to lose.
</div>

<!-- Status Bar -->
<div class="status-bar">
  <div class="sb-item">
    <span class="sb-dot" style="background:{'var(--green)' if has_creds else 'var(--red)'}"></span>
    <span>{'Wallet Connected' if has_creds else '<a href="/settings#polymarket" style="color:var(--blue);">Connect Wallet</a>'}</span>
  </div>
  <div class="sb-item" id="sb-balance" style="display:{'flex' if has_creds else 'none'};">
    <span style="color:var(--muted);">Balance:</span>
    <span id="usdc-balance" style="font-weight:600;">Loading...</span>
  </div>
  <div class="sb-item" id="sb-open-orders" style="display:{'flex' if has_creds else 'none'};">
    <span style="color:var(--muted);">Open Orders:</span>
    <span id="open-orders-count" style="font-weight:600;">0</span>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <button class="tab active" onclick="switchTab('markets')">Markets</button>
  <button class="tab" onclick="switchTab('favorites')">Favorites</button>
  <button class="tab" onclick="switchTab('positions')">Open Orders</button>
  <button class="tab" onclick="switchTab('history')">Trade History</button>
  <button class="tab" onclick="switchTab('alerts')">Insider Alerts</button>
</div>

<!-- Markets Tab -->
<div id="tab-markets" class="tab-content active">
  <div class="search-bar">
    <input type="text" id="market-search" placeholder="Search markets... (e.g. Bitcoin, Trump, Fed, World Cup)" oninput="debounceSearch()">
  </div>
  <div id="market-grid" class="market-grid">
    <div class="loading"><span class="spinner"></span><br>Loading markets...</div>
  </div>
  <div style="text-align:center;margin-top:16px;">
    <button id="load-more-btn" onclick="loadMoreMarkets()" style="display:none;background:var(--card);border:1px solid var(--border);color:var(--blue);padding:8px 24px;border-radius:6px;cursor:pointer;font-size:0.85em;">Load More</button>
  </div>
</div>

<!-- Favorites Tab -->
<div id="tab-favorites" class="tab-content">
  <div id="favorites-grid" class="market-grid">
    <div class="loading">No favorites yet. Star markets to add them here.</div>
  </div>
</div>

<!-- Open Orders Tab -->
<div id="tab-positions" class="tab-content">
  <div id="positions-container">
    {'<div class="loading">Connect your wallet in <a href="/settings#polymarket" style="color:var(--blue);">Settings</a> to see open orders.</div>' if not has_creds else '<div class="loading"><span class="spinner"></span><br>Loading open orders...</div>'}
  </div>
</div>

<!-- Trade History Tab -->
<div id="tab-history" class="tab-content">
  <div id="history-container">
    <div class="loading"><span class="spinner"></span><br>Loading trade history...</div>
  </div>
</div>

<!-- Insider Alerts Tab -->
<div id="tab-alerts" class="tab-content">
  <p style="color:var(--muted);font-size:0.75em;margin-bottom:8px;">
    Scans news for insider trading reports, suspicious bets &amp; prediction market anomalies. Cross-referenced with live Polymarket data.
  </p>
  <div id="news-trade-alerts" style="border:1px solid var(--red);border-radius:8px;max-height:600px;overflow-y:auto;">
    <div style="padding:16px;color:var(--muted);text-align:center;">Loading alerts...</div>
  </div>
</div>

<!-- Trading Modal -->
<div class="modal-overlay" id="trade-modal">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-question">Loading...</h2>
      <button class="modal-close" onclick="closeTradeModal()">&times;</button>
    </div>
    <div class="modal-body">
      <!-- Left: Order Book -->
      <div>
        <div class="ob-container">
          <div class="ob-header"><span>PRICE</span><span>SIZE</span></div>
          <div id="ob-asks" class="ob-side"></div>
          <div id="ob-spread" class="ob-spread">Spread: —</div>
          <div id="ob-bids" class="ob-side"></div>
        </div>
        <div style="margin-top:12px;">
          <div style="display:flex;gap:8px;margin-bottom:8px;">
            <div class="mc-outcome yes" style="flex:1;padding:10px;">
              <div class="label">YES</div>
              <div class="price" id="modal-yes-price">—</div>
            </div>
            <div class="mc-outcome no" style="flex:1;padding:10px;">
              <div class="label">NO</div>
              <div class="price" id="modal-no-price">—</div>
            </div>
          </div>
          <div style="font-size:0.72em;color:var(--muted);" id="modal-meta"></div>
        </div>
      </div>

      <!-- Right: Trade Panel -->
      <div class="trade-panel">
        <div style="font-size:0.85em;font-weight:600;margin-bottom:10px;">Place Order</div>

        <!-- Outcome selector -->
        <div class="tp-field">
          <label>Outcome</label>
          <div class="tp-toggle" id="outcome-toggle">
            <button class="active-buy" onclick="setOutcome('yes')">YES</button>
            <button onclick="setOutcome('no')">NO</button>
          </div>
        </div>

        <!-- Buy/Sell toggle -->
        <div class="tp-field">
          <label>Side</label>
          <div class="tp-toggle" id="side-toggle">
            <button class="active-buy" onclick="setSide('buy')">BUY</button>
            <button onclick="setSide('sell')">SELL</button>
          </div>
        </div>

        <!-- Order type -->
        <div class="tp-field">
          <label>Order Type</label>
          <select id="order-type" onchange="updateEstimate()">
            <option value="market">Market Order</option>
            <option value="limit">Limit Order</option>
          </select>
        </div>

        <!-- Amount (market orders) -->
        <div class="tp-field" id="amount-field">
          <label>Amount (USDC)</label>
          <input type="number" id="order-amount" placeholder="0.00" min="1" step="1" oninput="updateEstimate()">
        </div>

        <!-- Price (limit orders) -->
        <div class="tp-field" id="price-field" style="display:none;">
          <label>Limit Price</label>
          <input type="number" id="order-price" placeholder="0.50" min="0.01" max="0.99" step="0.01" oninput="updateEstimate()">
        </div>

        <!-- Size (limit orders) -->
        <div class="tp-field" id="size-field" style="display:none;">
          <label>Shares</label>
          <input type="number" id="order-size" placeholder="0" min="1" step="1" oninput="updateEstimate()">
        </div>

        <!-- Estimate -->
        <div class="tp-estimate" id="order-estimate">
          <div class="row"><span style="color:var(--muted);">Est. Cost</span><span id="est-cost">$0.00</span></div>
          <div class="row"><span style="color:var(--muted);">Potential Payout</span><span id="est-payout" style="color:var(--green);">$0.00</span></div>
          <div class="row"><span style="color:var(--muted);">Potential Profit</span><span id="est-profit" style="color:var(--green);">$0.00</span></div>
        </div>

        <button class="tp-submit buy" id="submit-order-btn" onclick="submitOrder()" {'disabled' if not has_creds else ''}>
          {'Connect Wallet First' if not has_creds else 'Place Order'}
        </button>

        {'' if has_creds else '<p style="font-size:0.7em;color:var(--muted);margin-top:8px;text-align:center;">Go to <a href="/settings#polymarket" style="color:var(--blue);">Settings</a> to connect your Polymarket wallet.</p>'}
      </div>
    </div>
  </div>
</div>

<!-- Confirm Dialog -->
<div class="confirm-overlay" id="confirm-dialog">
  <div class="confirm-box">
    <h3>Confirm Order</h3>
    <div class="details" id="confirm-details"></div>
    <div style="font-size:0.72em;color:var(--yellow);margin-bottom:12px;">&#9888; This will execute a real trade with real USDC.</div>
    <div class="btn-row">
      <button class="btn-cancel" onclick="cancelConfirm()">Cancel</button>
      <button class="btn-confirm" id="confirm-btn" onclick="confirmOrder()">Confirm Trade</button>
    </div>
  </div>
</div>

<div id="toast-container"></div>

<script>
(function() {{
  // ── State ──
  let allMarkets = [];
  let offset = 0;
  let favorites = new Set();
  let currentModal = null;  // {{market, yesToken, noToken}}
  let tradeSide = 'buy';
  let tradeOutcome = 'yes';
  let pendingOrder = null;
  let searchTimeout = null;
  const hasCreds = {'true' if has_creds else 'false'};

  function esc(s) {{
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }}

  function toast(title, body, isError) {{
    const c = document.getElementById('toast-container');
    const t = document.createElement('div');
    t.className = 'toast' + (isError ? ' error' : '');
    t.style.position = 'relative';
    t.innerHTML = '<button class="toast-close" onclick="this.parentElement.remove()">&times;</button>'
      + '<div class="toast-title">' + esc(title) + '</div>'
      + '<div class="toast-body">' + esc(body) + '</div>';
    c.appendChild(t);
    setTimeout(() => t.remove(), 8000);
  }}

  // ── Tab switching ──
  window.switchTab = function(tab) {{
    document.querySelectorAll('.tab').forEach((t,i) => {{
      const tabs = ['markets','favorites','positions','history','alerts'];
      t.classList.toggle('active', tabs[i] === tab);
    }});
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById('tab-' + tab).classList.add('active');
    if (tab === 'favorites') loadFavorites();
    if (tab === 'positions') loadOpenOrders();
    if (tab === 'history') loadTradeHistory();
    if (tab === 'alerts') loadAlerts();
  }};

  // ── Market loading ──
  async function loadMarkets(query) {{
    const grid = document.getElementById('market-grid');
    if (!query && allMarkets.length > 0) return;  // already loaded
    grid.innerHTML = '<div class="loading"><span class="spinner"></span><br>Loading markets...</div>';
    try {{
      const url = query
        ? '/api/clob/markets?q=' + encodeURIComponent(query) + '&limit=30'
        : '/api/clob/markets?limit=30&offset=0';
      const resp = await fetch(url);
      const data = await resp.json();
      if (query) {{
        renderMarkets(data, true);
      }} else {{
        allMarkets = data;
        offset = data.length;
        renderMarkets(allMarkets, true);
        document.getElementById('load-more-btn').style.display = data.length >= 30 ? 'inline-block' : 'none';
      }}
    }} catch(e) {{
      grid.innerHTML = '<div class="loading">Failed to load markets.</div>';
    }}
  }}

  window.loadMoreMarkets = async function() {{
    try {{
      const resp = await fetch('/api/clob/markets?limit=30&offset=' + offset);
      const data = await resp.json();
      allMarkets = allMarkets.concat(data);
      offset += data.length;
      renderMarkets(allMarkets, true);
      document.getElementById('load-more-btn').style.display = data.length >= 30 ? 'inline-block' : 'none';
    }} catch(e) {{}}
  }};

  function renderMarkets(markets, isMain) {{
    const grid = document.getElementById(isMain ? 'market-grid' : 'favorites-grid');
    if (!markets || markets.length === 0) {{
      grid.innerHTML = '<div class="loading">No markets found.</div>';
      return;
    }}
    let html = '';
    for (const m of markets) {{
      const q = esc(m.question || m.title || '?');
      const condId = m.conditionId || m.condition_id || '';

      // Parse token prices
      let yesPrice = '—', noPrice = '—';
      const tokens = m.tokens || [];
      const outcomePrices = m.outcomePrices ? JSON.parse(m.outcomePrices) : null;
      if (outcomePrices && outcomePrices.length >= 2) {{
        yesPrice = (parseFloat(outcomePrices[0]) * 100).toFixed(0) + '%';
        noPrice = (parseFloat(outcomePrices[1]) * 100).toFixed(0) + '%';
      }} else if (tokens.length >= 2) {{
        yesPrice = tokens[0].price ? (tokens[0].price * 100).toFixed(0) + '%' : '—';
        noPrice = tokens[1].price ? (tokens[1].price * 100).toFixed(0) + '%' : '—';
      }}

      const vol = m.volume ? '$' + Number(m.volume).toLocaleString(undefined, {{maximumFractionDigits:0}}) : '—';
      const liq = m.liquidity ? '$' + Number(m.liquidity).toLocaleString(undefined, {{maximumFractionDigits:0}}) : '—';
      const isFav = favorites.has(condId);

      html += '<div class="market-card" style="position:relative;" onclick="openTradeModal(\'' + condId + '\')">'
        + '<button class="mc-fav ' + (isFav ? 'active' : '') + '" onclick="event.stopPropagation();toggleFav(\'' + condId + '\',\'' + q.replace(/'/g, "\\'") + '\')">' + (isFav ? '&#9733;' : '&#9734;') + '</button>'
        + '<div class="mc-question">' + q + '</div>'
        + '<div class="mc-outcomes">'
        + '<div class="mc-outcome yes"><div class="label">Yes</div><div class="price">' + yesPrice + '</div></div>'
        + '<div class="mc-outcome no"><div class="label">No</div><div class="price">' + noPrice + '</div></div>'
        + '</div>'
        + '<div class="mc-meta">'
        + '<span>Vol: ' + vol + '</span>'
        + '<span>Liq: ' + liq + '</span>'
        + '</div></div>';
    }}
    grid.innerHTML = html;
  }}

  // ── Search ──
  window.debounceSearch = function() {{
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {{
      const q = document.getElementById('market-search').value.trim();
      if (q.length >= 2) {{
        loadMarkets(q);
      }} else if (q.length === 0) {{
        renderMarkets(allMarkets, true);
      }}
    }}, 400);
  }};

  // ── Favorites ──
  async function loadFavoritesData() {{
    try {{
      const resp = await fetch('/api/clob/favorites');
      const data = await resp.json();
      favorites = new Set((data.favorites || []).map(f => f.condition_id));
    }} catch(e) {{}}
  }}

  async function loadFavorites() {{
    const grid = document.getElementById('favorites-grid');
    try {{
      const resp = await fetch('/api/clob/favorites');
      const data = await resp.json();
      const favs = data.favorites || [];
      favorites = new Set(favs.map(f => f.condition_id));
      if (favs.length === 0) {{
        grid.innerHTML = '<div class="loading">No favorites yet. Star markets to add them here.</div>';
        return;
      }}
      // Fetch each favorite's current market data
      const marketPromises = favs.map(f => fetch('/api/clob/markets?q=' + encodeURIComponent(f.question || '')).then(r => r.json()));
      const results = await Promise.all(marketPromises);
      const favMarkets = results.flat().filter(m => favs.some(f => f.condition_id === (m.conditionId || m.condition_id)));
      if (favMarkets.length > 0) {{
        renderFavoritesGrid(favMarkets);
      }} else {{
        // Fallback: show names
        grid.innerHTML = favs.map(f => '<div class="market-card" onclick="openTradeModal(\'' + f.condition_id + '\')"><div class="mc-question">' + esc(f.question) + '</div></div>').join('');
      }}
    }} catch(e) {{
      grid.innerHTML = '<div class="loading">Failed to load favorites.</div>';
    }}
  }}

  function renderFavoritesGrid(markets) {{
    const grid = document.getElementById('favorites-grid');
    // Reuse the main renderer
    let html = '';
    for (const m of markets) {{
      const q = esc(m.question || m.title || '?');
      const condId = m.conditionId || m.condition_id || '';
      let yesPrice = '—', noPrice = '—';
      const outcomePrices = m.outcomePrices ? JSON.parse(m.outcomePrices) : null;
      if (outcomePrices && outcomePrices.length >= 2) {{
        yesPrice = (parseFloat(outcomePrices[0]) * 100).toFixed(0) + '%';
        noPrice = (parseFloat(outcomePrices[1]) * 100).toFixed(0) + '%';
      }}
      const vol = m.volume ? '$' + Number(m.volume).toLocaleString(undefined, {{maximumFractionDigits:0}}) : '—';
      html += '<div class="market-card" onclick="openTradeModal(\'' + condId + '\')">'
        + '<button class="mc-fav active" onclick="event.stopPropagation();toggleFav(\'' + condId + '\',\'' + q.replace(/'/g, "\\'") + '\')">&#9733;</button>'
        + '<div class="mc-question">' + q + '</div>'
        + '<div class="mc-outcomes">'
        + '<div class="mc-outcome yes"><div class="label">Yes</div><div class="price">' + yesPrice + '</div></div>'
        + '<div class="mc-outcome no"><div class="label">No</div><div class="price">' + noPrice + '</div></div>'
        + '</div><div class="mc-meta"><span>Vol: ' + vol + '</span></div></div>';
    }}
    grid.innerHTML = html;
  }}

  window.toggleFav = async function(conditionId, question) {{
    if (favorites.has(conditionId)) {{
      favorites.delete(conditionId);
      await fetch('/api/clob/favorite/' + conditionId, {{method: 'DELETE'}});
    }} else {{
      favorites.add(conditionId);
      await fetch('/api/clob/favorite', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{condition_id: conditionId, question: question}})
      }});
    }}
    // Re-render current view
    if (allMarkets.length > 0) renderMarkets(allMarkets, true);
  }};

  // ── Trade Modal ──
  window.openTradeModal = async function(conditionId) {{
    const modal = document.getElementById('trade-modal');
    modal.classList.add('open');
    document.getElementById('modal-question').textContent = 'Loading...';
    document.getElementById('ob-asks').innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);"><span class="spinner"></span></div>';
    document.getElementById('ob-bids').innerHTML = '';
    document.getElementById('ob-spread').textContent = 'Spread: —';

    // Find market in loaded data
    let market = allMarkets.find(m => (m.conditionId || m.condition_id) === conditionId);
    if (!market) {{
      // Try fetching from API
      try {{
        const resp = await fetch('/api/clob/market/' + conditionId);
        if (resp.ok) market = await resp.json();
      }} catch(e) {{}}
    }}

    if (!market) {{
      document.getElementById('modal-question').textContent = 'Market not found';
      return;
    }}

    // Parse tokens
    const tokens = market.tokens || [];
    let yesToken = null, noToken = null;
    if (tokens.length >= 2) {{
      yesToken = tokens.find(t => (t.outcome || '').toLowerCase() === 'yes') || tokens[0];
      noToken = tokens.find(t => (t.outcome || '').toLowerCase() === 'no') || tokens[1];
    }} else if (market.clobTokenIds) {{
      // CLOB market format
      const ids = typeof market.clobTokenIds === 'string' ? JSON.parse(market.clobTokenIds) : market.clobTokenIds;
      yesToken = {{token_id: ids[0], outcome: 'Yes'}};
      noToken = {{token_id: ids[1], outcome: 'No'}};
    }}

    currentModal = {{market, yesToken, noToken, conditionId}};
    document.getElementById('modal-question').textContent = market.question || market.title || '?';

    // Set prices
    const outcomePrices = market.outcomePrices ? JSON.parse(market.outcomePrices) : null;
    if (outcomePrices && outcomePrices.length >= 2) {{
      document.getElementById('modal-yes-price').textContent = (parseFloat(outcomePrices[0]) * 100).toFixed(1) + '%';
      document.getElementById('modal-no-price').textContent = (parseFloat(outcomePrices[1]) * 100).toFixed(1) + '%';
    }}

    const vol = market.volume ? '$' + Number(market.volume).toLocaleString() : '—';
    const liq = market.liquidity ? '$' + Number(market.liquidity).toLocaleString() : '—';
    document.getElementById('modal-meta').innerHTML = 'Volume: ' + vol + ' &bull; Liquidity: ' + liq
      + ' &bull; <a href="https://polymarket.com/event/' + esc(market.slug || conditionId) + '" target="_blank" style="color:var(--blue);">View on Polymarket &#8599;</a>';

    // Load order book for the YES token
    if (yesToken) {{
      loadOrderBook(yesToken.token_id || yesToken);
    }}

    // Reset trade form
    tradeOutcome = 'yes';
    tradeSide = 'buy';
    document.getElementById('order-amount').value = '';
    document.getElementById('order-price').value = '';
    document.getElementById('order-size').value = '';
    updateToggles();
    updateEstimate();
  }};

  async function loadOrderBook(tokenId) {{
    try {{
      const resp = await fetch('/api/clob/book/' + tokenId);
      if (!resp.ok) throw new Error('Failed');
      const book = resp.ok ? await resp.json() : null;
      renderOrderBook(book);
    }} catch(e) {{
      document.getElementById('ob-asks').innerHTML = '<div style="padding:12px;text-align:center;color:var(--muted);">Order book unavailable</div>';
      document.getElementById('ob-bids').innerHTML = '';
    }}
  }}

  function renderOrderBook(book) {{
    const asksEl = document.getElementById('ob-asks');
    const bidsEl = document.getElementById('ob-bids');
    const spreadEl = document.getElementById('ob-spread');

    if (!book) {{
      asksEl.innerHTML = '<div style="padding:12px;color:var(--muted);text-align:center;">No data</div>';
      bidsEl.innerHTML = '';
      spreadEl.textContent = 'Spread: —';
      return;
    }}

    const asks = (book.asks || []).slice(0, 15).reverse();
    const bids = (book.bids || []).slice(0, 15);
    const maxSize = Math.max(...[...asks, ...bids].map(o => parseFloat(o.size || 0)), 1);

    asksEl.innerHTML = asks.map(o => {{
      const pct = (parseFloat(o.size) / maxSize * 100).toFixed(0);
      return '<div class="ob-row ask"><span class="ob-fill" style="width:' + pct + '%"></span>'
        + '<span style="color:var(--red);z-index:1;position:relative;">' + parseFloat(o.price).toFixed(2) + '</span>'
        + '<span style="z-index:1;position:relative;">' + parseFloat(o.size).toFixed(0) + '</span></div>';
    }}).join('');

    bidsEl.innerHTML = bids.map(o => {{
      const pct = (parseFloat(o.size) / maxSize * 100).toFixed(0);
      return '<div class="ob-row bid"><span class="ob-fill" style="width:' + pct + '%"></span>'
        + '<span style="color:var(--green);z-index:1;position:relative;">' + parseFloat(o.price).toFixed(2) + '</span>'
        + '<span style="z-index:1;position:relative;">' + parseFloat(o.size).toFixed(0) + '</span></div>';
    }}).join('');

    // Spread
    const bestBid = bids.length ? parseFloat(bids[0].price) : 0;
    const bestAsk = asks.length ? parseFloat(asks[asks.length - 1].price) : 0;
    if (bestBid && bestAsk) {{
      spreadEl.textContent = 'Spread: ' + ((bestAsk - bestBid) * 100).toFixed(1) + '% | Mid: ' + (((bestBid + bestAsk) / 2) * 100).toFixed(1) + '%';
    }}
  }}

  window.closeTradeModal = function() {{
    document.getElementById('trade-modal').classList.remove('open');
    currentModal = null;
  }};

  // Close modal on overlay click
  document.getElementById('trade-modal').addEventListener('click', function(e) {{
    if (e.target === this) closeTradeModal();
  }});

  // ── Trade controls ──
  window.setOutcome = function(outcome) {{
    tradeOutcome = outcome;
    updateToggles();
    // Reload order book for the selected outcome's token
    if (currentModal) {{
      const token = outcome === 'yes' ? currentModal.yesToken : currentModal.noToken;
      if (token) loadOrderBook(token.token_id || token);
    }}
    updateEstimate();
  }};

  window.setSide = function(side) {{
    tradeSide = side;
    updateToggles();
    updateEstimate();
  }};

  function updateToggles() {{
    const ob = document.getElementById('outcome-toggle').children;
    ob[0].className = tradeOutcome === 'yes' ? 'active-buy' : '';
    ob[1].className = tradeOutcome === 'no' ? 'active-sell' : '';

    const sb = document.getElementById('side-toggle').children;
    sb[0].className = tradeSide === 'buy' ? 'active-buy' : '';
    sb[1].className = tradeSide === 'sell' ? 'active-sell' : '';

    const submitBtn = document.getElementById('submit-order-btn');
    submitBtn.className = 'tp-submit ' + tradeSide;
    if (hasCreds) submitBtn.textContent = tradeSide === 'buy' ? 'Buy ' + tradeOutcome.toUpperCase() : 'Sell ' + tradeOutcome.toUpperCase();

    const orderType = document.getElementById('order-type').value;
    document.getElementById('amount-field').style.display = orderType === 'market' ? 'block' : 'none';
    document.getElementById('price-field').style.display = orderType === 'limit' ? 'block' : 'none';
    document.getElementById('size-field').style.display = orderType === 'limit' ? 'block' : 'none';
  }}

  window.updateEstimate = function() {{
    updateToggles();
    const orderType = document.getElementById('order-type').value;
    let cost = 0, payout = 0;

    if (orderType === 'market') {{
      const amount = parseFloat(document.getElementById('order-amount').value) || 0;
      // Estimate using current price
      let price = 0.5;
      if (currentModal && currentModal.market.outcomePrices) {{
        const prices = JSON.parse(currentModal.market.outcomePrices);
        price = tradeOutcome === 'yes' ? parseFloat(prices[0]) : parseFloat(prices[1]);
      }}
      cost = amount;
      payout = price > 0 ? amount / price : 0;
    }} else {{
      const price = parseFloat(document.getElementById('order-price').value) || 0;
      const size = parseFloat(document.getElementById('order-size').value) || 0;
      cost = price * size;
      payout = size;  // each share pays $1 if correct
    }}

    document.getElementById('est-cost').textContent = '$' + cost.toFixed(2);
    document.getElementById('est-payout').textContent = '$' + payout.toFixed(2);
    document.getElementById('est-profit').textContent = '$' + (payout - cost).toFixed(2);
    document.getElementById('est-profit').style.color = (payout - cost) >= 0 ? 'var(--green)' : 'var(--red)';
  }};

  // ── Order submission ──
  window.submitOrder = function() {{
    if (!hasCreds) {{
      toast('Wallet Not Connected', 'Go to Settings to add your Polymarket API keys.', true);
      return;
    }}
    if (!currentModal) return;

    const orderType = document.getElementById('order-type').value;
    const token = tradeOutcome === 'yes' ? currentModal.yesToken : currentModal.noToken;
    const tokenId = token ? (token.token_id || token) : '';

    let amount = 0, price = 0, size = 0;
    if (orderType === 'market') {{
      amount = parseFloat(document.getElementById('order-amount').value) || 0;
      if (amount <= 0) {{ toast('Invalid Amount', 'Enter an amount greater than 0.', true); return; }}
    }} else {{
      price = parseFloat(document.getElementById('order-price').value) || 0;
      size = parseFloat(document.getElementById('order-size').value) || 0;
      if (price <= 0 || size <= 0) {{ toast('Invalid Order', 'Price and shares must be greater than 0.', true); return; }}
    }}

    // Show confirmation
    pendingOrder = {{
      token_id: tokenId,
      condition_id: currentModal.conditionId,
      market_question: currentModal.market.question || '',
      outcome: tradeOutcome.toUpperCase(),
      side: tradeSide,
      order_type: orderType,
      amount, price, size
    }};

    const cost = orderType === 'market' ? amount : (price * size);
    document.getElementById('confirm-details').innerHTML =
      '<div style="margin-bottom:6px;font-weight:600;">' + esc(currentModal.market.question || '') + '</div>'
      + '<div><strong>' + tradeSide.toUpperCase() + '</strong> ' + tradeOutcome.toUpperCase() + '</div>'
      + '<div>Type: ' + orderType + '</div>'
      + (orderType === 'market'
        ? '<div>Amount: <strong>$' + amount.toFixed(2) + ' USDC</strong></div>'
        : '<div>Price: ' + price.toFixed(2) + ' &times; ' + size + ' shares = <strong>$' + cost.toFixed(2) + ' USDC</strong></div>'
      );

    const confirmBtn = document.getElementById('confirm-btn');
    confirmBtn.className = 'btn-confirm' + (tradeSide === 'sell' ? ' sell' : '');
    confirmBtn.textContent = tradeSide === 'buy' ? 'Confirm Buy' : 'Confirm Sell';
    document.getElementById('confirm-dialog').classList.add('open');
  }};

  window.cancelConfirm = function() {{
    document.getElementById('confirm-dialog').classList.remove('open');
    pendingOrder = null;
  }};

  window.confirmOrder = async function() {{
    document.getElementById('confirm-dialog').classList.remove('open');
    if (!pendingOrder) return;

    const btn = document.getElementById('submit-order-btn');
    btn.disabled = true;
    btn.textContent = 'Submitting...';

    try {{
      const resp = await fetch('/api/clob/order', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(pendingOrder)
      }});
      const data = await resp.json();
      if (resp.ok && !data.error) {{
        toast('Order Placed', pendingOrder.side.toUpperCase() + ' ' + pendingOrder.outcome + ' — ' + (pendingOrder.order_type === 'market' ? '$' + pendingOrder.amount : pendingOrder.size + ' shares'), false);
        // Refresh order book
        const token = tradeOutcome === 'yes' ? currentModal.yesToken : currentModal.noToken;
        if (token) loadOrderBook(token.token_id || token);
        // Refresh balance
        loadBalance();
      }} else {{
        toast('Order Failed', data.error || 'Unknown error', true);
      }}
    }} catch(e) {{
      toast('Network Error', e.message, true);
    }}

    btn.disabled = false;
    btn.textContent = tradeSide === 'buy' ? 'Buy ' + tradeOutcome.toUpperCase() : 'Sell ' + tradeOutcome.toUpperCase();
    pendingOrder = null;
  }};

  // ── Balance & Orders ──
  async function loadBalance() {{
    if (!hasCreds) return;
    try {{
      const resp = await fetch('/api/clob/balance');
      if (resp.ok) {{
        const data = await resp.json();
        const bal = data.allowances ? '$' + Number(data.allowances).toLocaleString(undefined, {{minimumFractionDigits: 2}}) : (data.error ? 'Error' : '$0.00');
        document.getElementById('usdc-balance').textContent = bal;
      }}
    }} catch(e) {{}}
  }}

  async function loadOpenOrders() {{
    if (!hasCreds) return;
    const container = document.getElementById('positions-container');
    try {{
      const resp = await fetch('/api/clob/orders');
      if (!resp.ok) throw new Error('Failed');
      const orders = await resp.json();
      const list = Array.isArray(orders) ? orders : [];
      document.getElementById('open-orders-count').textContent = list.length;

      if (list.length === 0) {{
        container.innerHTML = '<div class="loading">No open orders.</div>';
        return;
      }}
      let html = '<div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;"><table>'
        + '<thead><tr><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Filled</th><th>Action</th></tr></thead><tbody>';
      for (const o of list) {{
        html += '<tr>'
          + '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;">' + esc(o.market || o.asset_id || '—') + '</td>'
          + '<td><span class="' + (o.side === 'BUY' ? 'positive' : 'negative') + '">' + (o.side || '—') + '</span></td>'
          + '<td>' + (o.price || '—') + '</td>'
          + '<td>' + (o.size || o.original_size || '—') + '</td>'
          + '<td>' + (o.size_matched || '0') + '</td>'
          + '<td><button onclick="cancelOrder(\'' + (o.id || '') + '\')" style="background:none;border:1px solid var(--red);color:var(--red);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.75em;">Cancel</button></td>'
          + '</tr>';
      }}
      html += '</tbody></table></div>';
      container.innerHTML = html;
    }} catch(e) {{
      container.innerHTML = '<div class="loading">Failed to load orders.</div>';
    }}
  }}

  window.cancelOrder = async function(orderId) {{
    try {{
      const resp = await fetch('/api/clob/order/' + orderId, {{method: 'DELETE'}});
      if (resp.ok) {{
        toast('Order Cancelled', 'Order ' + orderId.substring(0, 8) + '... cancelled.', false);
        loadOpenOrders();
      }} else {{
        const data = await resp.json();
        toast('Cancel Failed', data.error || 'Unknown error', true);
      }}
    }} catch(e) {{
      toast('Error', e.message, true);
    }}
  }};

  async function loadTradeHistory() {{
    const container = document.getElementById('history-container');
    try {{
      const resp = await fetch('/api/clob/trades');
      if (!resp.ok) throw new Error('Failed');
      const data = await resp.json();
      const trades = data.trades || [];
      if (trades.length === 0) {{
        container.innerHTML = '<div class="loading">No trades yet.</div>';
        return;
      }}
      let html = '<div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;"><table>'
        + '<thead><tr><th>Time</th><th>Market</th><th>Outcome</th><th>Side</th><th>Type</th><th>Amount</th><th>Status</th></tr></thead><tbody>';
      for (const t of trades) {{
        const time = t.created_at ? new Date(t.created_at).toLocaleString() : '—';
        const question = (t.market_question || '').substring(0, 50);
        html += '<tr>'
          + '<td style="font-size:0.78em;white-space:nowrap;">' + time + '</td>'
          + '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;">' + esc(question) + '</td>'
          + '<td>' + esc(t.outcome || '—') + '</td>'
          + '<td><span class="' + (t.side === 'buy' ? 'positive' : 'negative') + '">' + (t.side || '—').toUpperCase() + '</span></td>'
          + '<td>' + (t.order_type || '—') + '</td>'
          + '<td>$' + (t.amount || 0).toFixed(2) + '</td>'
          + '<td>' + esc(t.status || '—') + '</td>'
          + '</tr>';
      }}
      html += '</tbody></table></div>';
      container.innerHTML = html;
    }} catch(e) {{
      container.innerHTML = '<div class="loading">Failed to load trade history.</div>';
    }}
  }}

  // ── Insider Alerts ──
  async function loadAlerts() {{
    const el = document.getElementById('news-trade-alerts');
    try {{
      const resp = await fetch('/api/news-trade-alerts');
      if (!resp.ok) throw new Error('Failed');
      const data = await resp.json();
      const alerts = data.alerts || [];
      if (alerts.length === 0) {{
        el.innerHTML = '<div style="padding:16px;color:var(--muted);text-align:center;">No insider alerts detected.</div>';
        return;
      }}
      let html = '';
      for (const a of alerts) {{
        const scoreColor = a.score >= 50 ? 'var(--red)' : a.score >= 25 ? 'var(--yellow)' : 'var(--muted)';
        html += '<div style="padding:12px 14px;border-bottom:1px solid var(--border);">'
          + '<div style="display:flex;justify-content:space-between;align-items:flex-start;">'
          + '<div style="font-size:0.85em;font-weight:600;flex:1;">'
          + (a.link ? '<a href="' + esc(a.link) + '" target="_blank" style="color:var(--text);text-decoration:none;">' + esc(a.title || '') + '</a>' : esc(a.title || ''))
          + '</div>'
          + '<span style="background:' + scoreColor + ';color:#fff;padding:2px 8px;border-radius:4px;font-size:0.7em;font-weight:700;">' + (a.score || 0) + '/100</span>'
          + '</div>'
          + '<div style="font-size:0.7em;color:var(--muted);margin-top:3px;">' + esc(a.source || '') + '</div>'
          + '</div>';
      }}
      el.innerHTML = html;
    }} catch(e) {{
      el.innerHTML = '<div style="padding:16px;color:var(--muted);text-align:center;">Failed to load alerts.</div>';
    }}
  }}

  // ── Init ──
  loadFavoritesData().then(() => loadMarkets());
  if (hasCreds) {{
    loadBalance();
  }}
}})();
</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Current Affairs News Feed ───────────────────────────────────────

_news_cache: list = []
_news_cache_time: float = 0
_NEWS_CACHE_TTL = 60  # refresh from RSS every 60s

RSS_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYT"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
]


def _fetch_news_from_rss() -> list:
    """Fetch latest headlines from multiple RSS feeds."""
    articles = []
    for url, source in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "CryptoEdge/1.0"})
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            # Standard RSS 2.0 structure
            for item in root.iter("item"):
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                desc_el = item.find("description")
                if title_el is None or title_el.text is None:
                    continue
                articles.append({
                    "title": title_el.text.strip(),
                    "link": (link_el.text or "").strip() if link_el is not None else "",
                    "source": source,
                    "published": (pub_el.text or "").strip() if pub_el is not None else "",
                    "summary": (desc_el.text or "").strip()[:200] if desc_el is not None else "",
                })
        except Exception:
            continue
    # Sort by published date (most recent first), limit to 30
    from email.utils import parsedate_to_datetime
    def _parse_pub_date(article):
        try:
            return parsedate_to_datetime(article.get("published", ""))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    articles.sort(key=_parse_pub_date, reverse=True)
    return articles[:30]


@app.get("/api/news")
async def api_news(request: Request):
    """Return cached current affairs headlines as JSON."""
    global _news_cache, _news_cache_time
    if not _check_auth(request):
        raise HTTPException(status_code=401)
    now = time.time()
    if now - _news_cache_time > _NEWS_CACHE_TTL or not _news_cache:
        _news_cache = await asyncio.to_thread(_fetch_news_from_rss)
        _news_cache_time = now
    return JSONResponse({"articles": _news_cache, "updated": _news_cache_time})


# ─── News-Trade Alerts API ──────────────────────────────────────────

@app.get("/api/news-trade-alerts")
async def api_news_trade_alerts(request: Request):
    """Return news-trade correlation alerts."""
    if not _check_auth(request):
        raise HTTPException(status_code=401)
    try:
        min_score = int(request.query_params.get("min_score", "0"))
    except ValueError:
        return JSONResponse({"error": "Invalid min_score"}, status_code=400)
    try:
        hours = int(request.query_params.get("hours", "72"))
    except ValueError:
        return JSONResponse({"error": "Invalid hours"}, status_code=400)
    min_score = max(0, min(min_score, 100))
    hours = max(1, min(hours, 720))  # lower bound of 1h prevents future-window queries
    alerts = await asyncio.to_thread(db.get_news_alerts, min_score, 50, hours)
    return JSONResponse({"alerts": alerts, "updated": last_news_trade_time})


@app.get("/api/news-watchlist")
async def api_get_watchlist(request: Request):
    """Get the current user's news-trade watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    items = await asyncio.to_thread(db.get_news_watchlist, user["id"])
    return JSONResponse({"watchlist": items})


@app.post("/api/news-watchlist/add")
async def api_add_to_watchlist(request: Request):
    """Add an alert to the user's watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    alert_id = body.get("alert_id", "")
    if not alert_id:
        raise HTTPException(status_code=400, detail="alert_id required")
    notes = body.get("notes", "")
    ok = await asyncio.to_thread(
        db.add_to_news_watchlist, user["id"], alert_id, notes
    )
    return JSONResponse({"ok": ok})


@app.post("/api/news-watchlist/remove")
async def api_remove_from_watchlist(request: Request):
    """Remove an alert from the user's watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    alert_id = body.get("alert_id", "")
    if not alert_id:
        raise HTTPException(status_code=400, detail="alert_id required")
    await asyncio.to_thread(db.remove_from_news_watchlist, user["id"], alert_id)
    return JSONResponse({"ok": True})


# ─── Accuracy Tracker ────────────────────────────────────────────────

@app.get("/accuracy", response_class=HTMLResponse)
async def accuracy_page(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    user = _get_session_user(request)

    # Get accuracy stats for each ticker
    stats_html = ""
    overall = db.get_accuracy_stats(days=30)
    for ticker in ASSETS:
        s = db.get_accuracy_stats(ticker=ticker, days=30)
        if s["total"] == 0:
            acc_cls = "muted"
            acc_str = "No data"
            hc_str = "—"
        else:
            acc_cls = "positive" if s["accuracy"] >= 0.53 else ("negative" if s["accuracy"] < 0.50 else "yellow")
            acc_str = f'{s["accuracy"]*100:.1f}%'
            hc_str = f'{s["high_conf_accuracy"]*100:.1f}% ({s["high_conf_total"]})' if s["high_conf_total"] else "—"

        stats_html += f"""<div class="card">
          <div class="label">{ticker}</div>
          <div class="value {acc_cls}">{acc_str}</div>
          <div class="detail">{s['total']} predictions | HC: {hc_str}</div>
        </div>"""

    # Recent predictions
    recent = db.get_recent_predictions(limit=50)
    recent_rows = ""
    for p in recent:
        if p["was_correct"] is not None:
            correct_cls = "positive" if p["was_correct"] else "negative"
            correct_str = "&#10003;" if p["was_correct"] else "&#10007;"
        else:
            correct_cls = "muted"
            correct_str = "pending"
        dir_cls = "positive" if p["pred_direction"] == "positive" else "negative"
        conf_pct = (p["confidence"] or 0) * 100
        # pred_direction and pred_delta can both be NULL in the DB; render
        # safely so a single null row doesn't 500 the whole accuracy page.
        pdir_str = (p["pred_direction"] or "").upper() or "—"
        pd_val = p.get("pred_delta") if hasattr(p, "get") else p["pred_delta"]
        pd_str = f"${pd_val:+,.2f}" if pd_val is not None else "—"
        recent_rows += f"""<tr>
          <td>{p['ticker']}</td>
          <td>{p['window_start'][:16]}</td>
          <td class="{dir_cls}">{pdir_str}</td>
          <td>{pd_str}</td>
          <td>{conf_pct:.0f}%</td>
          <td>{p.get('actual_direction','—') or '—'}</td>
          <td class="{correct_cls}" style="font-weight:700;">{correct_str}</td>
        </tr>"""

    ov_acc = f'{overall["accuracy"]*100:.1f}%' if overall["total"] else "No data"
    ov_cls = "positive" if overall.get("accuracy",0) >= 0.53 else ("negative" if overall.get("accuracy",0) < 0.50 else "yellow")

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Accuracy Tracker</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:4px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }} .yellow {{ color:var(--yellow); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.4em;font-weight:700;margin-top:2px; }}
  .card .detail {{ color:var(--muted);font-size:0.7em;margin-top:2px; }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  .hero {{ background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;text-align:center;margin-bottom:20px; }}
</style></head><body>
<div class="nav">
  <div class="nav-links">
    <a href="/polybot">Polymarket Bot</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Model Accuracy Tracker</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:16px;">Live tracking of every prediction vs actual outcome — 30 day window</p>

<div class="hero">
  <div style="color:var(--muted);font-size:0.8em;text-transform:uppercase;">Overall Accuracy (30d)</div>
  <div style="font-size:2.5em;font-weight:800;" class="{ov_cls}">{ov_acc}</div>
  <div style="color:var(--muted);font-size:0.85em;margin-top:4px;">{overall['total']:,} total predictions | {overall['correct']:,} correct</div>
</div>

<h2 style="font-size:1em;color:var(--blue);margin-bottom:8px;">Per-Asset Accuracy</h2>
<div class="cards">{stats_html}</div>

<h2 style="font-size:1em;color:var(--blue);margin-bottom:8px;margin-top:24px;">Recent Predictions</h2>
<div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;max-height:60vh;overflow-y:auto;">
  <table>
    <thead><tr><th>Asset</th><th>Window</th><th>Predicted</th><th>Delta</th><th>Conf</th><th>Actual</th><th>Result</th></tr></thead>
    <tbody>{recent_rows if recent_rows else '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px;">Predictions will appear here as the models run. Data is logged every 5 minutes.</td></tr>'}</tbody>
  </table>
</div>

<script>setInterval(()=>location.reload(),60000);</script>
</body></html>"""
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════
# CLOB TRADING API
# ═══════════════════════════════════════════════════════════════════════

# -- In-memory trader cache (per user_id) with TTL --
_trader_cache: dict[str, tuple[clob.ClobTrader, float]] = {}
_TRADER_CACHE_TTL = 3600  # 1 hour
_TRADER_CACHE_MAX = 100


def _get_trader(user_id: str) -> clob.ClobTrader | None:
    """Get or create a ClobTrader for a user from their stored credentials."""
    now = time.time()
    if user_id in _trader_cache:
        trader, ts = _trader_cache[user_id]
        if now - ts < _TRADER_CACHE_TTL:
            return trader
        del _trader_cache[user_id]  # expired
    enc = db.get_clob_credentials(user_id)
    if not enc:
        return None
    try:
        creds = clob.decrypt_credentials(enc)
        trader = clob.ClobTrader(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
            private_key=creds["private_key"],
        )
        # Evict oldest entries if cache is full
        if len(_trader_cache) >= _TRADER_CACHE_MAX:
            oldest_key = min(_trader_cache, key=lambda k: _trader_cache[k][1])
            del _trader_cache[oldest_key]
        _trader_cache[user_id] = (trader, now)
        return trader
    except Exception as e:
        print(f"  [CLOB] Failed to create trader for {user_id}: {e}")
        return None


@app.get("/api/clob/book/{token_id}")
async def clob_order_book(token_id: str, request: Request):
    """Get order book for a token (no auth needed for read)."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    book = await asyncio.to_thread(clob.get_order_book, token_id)
    if book is None:
        return JSONResponse({"error": "Failed to fetch order book"}, status_code=502)
    return book


@app.get("/api/clob/price/{token_id}")
async def clob_price(token_id: str, request: Request, side: str = "buy"):
    """Get best price for a token."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    price = await asyncio.to_thread(clob.get_price, token_id, side)
    if price is None:
        return JSONResponse({"error": "Failed to fetch price"}, status_code=502)
    return price


@app.get("/api/clob/markets")
async def clob_markets(request: Request, limit: int = 50, offset: int = 0, q: str = ""):
    """Get markets from Gamma API with optional search."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if q:
        markets = await asyncio.to_thread(clob.search_markets, q, limit)
    else:
        markets = await asyncio.to_thread(clob.get_markets, limit, offset)
    return markets


@app.get("/api/clob/market/{condition_id}")
async def clob_market_detail(condition_id: str, request: Request):
    """Get detailed market info from CLOB."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    market = await asyncio.to_thread(clob.get_clob_market, condition_id)
    if market is None:
        return JSONResponse({"error": "Market not found"}, status_code=404)
    return market


@app.post("/api/clob/credentials")
async def save_clob_credentials(request: Request):
    """Save encrypted CLOB API credentials."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
    required = ["api_key", "api_secret", "api_passphrase", "private_key"]
    for field in required:
        # Use `or ""` so JSON null doesn't crash on .strip()
        if not (body.get(field) or "").strip():
            return JSONResponse({"error": f"Missing {field}"}, status_code=400)
    try:
        encrypted = clob.encrypt_credentials({
            "api_key": (body.get("api_key") or "").strip(),
            "api_secret": (body.get("api_secret") or "").strip(),
            "api_passphrase": (body.get("api_passphrase") or "").strip(),
            "private_key": (body.get("private_key") or "").strip(),
        })
        db.save_clob_credentials(user["id"], encrypted)
        # Clear cached trader so it re-initializes with new creds
        _trader_cache.pop(user["id"], None)
        return {"ok": True}
    except Exception:
        return JSONResponse({"error": "Failed to save credentials"}, status_code=500)


@app.delete("/api/clob/credentials")
async def delete_clob_credentials(request: Request):
    """Remove stored CLOB credentials."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_clob_credentials(user["id"])
    _trader_cache.pop(user["id"], None)
    return {"ok": True}


@app.get("/api/clob/test-connection")
async def test_clob_connection(request: Request):
    """Test that stored CLOB credentials work."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trader = _get_trader(user["id"])
    if not trader:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    result = await asyncio.to_thread(trader.test_connection)
    return result


@app.get("/api/clob/balance")
async def clob_balance(request: Request):
    """Get USDC balance from CLOB."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trader = _get_trader(user["id"])
    if not trader:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    balance = await asyncio.to_thread(trader.get_balance)
    return balance


@app.post("/api/clob/order")
async def place_clob_order(request: Request):
    """Place an order on Polymarket CLOB."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trader = _get_trader(user["id"])
    if not trader:
        return JSONResponse({"error": "No credentials configured. Go to Settings to add your Polymarket API keys."}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
    token_id = (body.get("token_id") or "").strip()
    side = (body.get("side") or "").strip().lower()
    order_type = (body.get("order_type") or "market").strip().lower()
    try:
        amount = float(body.get("amount") or 0)
        price = float(body.get("price") or 0)
        size = float(body.get("size") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "amount, price and size must be numeric"}, status_code=400)
    if math.isnan(amount) or math.isinf(amount) or math.isnan(price) or math.isinf(price) or math.isnan(size) or math.isinf(size):
        return JSONResponse({"error": "amount, price and size must be finite numbers"}, status_code=400)

    if not token_id:
        return JSONResponse({"error": "Missing token_id"}, status_code=400)
    if side not in ("buy", "sell"):
        return JSONResponse({"error": "Side must be 'buy' or 'sell'"}, status_code=400)
    if order_type == "market" and amount <= 0:
        return JSONResponse({"error": "Amount must be > 0 for market orders"}, status_code=400)
    if order_type == "limit" and (price <= 0 or size <= 0):
        return JSONResponse({"error": "Price and size must be > 0 for limit orders"}, status_code=400)

    # Place the order
    if order_type == "market":
        if side == "buy":
            result = await asyncio.to_thread(trader.place_market_buy, token_id, amount)
        else:
            result = await asyncio.to_thread(trader.place_market_sell, token_id, amount)
    else:
        result = await asyncio.to_thread(trader.place_limit_order, token_id, price, size, side)

    # Log the trade
    status = "error" if "error" in result else "submitted"
    order_id = result.get("orderID", result.get("order_id", ""))
    db.log_clob_trade(
        user_id=user["id"],
        order_id=order_id,
        condition_id=body.get("condition_id", ""),
        token_id=token_id,
        market_question=body.get("market_question", ""),
        outcome=body.get("outcome", ""),
        side=side,
        order_type=order_type,
        price=price if order_type == "limit" else 0,
        size=size if order_type == "limit" else 0,
        amount=amount,
        status=status,
        response_data=result,
    )

    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.get("/api/clob/orders")
async def clob_open_orders(request: Request):
    """Get user's open orders."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trader = _get_trader(user["id"])
    if not trader:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    orders = await asyncio.to_thread(trader.get_open_orders)
    return orders


@app.delete("/api/clob/order/{order_id}")
async def cancel_clob_order(order_id: str, request: Request):
    """Cancel an open order."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trader = _get_trader(user["id"])
    if not trader:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    result = await asyncio.to_thread(trader.cancel_order, order_id)
    return result


@app.get("/api/clob/trades")
async def clob_trade_history(request: Request):
    """Get user's trade history (from CLOB + local log)."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    local_trades = db.get_clob_trades(user["id"], limit=50)
    return {"trades": local_trades}


@app.post("/api/clob/favorite")
async def add_clob_favorite(request: Request):
    """Add a market to favorites."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or "condition_id" not in body:
        return JSONResponse({"error": "missing condition_id"}, status_code=400)
    db.add_clob_favorite(user["id"], body["condition_id"], body.get("question", ""))
    return {"ok": True}


@app.delete("/api/clob/favorite/{condition_id}")
async def remove_clob_favorite(condition_id: str, request: Request):
    """Remove a market from favorites."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.remove_clob_favorite(user["id"], condition_id)
    return {"ok": True}


@app.get("/api/clob/favorites")
async def get_clob_favorites(request: Request):
    """Get user's favorite markets."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    favs = db.get_clob_favorites(user["id"])
    return {"favorites": favs}


# ═══════════════════════════════════════════════════════════════════════
# KALSHI AUTHENTICATED API
# ═══════════════════════════════════════════════════════════════════════

# In-memory Kalshi client cache (per user_id) with TTL
_kalshi_cache: dict[str, tuple] = {}
_KALSHI_CACHE_TTL = 3600  # 1 hour
_KALSHI_CACHE_MAX = 100


def _get_kalshi_client(user_id: str):
    """Get or create a KalshiClient for a user from their stored credentials."""
    now = time.time()
    if user_id in _kalshi_cache:
        client, ts = _kalshi_cache[user_id]
        if now - ts < _KALSHI_CACHE_TTL:
            return client
        del _kalshi_cache[user_id]
    enc = db.get_kalshi_credentials(user_id)
    if not enc:
        return None
    try:
        creds = kalshi_auth.decrypt_kalshi_credentials(enc)
        client = kalshi_auth.KalshiClient(
            api_key=creds["api_key"],
            private_key_pem=creds["private_key_pem"],
        )
        if len(_kalshi_cache) >= _KALSHI_CACHE_MAX:
            oldest_key = min(_kalshi_cache, key=lambda k: _kalshi_cache[k][1])
            del _kalshi_cache[oldest_key]
        _kalshi_cache[user_id] = (client, now)
        return client
    except Exception as e:
        print(f"  [Kalshi] Failed to create client for {user_id}: {e}")
        return None


@app.post("/api/kalshi/credentials")
async def save_kalshi_credentials_api(request: Request):
    """Save encrypted Kalshi API credentials."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    private_key_pem = (body.get("private_key_pem") or "").strip()
    if not api_key or not private_key_pem:
        return JSONResponse({"error": "Both api_key and private_key_pem are required"}, status_code=400)
    if "BEGIN" not in private_key_pem or "PRIVATE KEY" not in private_key_pem:
        return JSONResponse({"error": "private_key_pem must be a PEM-encoded RSA key"}, status_code=400)
    # Validate the key actually parses before saving
    try:
        kalshi_auth._load_rsa_key(private_key_pem)
    except Exception as e:
        return JSONResponse({"error": f"Invalid RSA private key: {e}"}, status_code=400)
    try:
        encrypted = kalshi_auth.encrypt_kalshi_credentials(api_key, private_key_pem)
        db.save_kalshi_credentials(user["id"], encrypted)
        _kalshi_cache.pop(user["id"], None)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": f"Failed to save credentials: {e}"}, status_code=500)


@app.delete("/api/kalshi/credentials")
async def delete_kalshi_credentials_api(request: Request):
    """Remove stored Kalshi credentials."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_kalshi_credentials(user["id"])
    _kalshi_cache.pop(user["id"], None)
    return {"ok": True}


@app.get("/api/kalshi/test-connection")
async def test_kalshi_connection(request: Request):
    """Test that stored Kalshi credentials work."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_kalshi_client(user["id"])
    if not client:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    result = await asyncio.to_thread(client.test_connection)
    return result


@app.get("/api/kalshi/balance")
async def kalshi_balance(request: Request):
    """Get Kalshi portfolio balance."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_kalshi_client(user["id"])
    if not client:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    return await asyncio.to_thread(client.get_balance)


@app.post("/api/kalshi/order")
async def place_kalshi_order(request: Request):
    """Place an order on Kalshi."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_kalshi_client(user["id"])
    if not client:
        return JSONResponse({"error": "No credentials configured. Go to Settings to add your Kalshi API keys."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
    ticker = (body.get("ticker") or "").strip()
    side = (body.get("side") or "").strip().lower()
    action = (body.get("action") or "buy").strip().lower()
    try:
        count = int(float(body.get("count") or 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "count must be an integer"}, status_code=400)
    order_type = (body.get("order_type") or "market").strip().lower()
    if not ticker:
        return JSONResponse({"error": "Missing ticker"}, status_code=400)
    if side not in ("yes", "no"):
        return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
    if count <= 0:
        return JSONResponse({"error": "count must be > 0"}, status_code=400)
    yes_price = body.get("yes_price")
    no_price = body.get("no_price")
    result = await asyncio.to_thread(
        client.place_order, ticker, side, action, count, order_type,
        yes_price, no_price, body.get("client_order_id"),
    )
    if isinstance(result, dict) and "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.get("/api/kalshi/orders")
async def kalshi_open_orders(request: Request):
    """Get user's resting Kalshi orders."""
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    client = _get_kalshi_client(user["id"])
    if not client:
        return JSONResponse({"error": "No credentials configured"}, status_code=400)
    return await asyncio.to_thread(client.get_orders)


# ─── Settings / Watchlist ────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://narve.ai/login", status_code=302)
    user = _get_session_user(request)
    watchlists = db.get_watchlists(user["id"])
    alert_prefs = db.get_alert_prefs(user["id"])

    wl_html = ""
    for wl in watchlists:
        try:
            tickers = json.loads(wl["tickers"]) if isinstance(wl["tickers"], str) else wl["tickers"]
        except (json.JSONDecodeError, TypeError):
            tickers = []
        if not isinstance(tickers, list):
            tickers = []
        # Escape both the user-provided watchlist name and the ticker list to
        # prevent stored self-XSS via /api/watchlist/create.
        name_esc = html_mod.escape(str(wl["name"] or ""))
        tickers_esc = html_mod.escape(", ".join(str(t) for t in tickers))
        wl_html += f'<div class="card"><div class="label">{name_esc}</div><div class="value" style="font-size:1em;">{tickers_esc}</div></div>'
    if not wl_html:
        wl_html = '<div style="color:var(--muted);">No watchlists yet.</div>'

    tier_esc = html_mod.escape(user["tier"].upper())
    tier_badge = f'<span style="background:{"var(--green)" if user["tier"]=="premium" else "var(--blue)"};color:#fff;padding:3px 10px;border-radius:12px;font-size:0.75em;font-weight:600;">{tier_esc}</span>'

    has_creds = db.has_clob_credentials(user["id"])
    has_kalshi_creds = db.has_kalshi_credentials(user["id"])

    poly_status_color = "var(--green)" if has_creds else "var(--red)"
    poly_status_text = "Connected" if has_creds else "Not Connected"
    kalshi_status_color = "var(--green)" if has_kalshi_creds else "var(--red)"
    kalshi_status_text = "Connected" if has_kalshi_creds else "Not Connected"

    poly_connected_block = (
        '<div id="creds-connected">'
        '<p style="color:var(--muted);font-size:0.82em;margin-bottom:12px;">'
        'Your Polymarket CLOB API credentials are stored encrypted. You can trade directly '
        'from the <a href="/trade" style="color:var(--blue);">Trade</a> page or any market on '
        'the <a href="/" style="color:var(--blue);">Dashboard</a>, '
        '<a href="/kalshi" style="color:var(--blue);">Kalshi page</a>, or '
        '<a href="/polybot" style="color:var(--blue);">Bot page</a>.'
        '</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">'
        '<button class="btn btn-secondary" onclick="testConnection()">Test Connection</button>'
        '<button class="btn btn-secondary" onclick="showCredsForm()">Replace Credentials</button>'
        '<button class="btn btn-danger" onclick="removeCreds()">Disconnect</button>'
        '</div></div>'
    ) if has_creds else ""

    kalshi_connected_block = (
        '<div id="kalshi-connected">'
        '<p style="color:var(--muted);font-size:0.82em;margin-bottom:12px;">'
        'Your Kalshi API key is stored encrypted. You can place YES/NO orders directly '
        'from the <a href="/kalshi" style="color:var(--blue);">Kalshi page</a>.'
        '</p>'
        '<div style="display:flex;gap:8px;flex-wrap:wrap;">'
        '<button class="btn btn-secondary" onclick="testKalshiConnection()">Test Connection</button>'
        '<button class="btn btn-secondary" onclick="showKalshiForm()">Replace Credentials</button>'
        '<button class="btn btn-danger" onclick="removeKalshiCreds()">Disconnect</button>'
        '</div></div>'
    ) if has_kalshi_creds else ""

    poly_form_display = "none" if has_creds else "block"
    kalshi_form_display = "none" if has_kalshi_creds else "block"

    poly_cancel_btn = '<button class="btn btn-secondary" onclick="cancelCredsForm()">Cancel</button>' if has_creds else ""
    kalshi_cancel_btn = '<button class="btn btn-secondary" onclick="cancelKalshiForm()">Cancel</button>' if has_kalshi_creds else ""

    tier_html = (
        "<p style='color:var(--green);font-weight:600;'>Premium features active: Neural Net predictions, Model Marketplace</p>"
        if user['tier'] in ('premium','admin')
        else "<p style='color:var(--muted);'>Free tier — upgrade to Premium for neural net predictions and model marketplace.</p><p style='margin-top:8px;'><em>Contact admin to upgrade.</em></p>"
    )

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Settings</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922;--purple:#a371f7; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px;max-width:760px;margin:0 auto; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap; }}
  .nav a {{ color:var(--muted);text-decoration:none; }}
  .nav a:hover {{ color:var(--text); }}
  h1 {{ font-size:1.4em;margin-bottom:16px; }}
  h2 {{ font-size:1em;color:var(--blue);margin:24px 0 8px;display:flex;align-items:center;gap:8px; }}
  h2 .badge {{ font-size:0.7em;padding:2px 8px;border-radius:10px;font-weight:600; }}
  .badge.poly {{ background:rgba(163,113,247,0.15);color:var(--purple);border:1px solid rgba(163,113,247,0.4); }}
  .badge.kalshi {{ background:rgba(0,180,216,0.15);color:#00b4d8;border:1px solid rgba(0,180,216,0.4); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.1em;font-weight:600;margin-top:4px; }}
  .info-box {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px; }}
  .form-field {{ margin-bottom:12px; }}
  .form-field label {{ display:block;font-size:0.75em;color:var(--muted);margin-bottom:4px;text-transform:uppercase; }}
  .form-field .hint {{ font-size:0.7em;color:var(--muted);margin-top:3px;text-transform:none; }}
  .form-field input, .form-field textarea {{ width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-size:0.85em;font-family:'SF Mono',Monaco,monospace; }}
  .form-field textarea {{ min-height:140px;resize:vertical;line-height:1.4; }}
  .form-field input:focus, .form-field textarea:focus {{ outline:none;border-color:var(--blue); }}
  .btn {{ padding:8px 18px;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:0.85em;transition:all 0.2s; }}
  .btn-primary {{ background:var(--blue);color:#fff; }}
  .btn-primary:hover {{ opacity:0.9; }}
  .btn-danger {{ background:var(--red);color:#fff; }}
  .btn-danger:hover {{ opacity:0.9; }}
  .btn-secondary {{ background:transparent;color:var(--blue);border:1px solid var(--blue); }}
  .btn-secondary:hover {{ background:rgba(88,166,255,0.1); }}
  .btn:disabled {{ opacity:0.4;cursor:not-allowed; }}
  .status-dot {{ display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle; }}
  .help {{ background:rgba(88,166,255,0.05);border:1px solid rgba(88,166,255,0.2);border-radius:6px;padding:10px;margin-bottom:14px;font-size:0.78em;color:var(--text);line-height:1.5; }}
  .help b {{ color:var(--blue); }}
  .help ol {{ margin:6px 0 0 18px; }}
  .help a {{ color:var(--blue);text-decoration:underline; }}
  .warn {{ background:rgba(210,153,34,0.08);border:1px solid rgba(210,153,34,0.3);border-radius:6px;padding:10px;margin-bottom:14px;font-size:0.75em;color:var(--yellow);line-height:1.5; }}
</style></head><body>
<div class="nav">
  <a href="/">&larr; Dashboard</a>
  <a href="/kalshi">Kalshi</a>
  <a href="/trade">Trade</a>
  <a href="/polybot">Bot</a>
  <a href="/settings" style="color:var(--blue);font-weight:600;">Settings</a>
</div>

<h1>Account Settings {tier_badge}</h1>

<div class="info-box">
  <div style="color:var(--muted);font-size:0.8em;">EMAIL</div>
  <div style="font-size:1.1em;margin-top:2px;">{html_mod.escape(user['email'])}</div>
</div>
<div class="info-box">
  <div style="color:var(--muted);font-size:0.8em;">NAME</div>
  <div style="font-size:1.1em;margin-top:2px;">{html_mod.escape(user['display_name'] or '(not set)')}</div>
</div>

<h2>Your Tier: {tier_esc}</h2>
<div class="info-box">{tier_html}</div>

<!-- ═══════ POLYMARKET ═══════ -->
<h2 id="polymarket">Polymarket Wallet <span class="badge poly">Trading</span></h2>
<div class="info-box" id="clob-section">
  <div style="margin-bottom:12px;">
    <span class="status-dot" style="background:{poly_status_color};"></span>
    <strong>{poly_status_text}</strong>
    <span style="color:var(--muted);font-size:0.8em;margin-left:8px;" id="conn-test-result"></span>
  </div>

  {poly_connected_block}

  <div id="creds-form" style="display:{poly_form_display};">
    <div class="help">
      <b>How to get your Polymarket API keys</b>
      <ol>
        <li>Go to <a href="https://polymarket.com" target="_blank">polymarket.com</a> and log in.</li>
        <li>Click your profile &rarr; <b>API Keys</b> (or visit <code>/profile/api</code>).</li>
        <li>Click <b>Create API Key</b> &mdash; you'll get an <b>API Key</b>, <b>Secret</b>, and <b>Passphrase</b>.</li>
        <li>Your <b>private key</b> is the Polygon wallet you funded with USDC. Export it from MetaMask &rarr; Account Details &rarr; Show private key, or whatever wallet you're using.</li>
      </ol>
    </div>
    <div class="warn">
      &#9888; <b>Use a dedicated trading wallet</b>, not your main wallet. Credentials are encrypted with AES-256 before storage and the private key never leaves this server, but you should still minimize exposure. <br>
      &#9888; You also need <b>USDC.e on Polygon</b> in this wallet to actually trade.
    </div>

    <div class="form-field">
      <label>API Key</label>
      <input type="text" id="clob-api-key" placeholder="0x..." autocomplete="off">
    </div>
    <div class="form-field">
      <label>API Secret</label>
      <input type="password" id="clob-api-secret" placeholder="Your API secret" autocomplete="off">
    </div>
    <div class="form-field">
      <label>API Passphrase</label>
      <input type="password" id="clob-api-passphrase" placeholder="Your passphrase" autocomplete="off">
    </div>
    <div class="form-field">
      <label>Private Key (Polygon wallet)</label>
      <input type="password" id="clob-private-key" placeholder="0x..." autocomplete="off">
      <div class="hint">64-char hex string starting with 0x</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary" id="save-creds-btn" onclick="saveCreds()">Save &amp; Connect</button>
      {poly_cancel_btn}
    </div>
    <div id="creds-error" style="color:var(--red);font-size:0.8em;margin-top:8px;display:none;"></div>
  </div>
</div>

<!-- ═══════ KALSHI ═══════ -->
<h2 id="kalshi">Kalshi Account <span class="badge kalshi">Trading</span></h2>
<div class="info-box" id="kalshi-section">
  <div style="margin-bottom:12px;">
    <span class="status-dot" style="background:{kalshi_status_color};"></span>
    <strong>{kalshi_status_text}</strong>
    <span style="color:var(--muted);font-size:0.8em;margin-left:8px;" id="kalshi-test-result"></span>
  </div>

  {kalshi_connected_block}

  <div id="kalshi-form" style="display:{kalshi_form_display};">
    <div class="help">
      <b>How to get your Kalshi API key</b>
      <ol>
        <li>Go to <a href="https://kalshi.com/account/profile" target="_blank">kalshi.com/account/profile</a> and log in.</li>
        <li>Scroll to the <b>API Keys</b> section &rarr; click <b>Create New Key</b>.</li>
        <li>Kalshi will show you the <b>API Key ID</b> (a UUID) and download a <b>private_key.pem</b> file. Save the file &mdash; you cannot download it again.</li>
        <li>Open <code>private_key.pem</code> in a text editor and paste its full contents (including the <code>-----BEGIN PRIVATE KEY-----</code> lines) below.</li>
      </ol>
    </div>
    <div class="warn">
      &#9888; The PEM file contains your full RSA private key &mdash; treat it like a password. It's encrypted with AES-256 before storage and only used to sign Kalshi API requests on this server. <br>
      &#9888; You also need a <b>funded Kalshi account</b> to actually trade.
    </div>

    <div class="form-field">
      <label>API Key ID</label>
      <input type="text" id="kalshi-api-key" placeholder="00000000-0000-0000-0000-000000000000" autocomplete="off">
      <div class="hint">UUID provided by Kalshi when you created the key</div>
    </div>
    <div class="form-field">
      <label>Private Key (PEM)</label>
      <textarea id="kalshi-private-key" placeholder="-----BEGIN PRIVATE KEY-----&#10;MIIE...&#10;-----END PRIVATE KEY-----" autocomplete="off" spellcheck="false"></textarea>
      <div class="hint">Paste the full contents of private_key.pem (multi-line)</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary" id="save-kalshi-btn" onclick="saveKalshiCreds()">Save &amp; Connect</button>
      {kalshi_cancel_btn}
    </div>
    <div id="kalshi-error" style="color:var(--red);font-size:0.8em;margin-top:8px;display:none;"></div>
  </div>
</div>

<h2>Watchlists</h2>
<div class="cards">{wl_html}</div>

<script>
(function() {{
  // ─── Polymarket ───
  async function saveCreds() {{
    const btn = document.getElementById('save-creds-btn');
    const errEl = document.getElementById('creds-error');
    errEl.style.display = 'none';
    const data = {{
      api_key: document.getElementById('clob-api-key').value.trim(),
      api_secret: document.getElementById('clob-api-secret').value.trim(),
      api_passphrase: document.getElementById('clob-api-passphrase').value.trim(),
      private_key: document.getElementById('clob-private-key').value.trim(),
    }};
    if (!data.api_key || !data.api_secret || !data.api_passphrase || !data.private_key) {{
      errEl.textContent = 'All fields are required.';
      errEl.style.display = 'block';
      return;
    }}
    if (!data.private_key.startsWith('0x') || data.private_key.length < 64) {{
      errEl.textContent = 'Private key must be a 0x-prefixed hex string.';
      errEl.style.display = 'block';
      return;
    }}
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {{
      const resp = await fetch('/api/clob/credentials', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
      }});
      const result = await resp.json();
      if (resp.ok && result.ok) {{
        location.reload();
      }} else {{
        errEl.textContent = result.error || 'Failed to save credentials.';
        errEl.style.display = 'block';
      }}
    }} catch(e) {{
      errEl.textContent = 'Network error: ' + e.message;
      errEl.style.display = 'block';
    }}
    btn.disabled = false;
    btn.textContent = 'Save & Connect';
  }}

  async function testConnection() {{
    const resultEl = document.getElementById('conn-test-result');
    resultEl.textContent = 'Testing...';
    resultEl.style.color = 'var(--muted)';
    try {{
      const resp = await fetch('/api/clob/test-connection');
      const data = await resp.json();
      if (data.ok) {{
        resultEl.textContent = 'Connection successful';
        resultEl.style.color = 'var(--green)';
      }} else {{
        resultEl.textContent = 'Failed: ' + (data.error || 'Unknown error');
        resultEl.style.color = 'var(--red)';
      }}
    }} catch(e) {{
      resultEl.textContent = 'Error: ' + e.message;
      resultEl.style.color = 'var(--red)';
    }}
  }}

  async function removeCreds() {{
    if (!confirm('Disconnect your Polymarket wallet? You will not be able to trade Polymarket markets until you reconnect.')) return;
    try {{
      await fetch('/api/clob/credentials', {{method: 'DELETE'}});
      location.reload();
    }} catch(e) {{}}
  }}

  function showCredsForm() {{
    document.getElementById('creds-form').style.display = 'block';
    const conn = document.getElementById('creds-connected');
    if (conn) conn.style.display = 'none';
  }}

  function cancelCredsForm() {{
    document.getElementById('creds-form').style.display = 'none';
    const conn = document.getElementById('creds-connected');
    if (conn) conn.style.display = 'block';
  }}

  // ─── Kalshi ───
  async function saveKalshiCreds() {{
    const btn = document.getElementById('save-kalshi-btn');
    const errEl = document.getElementById('kalshi-error');
    errEl.style.display = 'none';
    const data = {{
      api_key: document.getElementById('kalshi-api-key').value.trim(),
      private_key_pem: document.getElementById('kalshi-private-key').value.trim(),
    }};
    if (!data.api_key || !data.private_key_pem) {{
      errEl.textContent = 'Both fields are required.';
      errEl.style.display = 'block';
      return;
    }}
    if (!data.private_key_pem.includes('BEGIN') || !data.private_key_pem.includes('PRIVATE KEY')) {{
      errEl.textContent = 'Paste the full PEM file including the -----BEGIN PRIVATE KEY----- lines.';
      errEl.style.display = 'block';
      return;
    }}
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {{
      const resp = await fetch('/api/kalshi/credentials', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
      }});
      const result = await resp.json();
      if (resp.ok && result.ok) {{
        location.reload();
      }} else {{
        errEl.textContent = result.error || 'Failed to save credentials.';
        errEl.style.display = 'block';
      }}
    }} catch(e) {{
      errEl.textContent = 'Network error: ' + e.message;
      errEl.style.display = 'block';
    }}
    btn.disabled = false;
    btn.textContent = 'Save & Connect';
  }}

  async function testKalshiConnection() {{
    const resultEl = document.getElementById('kalshi-test-result');
    resultEl.textContent = 'Testing...';
    resultEl.style.color = 'var(--muted)';
    try {{
      const resp = await fetch('/api/kalshi/test-connection');
      const data = await resp.json();
      if (data.ok) {{
        const bal = data.data && data.data.balance != null ? ' ($' + (data.data.balance/100).toFixed(2) + ')' : '';
        resultEl.textContent = 'Connection successful' + bal;
        resultEl.style.color = 'var(--green)';
      }} else {{
        resultEl.textContent = 'Failed: ' + (typeof data.error === 'string' ? data.error : JSON.stringify(data.error));
        resultEl.style.color = 'var(--red)';
      }}
    }} catch(e) {{
      resultEl.textContent = 'Error: ' + e.message;
      resultEl.style.color = 'var(--red)';
    }}
  }}

  async function removeKalshiCreds() {{
    if (!confirm('Disconnect your Kalshi account? You will not be able to trade Kalshi markets until you reconnect.')) return;
    try {{
      await fetch('/api/kalshi/credentials', {{method: 'DELETE'}});
      location.reload();
    }} catch(e) {{}}
  }}

  function showKalshiForm() {{
    document.getElementById('kalshi-form').style.display = 'block';
    const conn = document.getElementById('kalshi-connected');
    if (conn) conn.style.display = 'none';
  }}

  function cancelKalshiForm() {{
    document.getElementById('kalshi-form').style.display = 'none';
    const conn = document.getElementById('kalshi-connected');
    if (conn) conn.style.display = 'block';
  }}

  // Expose to global so inline onclick handlers work
  window.saveCreds = saveCreds;
  window.testConnection = testConnection;
  window.removeCreds = removeCreds;
  window.showCredsForm = showCredsForm;
  window.cancelCredsForm = cancelCredsForm;
  window.saveKalshiCreds = saveKalshiCreds;
  window.testKalshiConnection = testKalshiConnection;
  window.removeKalshiCreds = removeKalshiCreds;
  window.showKalshiForm = showKalshiForm;
  window.cancelKalshiForm = cancelKalshiForm;
}})();
</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Legal Pages ─────────────────────────────────────────────────────

LEGAL_STYLE = """<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --blue:#58a6ff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,'Segoe UI',sans-serif; padding:32px; max-width:800px; margin:0 auto; }
  h1 { font-size:1.6em; margin-bottom:8px; }
  h2 { font-size:1.1em; margin-top:24px; margin-bottom:8px; color:var(--blue); }
  p, li { line-height:1.7; color:var(--muted); font-size:0.9em; margin-bottom:12px; }
  ul { padding-left:20px; }
  a { color:var(--blue); text-decoration:none; }
  .back { display:inline-block; margin-bottom:20px; font-size:0.85em; }
  .updated { color:var(--muted); font-size:0.75em; margin-bottom:24px; }
</style>"""

@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Terms of Service — CryptoEdge</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">&larr; Back to Dashboard</a>
<h1>Terms of Service</h1>
<p class="updated">Last updated: April 2026</p>

<h2>1. Acceptance of Terms</h2>
<p>By accessing CryptoEdge ("the Service"), you agree to be bound by these Terms of Service. If you do not agree, do not use the Service.</p>

<h2>2. Service Description</h2>
<p>CryptoEdge provides cryptocurrency market analysis using neural network ensembles trained on historical Binance data. The Service displays predictions, volatility metrics, and suspicious trade alerts for informational purposes only.</p>

<h2>3. No Financial Advice</h2>
<p><strong>The Service does not constitute financial advice, investment advice, trading advice, or any other sort of advice.</strong> You should not treat any of the Service's content as such. CryptoEdge does not recommend that any cryptocurrency should be bought, sold, or held by you. Nothing on this Service should be taken as an offer to buy, sell, or hold a cryptocurrency.</p>

<h2>4. No Guarantee of Accuracy</h2>
<p>Predictions, signals, and analysis are generated by machine learning models that are inherently probabilistic. Past accuracy does not guarantee future performance. The Service makes no warranty regarding the accuracy, completeness, or reliability of any information provided.</p>

<h2>5. Risk Acknowledgment</h2>
<p>Cryptocurrency trading involves substantial risk of loss and is not suitable for every investor. You acknowledge that:</p>
<ul>
<li>You may lose some or all of your invested capital</li>
<li>Past performance is not indicative of future results</li>
<li>The high degree of leverage in crypto trading can work against you as well as for you</li>
<li>You are solely responsible for any trading decisions you make</li>
</ul>

<h2>6. Account Security</h2>
<p>You are responsible for maintaining the confidentiality of your login credentials. You agree to notify us immediately of any unauthorized use of your account.</p>

<h2>7. Prohibited Use</h2>
<p>You may not: reverse-engineer the Service, redistribute data without permission, use the Service for market manipulation, or share access credentials.</p>

<h2>8. Limitation of Liability</h2>
<p>To the fullest extent permitted by law, CryptoEdge and its operators shall not be liable for any indirect, incidental, special, consequential, or punitive damages, including loss of profits, data, or funds, arising from your use of the Service.</p>

<h2>9. Changes to Terms</h2>
<p>We reserve the right to modify these terms at any time. Continued use of the Service after changes constitutes acceptance of the new terms.</p>
</body></html>""")


@app.get("/disclaimer", response_class=HTMLResponse)
async def disclaimer_page():
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Risk Disclaimer — CryptoEdge</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">&larr; Back to Dashboard</a>
<h1>Risk Disclaimer</h1>
<p class="updated">Last updated: April 2026</p>

<div style="background:#1c1200;border:2px solid #d29922;border-radius:10px;padding:20px;margin-bottom:24px;">
<p style="color:#d29922;font-weight:700;font-size:1em;margin-bottom:8px;">&#9888; IMPORTANT WARNING</p>
<p style="color:#e6edf3;">Trading cryptocurrencies carries a high level of risk and may not be suitable for all investors. Before deciding to trade, you should carefully consider your investment objectives, level of experience, and risk appetite. <strong>The possibility exists that you could sustain a loss of some or all of your initial investment.</strong> You should not invest money that you cannot afford to lose.</p>
</div>

<h2>Model Limitations</h2>
<p>The neural network ensemble predictions displayed on this dashboard are based on historical price patterns. These models:</p>
<ul>
<li>Have been trained on historical data that may not reflect future market conditions</li>
<li>Cannot predict black swan events, regulatory changes, or market manipulation</li>
<li>Show directional accuracy of approximately 50-55%, which is marginally above random chance</li>
<li>Are retrained periodically and past accuracy metrics may not reflect current model performance</li>
</ul>

<h2>Data Sources</h2>
<p>Price data is sourced from Binance via their public API. Suspicious trade data is sourced from Polymarket's public CLOB API. We do not guarantee data availability, accuracy, or timeliness.</p>

<h2>Not Regulated Financial Product</h2>
<p>CryptoEdge is an analytics tool, not a regulated financial product. It is not registered with any financial regulatory authority. The operators are not licensed financial advisors.</p>

<h2>Your Responsibility</h2>
<p>You are solely responsible for your own trading decisions. Always do your own research (DYOR) and consider consulting a licensed financial advisor before making investment decisions.</p>
</body></html>""")


# ─── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Authenticate via gateway SSO headers (same as HTTP requests)
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    headers = ws.headers
    authed = False
    if _sso_secret and hmac.compare_digest(headers.get("x-gateway-secret", ""), _sso_secret):
        if headers.get("x-gateway-user-id") and headers.get("x-gateway-user-email"):
            authed = True
    # Localhost bypass for bots — only when explicitly enabled via env var
    if not authed and os.getenv("DEV_LOCALHOST_BYPASS") == "1":
        client_host = ws.client.host if ws.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            authed = True
    if not authed:
        await ws.close(code=4001, reason="Not authenticated")
        return
    await ws.accept()
    async with _ws_lock:
        connected_ws.add(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "type": "init",
            "data": {ticker: serialize_asset(ticker) for ticker in asset_state},
        }))
        # Keep alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        async with _ws_lock:
            connected_ws.discard(ws)
    except Exception as e:
        print(f"  [WS] Connection error: {type(e).__name__}: {e}")
        async with _ws_lock:
            connected_ws.discard(ws)


# ===================================================================
# LONG-TERM HOLDING — analytics, portfolio, DCA, rebalance
# ===================================================================

@app.get("/api/long-term/snapshot")
async def long_term_snapshot(request: Request):
    """All-asset snapshot: cycle phase, drawdown, vol regime, MVRV, risk-off."""
    snaps = await asyncio.to_thread(lt.all_snapshots)
    return {"assets": snaps, "as_of": datetime.now(timezone.utc).isoformat()}


@app.get("/api/long-term/asset/{ticker}")
async def long_term_asset(ticker: str, request: Request):
    ticker = ticker.upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=404, detail="Unknown ticker")
    snap = await asyncio.to_thread(lt.asset_snapshot, ticker)
    # Include a slim daily price series for charting (last 365 days only).
    dates, closes = await asyncio.to_thread(lt.get_daily_closes, ticker, 365)
    snap["series"] = {"dates": dates, "closes": [float(c) for c in closes]}
    # Plus a couple of on-chain series if we have them.
    if ticker in lt.ONCHAIN_COVERED:
        for m in ("CapMrktCurUSD", "CapRealUSD", "AdrActCnt"):
            d, v = await asyncio.to_thread(lt.get_onchain_series, ticker, m, 365)
            if len(v) > 0:
                snap.setdefault("onchain", {})[m] = {"dates": d, "values": [float(x) for x in v]}
    return snap


# ── Cycle indicators ────────────────────────────────────────────────────────

@app.get("/api/long-term/indicators")
async def list_indicators(request: Request):
    """Run every cycle indicator across every tracked asset.
    Each result has {name, ticker, value, signal, description, threshold,
    source, extras}."""
    results = await asyncio.to_thread(ind.evaluate_all)
    return {"indicators": results, "as_of": datetime.now(timezone.utc).isoformat()}


@app.get("/api/long-term/indicators/composite/{ticker}")
async def indicator_composite(ticker: str, request: Request):
    ticker = ticker.upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=404, detail="Unknown ticker")
    score = await asyncio.to_thread(ind.composite_score, ticker)
    return {"ticker": ticker, **score}


# ── Derivatives ─────────────────────────────────────────────────────────────

@app.get("/api/long-term/derivatives")
async def derivatives_overview(request: Request):
    """Per-ticker funding + OI + basis snapshots, plus the funding composite."""
    out = []
    for ticker in deriv.PERP_SYMBOLS.keys():
        funding = await asyncio.to_thread(deriv.funding_snapshot, ticker)
        oi = await asyncio.to_thread(deriv.oi_snapshot, ticker)
        out.append({
            "ticker": ticker,
            "funding": funding.to_dict() if funding else None,
            "open_interest": oi.to_dict() if oi else None,
        })
    composite = await asyncio.to_thread(deriv.funding_composite)
    return {"assets": out, "funding_composite": composite,
            "as_of": datetime.now(timezone.utc).isoformat()}


@app.post("/api/long-term/derivatives/refresh")
async def derivatives_force_refresh(request: Request):
    user = _get_session_user(request)
    client_host = request.client.host if request.client else ""
    if user is None and client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await asyncio.to_thread(deriv.refresh_all_derivatives)


# ── Macro overlay ───────────────────────────────────────────────────────────

@app.get("/api/long-term/macro")
async def macro_overview_endpoint(request: Request):
    overview = await asyncio.to_thread(macro.macro_overview)
    regime = await asyncio.to_thread(macro.macro_regime)
    return {"series": overview, "regime": regime,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "fred_configured": bool(os.environ.get("FRED_API_KEY"))}


@app.post("/api/long-term/macro/refresh")
async def macro_force_refresh(request: Request):
    user = _get_session_user(request)
    client_host = request.client.host if request.client else ""
    if user is None and client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await asyncio.to_thread(macro.refresh_all_macro)


# ── Backtests ───────────────────────────────────────────────────────────────

@app.get("/api/long-term/backtests")
async def list_backtests(request: Request):
    rows = await asyncio.to_thread(bt.latest_results)
    return {"results": rows, "as_of": datetime.now(timezone.utc).isoformat()}


@app.post("/api/long-term/backtests/run")
async def run_backtests(request: Request):
    user = _get_session_user(request)
    client_host = request.client.host if request.client else ""
    if user is None and client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await asyncio.to_thread(bt.run_all)


@app.post("/api/long-term/refresh")
async def long_term_force_refresh(request: Request):
    """Force a refresh of daily bars + on-chain metrics. Localhost or admin only."""
    user = _get_session_user(request)
    client_host = request.client.host if request.client else ""
    if user is None and client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = await asyncio.to_thread(lt.refresh_all)
    return result


# ── Holdings ────────────────────────────────────────────────────────────────

@app.get("/api/long-term/holdings")
async def get_holdings(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    lots = db.get_holdings(user["id"])
    rollup = db.get_holdings_rollup(user["id"])
    # Mark each lot with current value + unrealized P&L.
    enriched_lots = []
    snaps = {s["ticker"]: s for s in lt.all_snapshots()}
    for lot in lots:
        price = (snaps.get(lot["ticker"]) or {}).get("price")
        cur_val = float(lot["qty"]) * price if price else None
        cost = float(lot["qty"]) * float(lot["cost_basis"])
        pnl = (cur_val - cost) if cur_val is not None else None
        # Long-term holding = >= 365 days. Used for capital-gains hinting.
        try:
            held_days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(lot["acquired_at"]).date()).days
        except Exception:
            held_days = None
        enriched_lots.append({
            **dict(lot), "current_price": price, "current_value": cur_val,
            "unrealized_pnl": pnl,
            "long_term_eligible": (held_days is not None and held_days >= 365),
            "held_days": held_days,
        })
    # Roll up with current price too.
    enriched_rollup = []
    for r in rollup:
        price = (snaps.get(r["ticker"]) or {}).get("price")
        cur_val = r["qty"] * price if price else None
        enriched_rollup.append({
            **r, "current_price": price, "current_value": cur_val,
            "unrealized_pnl": (cur_val - r["cost_total"]) if cur_val is not None else None,
        })
    return {"lots": enriched_lots, "rollup": enriched_rollup}


@app.post("/api/long-term/holdings")
async def add_holding(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    ticker = str(payload.get("ticker", "")).upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=400, detail="Unknown ticker")
    try:
        qty = float(payload.get("qty"))
        cost_basis = float(payload.get("cost_basis"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="qty and cost_basis must be numeric")
    if qty <= 0 or cost_basis <= 0:
        raise HTTPException(status_code=400, detail="qty and cost_basis must be positive")
    acquired_at = str(payload.get("acquired_at") or datetime.now(timezone.utc).date().isoformat())
    note = str(payload.get("note", ""))[:200]
    holding_id = db.add_holding(user["id"], ticker, qty, cost_basis, acquired_at, note)
    return {"id": holding_id}


@app.delete("/api/long-term/holdings/{holding_id}")
async def delete_holding(holding_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.remove_holding(user["id"], holding_id)
    return {"ok": True}


# ── Target weights ──────────────────────────────────────────────────────────

@app.get("/api/long-term/targets")
async def get_targets(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_target_weights(user["id"])
    return {"targets": [dict(r) for r in rows]}


@app.post("/api/long-term/targets")
async def set_targets(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    targets = payload.get("targets", [])
    if not isinstance(targets, list):
        raise HTTPException(status_code=400, detail="targets must be a list")
    # Validate sum is roughly 1.0 (allow some slack for cash). Not enforced strictly.
    total = sum(float(t.get("weight", 0)) for t in targets)
    if total > 1.01:
        raise HTTPException(status_code=400, detail=f"weights sum to {total:.3f}, must be <= 1.0")
    for t in targets:
        ticker = str(t.get("ticker", "")).upper()
        if ticker not in lt.TICKER_MAP:
            continue
        weight = max(0.0, min(1.0, float(t.get("weight", 0))))
        band = max(0.0, min(0.5, float(t.get("drift_band", 0.05))))
        if weight == 0:
            db.remove_target_weight(user["id"], ticker)
        else:
            db.set_target_weight(user["id"], ticker, weight, band)
    return {"ok": True, "total_weight": total}


# ── Rebalance ───────────────────────────────────────────────────────────────

@app.get("/api/long-term/rebalance")
async def get_rebalance(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rollup = db.get_holdings_rollup(user["id"])
    targets = db.get_target_weights(user["id"])
    if not targets:
        return {"error": "no target weights set", "legs": []}
    # Use the smallest user-set drift band so the most sensitive ticker triggers first.
    drift_band = min((float(t["drift_band"]) for t in targets), default=0.05)
    plan = await asyncio.to_thread(
        lt.rebalance_plan,
        [{"ticker": r["ticker"], "qty": r["qty"]} for r in rollup],
        [{"ticker": t["ticker"], "weight": t["weight"]} for t in targets],
        drift_band,
    )
    return plan


# ── DCA ─────────────────────────────────────────────────────────────────────

@app.get("/api/long-term/dca")
async def get_dca(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_dca_schedules(user["id"])
    return {"schedules": [dict(r) for r in rows]}


@app.post("/api/long-term/dca")
async def upsert_dca(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    ticker = str(payload.get("ticker", "")).upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=400, detail="Unknown ticker")
    frequency = str(payload.get("frequency", "weekly"))
    if frequency not in ("daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="frequency must be daily/weekly/monthly")
    try:
        amount = float(payload.get("base_amount_usd", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="base_amount_usd must be numeric")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="base_amount_usd must be positive")
    use_mult = bool(payload.get("use_multiplier", True))
    active = bool(payload.get("active", True))
    db.upsert_dca_schedule(user["id"], ticker, frequency, amount, use_mult, active)
    return {"ok": True}


@app.delete("/api/long-term/dca/{ticker}")
async def delete_dca(ticker: str, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.remove_dca_schedule(user["id"], ticker.upper())
    return {"ok": True}


@app.get("/api/long-term/dca/recommendations")
async def dca_recommendations(request: Request):
    """Today's suggested DCA amounts for each scheduled asset.
    Returns the cycle-aware suggestion alongside the user's base amount so they
    can override if they disagree."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    schedules = db.get_dca_schedules(user["id"])
    out = []
    for s in schedules:
        if not s["active"]:
            continue
        base = float(s["base_amount_usd"])
        if s["use_multiplier"]:
            plan = await asyncio.to_thread(lt.dca_recommendation, s["ticker"], base)
            out.append(plan.to_dict())
        else:
            out.append({
                "ticker": s["ticker"], "base_amount_usd": base, "multiplier": 1.0,
                "suggested_amount_usd": base, "reason": "fixed (multiplier disabled)",
                "phase": None, "mayer": None, "dd": None, "vol_regime": None,
            })
    return {"recommendations": out, "as_of": datetime.now(timezone.utc).isoformat()}


# ── Long-term alerts ────────────────────────────────────────────────────────

@app.get("/api/long-term/alerts")
async def list_long_term_alerts(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_long_term_alerts(user["id"])
    return {"alerts": [dict(r) for r in rows]}


@app.post("/api/long-term/alerts")
async def upsert_long_term_alert(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    ticker = str(payload.get("ticker", "")).upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=400, detail="Unknown ticker")
    alert_type = str(payload.get("alert_type", ""))
    if alert_type not in ("drawdown", "mvrv_high", "mvrv_low", "vol_regime", "risk_off"):
        raise HTTPException(status_code=400, detail="invalid alert_type")
    threshold = payload.get("threshold")
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="threshold must be numeric")
    db.upsert_long_term_alert(user["id"], ticker, alert_type, threshold)
    return {"ok": True}


@app.delete("/api/long-term/alerts/{ticker}/{alert_type}")
async def delete_long_term_alert(ticker: str, alert_type: str, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.remove_long_term_alert(user["id"], ticker.upper(), alert_type)
    return {"ok": True}


# ===================================================================
# AUTO-EXECUTION — Phase 2
# ===================================================================
# Every order route here is gated by Fernet-encrypted creds + the safety
# gauntlet in execution.py. Default is dry-run; flipping that is a
# deliberate user action that requires confirming each safety field.

@app.post("/api/exchanges/credentials")
async def save_exchange_creds(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _require_feature(user, "exchange_connect")
    payload = await request.json()
    exchange = str(payload.get("exchange", "")).lower()
    if exchange not in ("coinbase", "kraken"):
        raise HTTPException(status_code=400, detail="exchange must be coinbase or kraken")
    # Wealth-only: multiple exchanges configured at once.
    if not billing_mod.feature_allowed(user["tier"], "multi_exchange"):
        existing = xch.configured_exchanges(user["id"])
        if existing and existing != [exchange]:
            raise HTTPException(
                status_code=402,
                detail="Multi-exchange routing requires the Wealth tier. "
                       "Disconnect the existing exchange to switch.",
            )
    if exchange == "coinbase":
        api_key = str(payload.get("api_key", "")).strip()
        pem = str(payload.get("private_key_pem", "")).strip()
        if not api_key or "BEGIN" not in pem:
            raise HTTPException(status_code=400, detail="api_key + private_key_pem required for Coinbase")
        creds = {"api_key": api_key, "private_key_pem": pem}
    else:
        api_key = str(payload.get("api_key", "")).strip()
        secret = str(payload.get("secret", "")).strip()
        if not api_key or not secret:
            raise HTTPException(status_code=400, detail="api_key + secret required for Kraken")
        creds = {"api_key": api_key, "secret": secret}
    # Validate by trying to instantiate the adapter.
    try:
        if exchange == "coinbase":
            adapter = xch.CoinbaseAdapter(creds["api_key"], creds["private_key_pem"])
        else:
            adapter = xch.KrakenAdapter(creds["api_key"], creds["secret"])
        ok, msg = await asyncio.to_thread(adapter.test_connection)
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    xch.save_exchange_credentials(user["id"], exchange, creds)
    return {"ok": True, "exchange": exchange, "message": msg}


@app.delete("/api/exchanges/credentials/{exchange}")
async def delete_exchange_creds(exchange: str, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    xch.delete_exchange_credentials(user["id"], exchange.lower())
    return {"ok": True}


@app.get("/api/exchanges/configured")
async def list_configured_exchanges(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"exchanges": xch.configured_exchanges(user["id"])}


@app.get("/api/exchanges/test/{exchange}")
async def test_exchange_connection(exchange: str, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    adapter = xch.get_adapter(user["id"], exchange.lower())
    if not adapter:
        return JSONResponse({"error": "not configured"}, status_code=404)
    ok, msg = await asyncio.to_thread(adapter.test_connection)
    return {"ok": ok, "message": msg}


@app.get("/api/exchanges/balances")
async def get_exchange_balances(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    out = {}
    for name in xch.configured_exchanges(user["id"]):
        adapter = xch.get_adapter(user["id"], name)
        if not adapter:
            continue
        try:
            balances = await asyncio.to_thread(adapter.get_balances)
            out[name] = [b.to_dict() for b in balances]
        except Exception as e:
            out[name] = {"error": str(e)}
    return {"balances": out}


# ── Safety limits ───────────────────────────────────────────────────────────

@app.get("/api/long-term/safety")
async def get_safety(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return exec_mod.get_safety_for(user["id"])


@app.post("/api/long-term/safety")
async def set_safety(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json() or {}
    # Free users can read + change everything EXCEPT dry_run = False
    # (turning off dry-run is the paywall — it's what makes execution real).
    if payload.get("dry_run") is False and not billing_mod.feature_allowed(user["tier"], "live_execution"):
        raise HTTPException(
            status_code=402,
            detail="Disabling dry-run requires the Pro tier. Free users can preview + simulate as much as they like.",
        )
    return exec_mod.update_safety_for(user["id"], payload)


# ── Execution ──────────────────────────────────────────────────────────────

@app.get("/api/long-term/executions")
async def list_executions(request: Request, limit: int = 100):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_executions(user["id"], limit=min(max(1, limit), 500))
    return {"executions": [dict(r) for r in rows]}


@app.get("/api/long-term/executions/preview")
async def preview_executions(request: Request):
    """What would the executor do right now? Dry-run preview of every active
    DCA schedule for this user."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    preview = await asyncio.to_thread(exec_mod.preview_dca, user["id"])
    return {"preview": preview}


@app.post("/api/long-term/executions/dca/{ticker}")
async def run_dca_now(ticker: str, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ticker = ticker.upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=404, detail="Unknown ticker")
    decision = await asyncio.to_thread(exec_mod.execute_dca_now, user["id"], ticker)
    return decision.to_dict()


@app.post("/api/long-term/executions/rebalance")
async def run_rebalance_now(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    decisions = await asyncio.to_thread(exec_mod.execute_rebalance_now, user["id"])
    return {"decisions": [d.to_dict() for d in decisions]}


# ===================================================================
# TAX — Phase 3 (lot selection, dispositions, harvest, Form 8949)
# ===================================================================

@app.get("/api/long-term/tax/settings")
async def get_tax_settings_api(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return tax_mod.get_tax_settings(user["id"])


@app.post("/api/long-term/tax/settings")
async def set_tax_settings_api(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    return tax_mod.update_tax_settings(user["id"], payload or {})


@app.get("/api/long-term/tax/lots")
async def list_open_lots(request: Request, ticker: str | None = None):
    """Open lots — qty_original, qty_consumed, qty_remaining, cost basis,
    acquired_at. Lets the UI render the lot picker."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    lots = await asyncio.to_thread(tax_mod.open_lots, user["id"], ticker)
    return {"lots": [l.to_dict() for l in lots]}


@app.get("/api/long-term/tax/preview-sell")
async def preview_sell_api(request: Request, ticker: str, qty: float,
                           method: str = "HIFO", sell_price: float | None = None):
    """Hypothetical sale — show which lots would be consumed and the LT/ST
    split. Nothing is persisted. The UI calls this on every keystroke so the
    user can compare methods side by side."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if ticker.upper() not in lt.TICKER_MAP:
        raise HTTPException(status_code=400, detail="Unknown ticker")
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")
    try:
        preview = await asyncio.to_thread(
            tax_mod.preview_sell, user["id"], ticker, float(qty), method, sell_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return preview.to_dict()


@app.post("/api/long-term/tax/dispositions")
async def record_disposition_api(request: Request):
    """Record a real sale. Use this when the user reports an off-platform
    sell, or when the executor confirms a fill (execution_id link prevents
    double-records)."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    ticker = str(payload.get("ticker", "")).upper()
    if ticker not in lt.TICKER_MAP:
        raise HTTPException(status_code=400, detail="Unknown ticker")
    try:
        qty = float(payload.get("qty"))
        sell_price = float(payload.get("sell_price"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="qty + sell_price must be numeric")
    if qty <= 0 or sell_price <= 0:
        raise HTTPException(status_code=400, detail="qty + sell_price must be positive")
    method = payload.get("method")
    sell_date = payload.get("sell_date")
    exchange = payload.get("exchange", "manual")
    notes = str(payload.get("notes", ""))[:500]
    try:
        result = await asyncio.to_thread(
            tax_mod.record_disposition, user["id"], ticker, qty, sell_price,
            method, sell_date, exchange, None, notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.get("/api/long-term/tax/dispositions")
async def list_dispositions_api(request: Request, limit: int = 100):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    dispositions = await asyncio.to_thread(
        tax_mod.list_dispositions, user["id"], min(max(1, limit), 500),
    )
    return {"dispositions": dispositions}


@app.get("/api/long-term/tax/harvest")
async def harvest_opportunities(request: Request,
                                 min_loss_usd: float | None = None,
                                 min_age_days: int | None = None):
    """Scan open lots for tax-loss harvesting candidates."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    opps = await asyncio.to_thread(
        tax_mod.find_harvest_opportunities, user["id"], min_loss_usd, min_age_days,
    )
    total_loss = sum(o.unrealized_loss_usd for o in opps)
    total_save = sum(o.estimated_tax_save_usd for o in opps)
    return {
        "opportunities": [o.to_dict() for o in opps],
        "total_unrealized_loss_usd": round(total_loss, 2),
        "estimated_total_tax_save_usd": round(total_save, 2),
    }


@app.post("/api/long-term/tax/harvest/execute")
async def execute_harvest_api(request: Request):
    """One-click TLH: place sells for the selected lots through the same
    safety gauntlet the executor uses. Fill poller picks up the fills and
    creates tax dispositions automatically.

    Body: {holding_ids: [1, 5, 12]}. Use the IDs returned by
    /api/long-term/tax/harvest. If omitted, harvests every opportunity.
    """
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _require_feature(user, "tax_harvest_execute")
    payload = await request.json()
    holding_ids = payload.get("holding_ids")
    if not holding_ids:
        # Default to "all current opportunities".
        opps = await asyncio.to_thread(tax_mod.find_harvest_opportunities, user["id"])
        holding_ids = [o.holding_id for o in opps]
    if not holding_ids:
        return {"decisions": [], "message": "no harvest opportunities"}
    decisions = await asyncio.to_thread(
        exec_mod.execute_harvest_now, user["id"], holding_ids,
    )
    return {"decisions": [d.to_dict() for d in decisions]}


@app.get("/api/long-term/tax/summary/{year}")
async def tax_summary(year: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if year < 2009 or year > 2100:
        raise HTTPException(status_code=400, detail="invalid year")
    return await asyncio.to_thread(tax_mod.realized_pnl_summary, user["id"], year)


@app.get("/api/long-term/tax/export/{year}")
async def tax_export(year: int, request: Request):
    """Form 8949 CSV download. One row per lot consumption, split into Part I
    (short-term) and Part II (long-term)."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _require_feature(user, "tax_form_8949")
    csv_text = await asyncio.to_thread(tax_mod.export_form_8949, user["id"], year)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="form-8949-{year}.csv"',
        },
    )


# ===================================================================
# PUSH NOTIFICATIONS — Phase 5.1
# ===================================================================

@app.get("/api/push/vapid-key")
async def get_vapid_key(request: Request):
    """Server public key used by pushManager.subscribe()."""
    try:
        return {"key": push_mod.get_vapid_public_key_b64()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    sub = payload.get("subscription") or {}
    endpoint = str(sub.get("endpoint", "")).strip()
    keys = sub.get("keys", {})
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="endpoint + keys.p256dh + keys.auth required")
    ua = request.headers.get("user-agent", "")[:200]
    sub_id = db.upsert_push_subscription(user["id"], endpoint, p256dh, auth, ua)
    return {"ok": True, "id": sub_id}


@app.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    endpoint = str(payload.get("endpoint", "")).strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="endpoint required")
    db.delete_push_subscription(user["id"], endpoint)
    return {"ok": True}


@app.get("/api/push/subscriptions")
async def list_push_subscriptions(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    subs = db.get_push_subscriptions(user["id"])
    return {"subscriptions": [
        {"id": s["id"], "endpoint": s["endpoint"][:80] + "…",
         "user_agent": s["user_agent"], "created_at": s["created_at"]} for s in subs
    ]}


@app.post("/api/push/test")
async def push_test(request: Request):
    """Send a test push to all of this user's subscriptions."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = await asyncio.to_thread(
        push_mod.notify_user, user["id"],
        "CryptoEdge test",
        "Push notifications are wired up. You'll get alerts for cycle indicators, DCA fills, and the circuit breaker.",
        "/long-term",
        "test",
    )
    return result


@app.get("/api/notifications/pending")
async def pending_notifications(request: Request):
    """Service worker fetches this on each `push` event to render a
    notification with content. Marks all returned notifications as
    delivered."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_pending_notifications(user["id"], limit=5)
    if rows:
        db.mark_notifications_delivered(user["id"], [r["id"] for r in rows])
    return {"notifications": [dict(r) for r in rows]}


# ── PWA assets ──────────────────────────────────────────────────────────────

@app.get("/manifest.webmanifest")
async def webmanifest():
    """PWA manifest. Lets the browser offer "Add to home screen".
    Standalone display gives the app a chromeless UI on mobile."""
    return JSONResponse({
        "name": "CryptoEdge — Long-term",
        "short_name": "CryptoEdge",
        "description": "Cycle-aware DCA, on-chain analytics, auto-execution, tax-optimal selling.",
        "start_url": "/long-term",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0a0d12",
        "theme_color": "#0a0d12",
        "icons": [
            {"src": "/favicon.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/favicon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.get("/service-worker.js")
async def service_worker():
    """Service worker. Stays in the page root so it can intercept network
    requests for any path under /. Caches the long-term page shell + handles
    push events by fetching the pending-notification queue."""
    sw_js = r"""
const CACHE = 'cryptoedge-v1';
const SHELL = ['/long-term', '/favicon.png', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    try { await cache.addAll(SHELL); } catch(err) { console.warn('shell cache:', err); }
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    for (const n of names) if (n !== CACHE) await caches.delete(n);
    self.clients.claim();
  })());
});

// Network-first for everything (we want fresh data); fall back to cache for
// the page shell when offline.
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  e.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok && SHELL.includes(new URL(req.url).pathname)) {
        const cache = await caches.open(CACHE);
        cache.put(req, fresh.clone());
      }
      return fresh;
    } catch(err) {
      const cached = await caches.match(req);
      if (cached) return cached;
      throw err;
    }
  })());
});

// Push handler — fetch the pending notification queue and render up to 5.
self.addEventListener('push', (e) => {
  e.waitUntil((async () => {
    let notifs = [];
    try {
      const r = await fetch('/api/notifications/pending', {credentials: 'include'});
      if (r.ok) notifs = (await r.json()).notifications || [];
    } catch(err) {}
    if (!notifs.length) {
      // No queued content — show a generic "open the app" prompt.
      return self.registration.showNotification('CryptoEdge', {
        body: 'New activity. Tap to open.',
        icon: '/favicon.png',
        badge: '/favicon.png',
        tag: 'generic',
      });
    }
    for (const n of notifs) {
      await self.registration.showNotification(n.title, {
        body: n.body,
        icon: '/favicon.png',
        badge: '/favicon.png',
        tag: n.tag || ('n-' + n.id),
        data: {url: n.url || '/long-term'},
        renotify: !!n.tag,
      });
    }
  })());
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/long-term';
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of all) {
      if (c.url.includes(url)) { c.focus(); return; }
    }
    self.clients.openWindow(url);
  })());
});
"""
    return Response(content=sw_js, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


# ===================================================================
# STRATEGIES — Phase 4
# ===================================================================

@app.get("/api/long-term/strategies")
async def list_strategies(request: Request):
    """User's own strategies, all visibilities."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    items = await asyncio.to_thread(strat_mod.list_user_strategies, user["id"])
    return {"strategies": items}


@app.get("/api/long-term/strategies/marketplace")
async def marketplace(request: Request, limit: int = 50):
    """All public strategies, newest first."""
    items = await asyncio.to_thread(strat_mod.list_public_strategies, min(max(1, limit), 100))
    return {"strategies": items}


@app.get("/api/long-term/strategies/leaderboard")
async def leaderboard_api(request: Request, limit: int = 25):
    rows = await asyncio.to_thread(strat_mod.leaderboard, min(max(1, limit), 100))
    return {"leaderboard": rows}


@app.get("/api/long-term/strategies/{strategy_id}")
async def get_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    uid = user["id"] if user else None
    s = await asyncio.to_thread(strat_mod.get_strategy, strategy_id, uid)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found or private")
    return s


@app.post("/api/long-term/strategies")
async def create_strategy_api(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    rules = payload.get("rules") or {}
    visibility = str(payload.get("visibility", "private"))
    if visibility == "public":
        _require_feature(user, "strategy_publish")
    try:
        strategy = strat_mod.Strategy.from_dict(rules)
        sid = await asyncio.to_thread(
            strat_mod.create_strategy, user["id"], strategy, None, visibility,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": sid}


@app.put("/api/long-term/strategies/{strategy_id}")
async def update_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    rules = payload.get("rules") or {}
    visibility = payload.get("visibility")
    strategy = strat_mod.Strategy.from_dict(rules)
    ok = await asyncio.to_thread(
        strat_mod.update_strategy, user["id"], strategy_id, strategy, visibility,
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Not owner or strategy not found")
    return {"ok": True}


@app.delete("/api/long-term/strategies/{strategy_id}")
async def delete_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = await asyncio.to_thread(strat_mod.delete_strategy, user["id"], strategy_id)
    if not ok:
        raise HTTPException(status_code=403, detail="Not owner or strategy not found")
    return {"ok": True}


@app.post("/api/long-term/strategies/{strategy_id}/backtest")
async def run_strategy_backtest(strategy_id: int, request: Request):
    """Backtest the strategy and persist the result. Owner-only (or public)."""
    user = _get_session_user(request)
    uid = user["id"] if user else None
    s = await asyncio.to_thread(strat_mod.get_strategy, strategy_id, uid)
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found or private")
    result = await asyncio.to_thread(strat_mod.run_and_save_backtest, strategy_id)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


@app.post("/api/long-term/strategies/{strategy_id}/fork")
async def fork_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    new_name = payload.get("name")
    new_id = await asyncio.to_thread(
        strat_mod.fork_strategy, user["id"], strategy_id, new_name,
    )
    if not new_id:
        raise HTTPException(status_code=404, detail="source strategy not found or not public")
    return {"id": new_id}


@app.post("/api/long-term/strategies/{strategy_id}/follow")
async def follow_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = await asyncio.to_thread(strat_mod.follow_strategy, user["id"], strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="strategy not public")
    return {"ok": True}


@app.delete("/api/long-term/strategies/{strategy_id}/follow")
async def unfollow_strategy_api(strategy_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await asyncio.to_thread(strat_mod.unfollow_strategy, user["id"], strategy_id)
    return {"ok": True}


# ── Live subscriptions ──────────────────────────────────────────────────────

@app.post("/api/long-term/strategies/{strategy_id}/subscribe")
async def subscribe_strategy(strategy_id: int, request: Request):
    """Subscribe to a public strategy. The subscription ticker evaluates the
    rules every 5 min and routes the resulting buys through `_evaluate_leg`
    (so dry-run / caps / circuit-breaker still apply)."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    s = await asyncio.to_thread(strat_mod.get_strategy, strategy_id, user["id"])
    if not s:
        raise HTTPException(status_code=404, detail="Strategy not found")
    # Restrict subscription to public strategies OR the user's own.
    if s["visibility"] != "public" and s["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Cannot subscribe to private strategy")
    # Tier-based subscription cap.
    limit = billing_mod.subscription_limit(user["tier"])
    if limit is not None:
        current = db.get_strategy_subscriptions(user["id"])
        # Allow re-subscribe to an existing strategy (no new slot used).
        if not any(c["strategy_id"] == strategy_id for c in current):
            if len(current) >= limit:
                raise HTTPException(
                    status_code=402,
                    detail=f"Your {user['tier']} tier allows {limit} active subscription(s). "
                           f"Upgrade for more, or unsubscribe an existing one.",
                )
    # First run = now (so the user sees an immediate dry-run result).
    sub_id = await asyncio.to_thread(
        db.upsert_strategy_subscription, user["id"], strategy_id,
        datetime.now(timezone.utc).isoformat(),
    )
    return {"id": sub_id, "ok": True}


@app.get("/api/long-term/subscriptions")
async def list_subscriptions(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rows = db.get_strategy_subscriptions(user["id"])
    return {"subscriptions": [dict(r) for r in rows]}


@app.post("/api/long-term/subscriptions/{subscription_id}/pause")
async def pause_subscription(subscription_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    paused = bool(payload.get("paused", True))
    ok = db.set_strategy_subscription_paused(user["id"], subscription_id, paused)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"ok": True, "paused": paused}


@app.delete("/api/long-term/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: int, request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = db.delete_strategy_subscription(user["id"], subscription_id)
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    return {"ok": True}


@app.post("/api/long-term/subscriptions/{subscription_id}/run-now")
async def run_subscription_now(subscription_id: int, request: Request):
    """Manual trigger — useful for testing a fresh subscription."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    decisions = await asyncio.to_thread(
        exec_mod.execute_subscription_now, user["id"], subscription_id,
    )
    return {"decisions": [d.to_dict() for d in decisions]}


# ===================================================================
# BILLING — Stripe subscriptions + tier resolution
# ===================================================================

@app.get("/api/billing/tier")
async def get_billing_tier(request: Request):
    """Current tier + which features it unlocks. Free users see this to
    understand what an upgrade buys them."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "tier": user["tier"],
        "features": {
            k: billing_mod.feature_allowed(user["tier"], k)
            for k in billing_mod.FEATURE_TIERS.keys()
        },
        "subscription_limit": billing_mod.subscription_limit(user["tier"]),
        "billing_configured": billing_mod.billing_configured(),
    }


@app.post("/api/billing/checkout")
async def billing_checkout(request: Request):
    """Create a Stripe Checkout Session for the requested tier. Returns the
    `url` the client should redirect to. Free → Pro / Free → Wealth /
    Pro → Wealth all use the same flow."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not billing_mod.billing_configured():
        raise HTTPException(status_code=503, detail="Billing not configured on this deployment")
    payload = await request.json()
    tier = str(payload.get("tier", "")).lower()
    if tier not in ("pro", "wealth"):
        raise HTTPException(status_code=400, detail="tier must be pro or wealth")
    success = os.environ.get("STRIPE_SUCCESS_URL", "").strip() or "https://crypto.narve.ai/long-term?upgraded=1"
    cancel = os.environ.get("STRIPE_CANCEL_URL", "").strip() or "https://crypto.narve.ai/pricing"
    result = await asyncio.to_thread(
        billing_mod.create_checkout_session,
        user["id"], user["email"], tier, success, cancel,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"url": result.get("url"), "id": result.get("id")}


@app.post("/api/billing/portal")
async def billing_portal(request: Request):
    """Create a Stripe Customer Portal session — lets the user upgrade,
    downgrade, or cancel without going back through Checkout."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return_url = "https://crypto.narve.ai/long-term"
    result = await asyncio.to_thread(
        billing_mod.create_billing_portal_session, user["id"], return_url,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"url": result.get("url")}


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    """Receives Stripe events. Signature-verified with the webhook secret.
    Idempotent — Stripe retries failed deliveries."""
    sig = request.headers.get("stripe-signature", "")
    body = await request.body()
    if not billing_mod.verify_webhook_signature(body, sig):
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid payload")
    result = await asyncio.to_thread(billing_mod.handle_webhook_event, event)
    return result


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    """Public pricing page. Three tiers, one CTA each. Free is "stay where
    you are"; Pro and Wealth open a Stripe Checkout Session."""
    user = _get_session_user(request)
    tier = user["tier"] if user else "free"
    badge = f'<span style="background:#3b82f6;color:#fff;padding:3px 10px;border-radius:12px;font-size:.75em;margin-left:8px">{tier.upper()}</span>' if user else ""
    return HTMLResponse(_pricing_html(tier, billing_mod.billing_configured(), badge))


def _pricing_html(current_tier: str, billing_on: bool, badge_html: str) -> str:
    pro_btn = ('<button id="buy-pro">Upgrade to Pro</button>' if billing_on
               else '<button disabled title="Billing not configured">Pro</button>')
    wealth_btn = ('<button id="buy-wealth">Upgrade to Wealth</button>' if billing_on
                  else '<button disabled title="Billing not configured">Wealth</button>')
    return r"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CryptoEdge — pricing</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0d12;color:#e6edf5}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px}
h1{font-size:1.8em;margin:0 0 6px}
.sub{color:#7d8a99;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:#131820;border:1px solid #222a36;border-radius:12px;padding:20px}
.card.featured{border-color:#3b82f6;box-shadow:0 0 0 1px #3b82f6}
.price{font-size:2em;font-weight:700;margin:8px 0}
.muted{color:#7d8a99}
ul{padding-left:18px;line-height:1.6;margin:14px 0}
button{background:#3b82f6;color:#fff;border:0;padding:10px 18px;border-radius:6px;cursor:pointer;font-size:.95em;width:100%;margin-top:8px}
button:disabled{background:#3b82f680;cursor:not-allowed}
button:hover{opacity:.85}
a{color:#3b82f6}
@media (max-width:700px){.wrap{padding:20px 12px}}
</style></head><body>
<div class="wrap">
<h1>Pricing __BADGE__</h1>
<div class="sub">Everything in the lower tier is included in the higher one. Annual plans are coming — for now, monthly.</div>

<div class="grid">
  <div class="card">
    <h2>Free</h2>
    <div class="price">$0<span class="muted" style="font-size:.5em">/mo</span></div>
    <ul>
      <li>All cycle indicators + on-chain data</li>
      <li>Portfolio + lot tracking</li>
      <li>Cycle-aware DCA recommendations</li>
      <li>1 strategy subscription</li>
      <li>Dry-run execution only</li>
      <li>Push notifications</li>
    </ul>
    <button disabled>Current tier</button>
  </div>

  <div class="card featured">
    <h2>Pro</h2>
    <div class="price">$25<span class="muted" style="font-size:.5em">/mo</span></div>
    <ul>
      <li>Everything in Free</li>
      <li><b>Live execution</b> on Coinbase or Kraken</li>
      <li><b>Tax-loss harvest</b> one-click execute</li>
      <li><b>Form 8949 CSV</b> export</li>
      <li>Publish strategies to the marketplace</li>
      <li>3 strategy subscriptions</li>
    </ul>
    __PRO_BTN__
  </div>

  <div class="card">
    <h2>Wealth</h2>
    <div class="price">$75<span class="muted" style="font-size:.5em">/mo</span></div>
    <ul>
      <li>Everything in Pro</li>
      <li>Both exchanges linked at once</li>
      <li>Unlimited strategy subscriptions</li>
      <li>Priority support</li>
    </ul>
    __WEALTH_BTN__
  </div>
</div>

<div style="margin-top:30px;text-align:center;color:#7d8a99;font-size:.9em">
  <a href="#" id="portal-btn">Manage existing subscription →</a>
</div>

<script>
async function checkout(tier){
  try {
    const r = await fetch('/api/billing/checkout', {
      method:'POST', credentials:'include',
      headers: {'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({tier})
    });
    if (!r.ok) { alert((await r.json()).detail || 'Checkout failed'); return; }
    const d = await r.json();
    if (d.url) window.location.href = d.url;
  } catch(e) { alert(e.message); }
}
const proBtn = document.getElementById('buy-pro');
if (proBtn) proBtn.onclick = () => checkout('pro');
const wealthBtn = document.getElementById('buy-wealth');
if (wealthBtn) wealthBtn.onclick = () => checkout('wealth');

document.getElementById('portal-btn').onclick = async (e) => {
  e.preventDefault();
  try {
    const r = await fetch('/api/billing/portal', {
      method:'POST', credentials:'include',
      headers: {'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
    });
    if (!r.ok) { alert((await r.json()).detail || 'No active subscription'); return; }
    const d = await r.json();
    if (d.url) window.location.href = d.url;
  } catch(err) { alert(err.message); }
};
</script>
</div></body></html>
""".replace("__BADGE__", badge_html).replace("__PRO_BTN__", pro_btn).replace("__WEALTH_BTN__", wealth_btn)


# ── Notification preferences (digest opt-in) ────────────────────────────────

@app.get("/api/preferences")
async def get_preferences(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    prefs = db.get_user_preferences(user["id"])
    if not prefs:
        return {
            "user_id": user["id"], "email": user["email"],
            "digest_enabled": True, "digest_day_of_week": 0,
            "last_digest_sent_at": None,
        }
    if not prefs.get("email"):
        prefs["email"] = user["email"]
    return prefs


@app.post("/api/preferences")
async def set_preferences(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    email = payload.get("email") or user["email"]
    digest_enabled = payload.get("digest_enabled")
    digest_dow = payload.get("digest_day_of_week")
    db.upsert_user_preferences(
        user["id"], email=email,
        digest_enabled=bool(digest_enabled) if digest_enabled is not None else None,
        digest_day_of_week=int(digest_dow) if digest_dow is not None else None,
    )
    return {"ok": True}


@app.post("/api/preferences/digest/test")
async def send_test_digest(request: Request):
    """Send a one-off digest to the user now. Useful for "make sure it works"
    after adjusting preferences."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    prefs = db.get_user_preferences(user["id"]) or {}
    email = prefs.get("email") or user["email"]
    if not email:
        raise HTTPException(status_code=400, detail="no email on file")
    ok = await asyncio.to_thread(digest_mod.send_digest_for_user, user["id"], email)
    return {"sent": ok}


# ── Onboarding ──────────────────────────────────────────────────────────────

ONBOARDING_STEPS = ["welcome", "jurisdiction", "exchange", "targets",
                    "strategy", "push", "done"]


@app.get("/api/onboarding/state")
async def onboarding_state(request: Request):
    """Returns the user's current onboarding step. The UI uses this to
    decide whether to show the wizard or skip straight to the dashboard."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = db.get_onboarding(user["id"])
    if not row:
        return {"step": "welcome", "completed": False, "steps": ONBOARDING_STEPS}
    return {
        "step": row["step"],
        "completed": bool(row["completed_at"]),
        "settings": json.loads(row.get("settings_json") or "{}"),
        "steps": ONBOARDING_STEPS,
    }


@app.post("/api/onboarding/advance")
async def onboarding_advance(request: Request):
    """Save the response for the current step and move to the next.
    Body: {step: "...", payload: {...}}. The server validates the step name
    and applies any side-effects (e.g. step=jurisdiction → upserts tax settings)."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = await request.json()
    step = str(payload.get("step", ""))
    if step not in ONBOARDING_STEPS:
        raise HTTPException(status_code=400, detail=f"unknown step: {step}")
    data = payload.get("payload") or {}

    # Persist per-step settings + apply side-effects.
    existing = db.get_onboarding(user["id"])
    settings = json.loads(existing["settings_json"]) if existing else {}
    settings[step] = data

    if step == "jurisdiction":
        # Pipe into the tax settings.
        try:
            tax_mod.update_tax_settings(user["id"], {
                "jurisdiction": data.get("jurisdiction", "US"),
                "default_lot_method": data.get("default_lot_method", "HIFO"),
            })
        except Exception as e:
            log.warning("onboarding jurisdiction side-effect failed: %s", e)
    elif step == "targets":
        # Save target weights into the existing tax/portfolio path.
        targets = data.get("targets") or []
        for t in targets:
            tk = str(t.get("ticker", "")).upper()
            if tk not in lt.TICKER_MAP:
                continue
            w = max(0.0, min(1.0, float(t.get("weight", 0))))
            if w > 0:
                db.set_target_weight(user["id"], tk, w, 0.05)
            else:
                db.remove_target_weight(user["id"], tk)
    elif step == "strategy":
        # Optional: subscribe to a leaderboard strategy.
        sid = data.get("strategy_id")
        if sid:
            try:
                db.upsert_strategy_subscription(
                    user["id"], int(sid),
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as e:
                log.warning("onboarding subscribe failed: %s", e)

    # Compute next step.
    idx = ONBOARDING_STEPS.index(step)
    next_step = ONBOARDING_STEPS[idx + 1] if idx + 1 < len(ONBOARDING_STEPS) else "done"
    completed = next_step == "done"
    db.upsert_onboarding(user["id"], next_step, json.dumps(settings), completed=completed)
    return {"step": next_step, "completed": completed}


@app.post("/api/onboarding/skip")
async def onboarding_skip(request: Request):
    """Skip onboarding entirely. Marks completed without setting anything."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.upsert_onboarding(user["id"], "done", "{}", completed=True)
    return {"ok": True}


# ── HTML page ───────────────────────────────────────────────────────────────

@app.get("/long-term", response_class=HTMLResponse)
async def long_term_page(request: Request):
    """Renders the long-term holding dashboard. Auth optional — read-only views
    don't require login, but holdings/DCA/targets do (UI gates them)."""
    return HTMLResponse(_long_term_html())


def _long_term_html() -> str:
    # Kept inline (single-file) to match the rest of the dashboard. The
    # frontend is intentionally vanilla JS — no build step.
    return r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0d12">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="CryptoEdge">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/favicon.png">
<title>CryptoEdge — Long-term Holding</title>
<style>
:root{--bg:#0a0d12;--card:#131820;--card2:#1a2029;--muted:#7d8a99;--text:#e6edf5;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#eab308;--purple:#a855f7;--border:#222a36}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro',sans-serif;background:var(--bg);color:var(--text);line-height:1.4}
.wrap{max-width:1400px;margin:0 auto;padding:20px}
h1{margin:0 0 4px;font-size:1.5em}h2{margin:24px 0 12px;font-size:1.15em;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;font-size:.85em}
.sub{color:var(--muted);font-size:.9em;margin-bottom:16px}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--border);margin:16px 0}
.tab{padding:10px 14px;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;font-size:.95em}
.tab.active{color:var(--text);border-bottom-color:var(--blue)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.card h3{margin:0 0 8px;font-size:1em}
.row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:.9em}
.row .l{color:var(--muted)}.row .v{font-variant-numeric:tabular-nums;font-weight:500}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;font-weight:600;text-transform:uppercase;letter-spacing:.3px}
.p-capitulation{background:rgba(34,197,94,.18);color:var(--green)}
.p-deep-bear{background:rgba(34,197,94,.18);color:var(--green)}
.p-accumulation{background:rgba(59,130,246,.18);color:var(--blue)}
.p-neutral{background:rgba(125,138,153,.18);color:var(--muted)}
.p-expansion{background:rgba(234,179,8,.18);color:var(--yellow)}
.p-euphoria{background:rgba(239,68,68,.18);color:var(--red)}
.p-warming-up{background:rgba(125,138,153,.18);color:var(--muted)}
.r-calm{color:var(--green)}.r-neutral{color:var(--muted)}.r-watchful{color:var(--yellow)}.r-defensive{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:.9em}
th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;font-size:.8em;text-transform:uppercase}
button{background:var(--blue);color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:.85em;font-weight:500}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--border)}
button.danger{background:var(--red)}
button:hover{opacity:.85}
input,select{background:var(--card2);color:var(--text);border:1px solid var(--border);padding:7px 10px;border-radius:6px;font-size:.9em;font-family:inherit}
input:focus,select:focus{outline:0;border-color:var(--blue)}
.err{color:var(--red);font-size:.85em;margin-top:6px}
.ok{color:var(--green);font-size:.85em;margin-top:6px}
.gain{color:var(--green)}.loss{color:var(--red)}
.note{color:var(--muted);font-size:.8em;margin-top:6px;font-style:italic}
.actionrow{display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin:8px 0}
.field{display:flex;flex-direction:column;gap:3px}
.field label{font-size:.75em;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
.spark{height:32px;width:100%;display:block}
.legend{font-size:.75em;color:var(--muted);margin-top:4px}
hr{border:0;border-top:1px solid var(--border);margin:16px 0}
.kbd{background:var(--card2);padding:1px 5px;border-radius:3px;font-family:monospace;font-size:.85em}
.tabs{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tabs::-webkit-scrollbar{height:3px}
.tabs::-webkit-scrollbar-thumb{background:var(--border)}
@media (max-width:700px){
  .wrap{padding:12px}
  h1{font-size:1.25em}
  .tab{padding:8px 10px;font-size:.85em;white-space:nowrap}
  table{display:block;overflow-x:auto;white-space:nowrap}
  th,td{padding:6px 8px;font-size:.85em}
  .actionrow{flex-direction:column;align-items:stretch}
  .actionrow .field{width:100%}
  input,select,textarea,button{font-size:16px}  /* prevent iOS zoom on focus */
  .grid{grid-template-columns:1fr}
  button{padding:10px 14px;min-height:44px}     /* iOS touch target */
}
@supports (padding-bottom: env(safe-area-inset-bottom)){
  body{padding-bottom: env(safe-area-inset-bottom)}
}
</style>
</head><body><div class="wrap">
<h1>Long-term Holding</h1>
<div class="sub">Cycle phase, fundamentals, drawdown — the lens for months/years, not minutes.</div>

<div id="onboarding-overlay" hidden style="position:fixed;inset:0;background:rgba(10,13,18,.95);z-index:100;overflow-y:auto;padding:24px">
  <div style="max-width:640px;margin:40px auto;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h2 style="margin:0;text-transform:none;letter-spacing:0;font-size:1.1em;color:var(--text)">Quick setup</h2>
      <button id="onb-skip" class="ghost" style="font-size:.8em">Skip for now</button>
    </div>
    <div id="onb-progress" style="display:flex;gap:4px;margin-bottom:16px"></div>
    <div id="onb-content"></div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="overview">Overview</div>
  <div class="tab" data-tab="indicators">Indicators</div>
  <div class="tab" data-tab="derivatives">Derivatives</div>
  <div class="tab" data-tab="macro">Macro</div>
  <div class="tab" data-tab="backtests">Backtests</div>
  <div class="tab" data-tab="portfolio">Portfolio</div>
  <div class="tab" data-tab="targets">Targets &amp; Rebalance</div>
  <div class="tab" data-tab="dca">DCA Plan</div>
  <div class="tab" data-tab="alerts">Risk Alerts</div>
  <div class="tab" data-tab="execution">Execution</div>
  <div class="tab" data-tab="tax">Taxes</div>
  <div class="tab" data-tab="strategies">Strategies</div>
</div>

<section data-section="overview">
  <h2>Asset overview</h2>
  <div id="overview-grid" class="grid"></div>
  <div class="note">MVRV/NVT shown only for BTC and ETH (free CoinMetrics tier). Others use price-only signals — still everything you need for cycle-aware DCA.</div>
</section>

<section data-section="indicators" hidden>
  <h2>Cycle indicators</h2>
  <div class="note">Every indicator returns bullish / neutral / bearish based on its calibrated thresholds. Click an indicator to see what calls it generated historically.</div>
  <div id="ind-composites" style="margin:12px 0"></div>
  <table id="ind-table"><thead><tr>
    <th>Ticker</th><th>Indicator</th><th>Value</th><th>Signal</th><th>Description</th><th>Source</th></tr></thead><tbody></tbody></table>
</section>

<section data-section="derivatives" hidden>
  <h2>Derivatives</h2>
  <div class="note">Funding rate is the single best risk-off signal in crypto. Sustained funding &gt; +0.05%/8h ≈ 55%/yr — that's leveraged longs paying through the nose to keep their positions open.</div>
  <div id="deriv-composite" style="margin:12px 0"></div>
  <table id="deriv-table"><thead><tr>
    <th>Ticker</th><th>Funding now</th><th>Annualised</th><th>7d avg</th><th>Signal</th><th>OI (USD)</th><th>OI 7d Δ</th><th>OI signal</th></tr></thead><tbody></tbody></table>
</section>

<section data-section="macro" hidden>
  <h2>Macro overlay</h2>
  <div id="macro-note" class="note"></div>
  <div id="macro-regime" style="margin:12px 0"></div>
  <table id="macro-table"><thead><tr>
    <th>Series</th><th>Value</th><th>30d Δ</th><th>1y Δ</th><th>BTC corr (90d)</th><th>Signal</th><th>Note</th></tr></thead><tbody></tbody></table>
</section>

<section data-section="backtests" hidden>
  <h2>Indicator backtests</h2>
  <div class="note">Walk-forward: at every historical day we recomputed the indicator using only data available up to that day, then measured the forward return at 30 / 90 / 365 days. "Excess" is median forward return on bullish signals minus the unconditional baseline median. Sample sizes are still small (one cycle of data) — treat as illustrative until the dataset grows.</div>
  <div style="margin:8px 0">
    <button id="bt-rerun" class="ghost">Re-run all backtests</button>
    <span id="bt-msg"></span>
  </div>
  <table id="bt-table"><thead><tr>
    <th>Indicator</th><th>Ticker</th><th>Horizon</th><th>Fires</th><th>Median fwd</th><th>Mean fwd</th><th>Win rate</th><th>Baseline</th><th>Excess</th><th>Hit ratio</th></tr></thead><tbody></tbody></table>
</section>

<section data-section="portfolio" hidden>
  <h2>Holdings</h2>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="h-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <div class="field"><label>Quantity</label><input id="h-qty" type="number" step="any" min="0" placeholder="0.5"></div>
    <div class="field"><label>Cost basis (USD/unit)</label><input id="h-cb" type="number" step="any" min="0" placeholder="42000"></div>
    <div class="field"><label>Acquired</label><input id="h-date" type="date"></div>
    <button id="h-add">Add lot</button>
  </div>
  <div id="h-msg"></div>
  <table id="h-table"><thead><tr>
    <th>Ticker</th><th>Qty</th><th>Avg cost</th><th>Cur. price</th><th>Value</th><th>P&amp;L</th><th>Lots</th></tr></thead><tbody></tbody></table>
  <h2>Lot detail</h2>
  <table id="h-lots"><thead><tr>
    <th>Acquired</th><th>Ticker</th><th>Qty</th><th>Cost/u</th><th>P&amp;L</th><th>LT eligible</th><th></th></tr></thead><tbody></tbody></table>
</section>

<section data-section="targets" hidden>
  <h2>Target weights</h2>
  <div class="note">Weights should sum to 1.0 (or less, if you keep cash). Drift band = how far each leg can drift before rebalance triggers.</div>
  <div id="t-rows"></div>
  <button id="t-save">Save targets</button>
  <div id="t-msg"></div>
  <h2>Rebalance plan</h2>
  <button id="t-refresh-rebalance" class="ghost">Recompute</button>
  <div id="rebalance-out" style="margin-top:12px"></div>
</section>

<section data-section="dca" hidden>
  <h2>DCA schedule</h2>
  <div class="note">Set a base amount per asset. The recommender tilts it up in fear (Mayer &lt; 1, deep drawdown) and down in euphoria (Mayer &gt; 2.4). Set "Use multiplier" off to keep it perfectly fixed.</div>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="d-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <div class="field"><label>Frequency</label>
      <select id="d-freq"><option value="daily">Daily</option><option value="weekly" selected>Weekly</option><option value="monthly">Monthly</option></select>
    </div>
    <div class="field"><label>Base amount (USD)</label><input id="d-amt" type="number" step="any" min="0" value="100"></div>
    <div class="field"><label><input id="d-mult" type="checkbox" checked> Use cycle multiplier</label></div>
    <button id="d-add">Save</button>
  </div>
  <div id="d-msg"></div>
  <table id="d-table"><thead><tr>
    <th>Ticker</th><th>Freq</th><th>Base $</th><th>Multiplier</th><th>Suggested today</th><th>Phase</th><th>Reason</th><th></th></tr></thead><tbody></tbody></table>
</section>

<section data-section="alerts" hidden>
  <h2>Risk-off alerts</h2>
  <div class="note">Alerts fire when a threshold is crossed. They're recorded in <span class="kbd">crypto_alert_history</span>; wire to email/push by setting up the existing alert pipeline.</div>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="a-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <div class="field"><label>Type</label>
      <select id="a-type">
        <option value="drawdown">Drawdown ≥</option>
        <option value="mvrv_high">MVRV ≥</option>
        <option value="mvrv_low">MVRV ≤</option>
        <option value="risk_off">Risk-off score ≥</option>
        <option value="vol_regime">Vol regime: elevated/extreme</option>
      </select>
    </div>
    <div class="field"><label>Threshold</label><input id="a-thresh" type="number" step="any" placeholder="0.4 (40% dd) or 3.5 (mvrv)"></div>
    <button id="a-add">Add alert</button>
  </div>
  <div id="a-msg"></div>
  <table id="a-table"><thead><tr>
    <th>Ticker</th><th>Type</th><th>Threshold</th><th>Last fired</th><th></th></tr></thead><tbody></tbody></table>
</section>

<section data-section="execution" hidden>
  <h2>Auto-execution</h2>
  <div class="note" style="color:var(--yellow);font-weight:600">⚠ Dry-run mode is ON by default. Real orders are <b>never</b> placed unless you explicitly flip dry-run off and supply working exchange credentials.</div>

  <h2 style="margin-top:18px">Exchange connections</h2>
  <div class="grid">
    <div class="card">
      <h3>Coinbase Advanced Trade</h3>
      <div class="note">Create an Advanced Trade API key at <span class="kbd">coinbase.com/settings/api</span> with <b>view + trade</b> scopes only — never withdraw.</div>
      <div class="field"><label>API Key (UUID)</label><input id="cb-key" type="text" placeholder="00000000-0000-0000-0000-000000000000"></div>
      <div class="field"><label>EC Private Key (PEM)</label><textarea id="cb-pem" rows="4" placeholder="-----BEGIN EC PRIVATE KEY-----..." style="background:var(--card2);color:var(--text);border:1px solid var(--border);padding:7px 10px;border-radius:6px;font-family:monospace;font-size:.8em;width:100%"></textarea></div>
      <div class="actionrow">
        <button id="cb-save">Save &amp; test</button>
        <button id="cb-del" class="ghost danger">Disconnect</button>
        <span id="cb-msg"></span>
      </div>
    </div>
    <div class="card">
      <h3>Kraken</h3>
      <div class="note">Create an API key at <span class="kbd">kraken.com/u/security/api</span> with <b>Query Funds</b> + <b>Create Orders</b> only — disable withdraw.</div>
      <div class="field"><label>API Key</label><input id="kr-key" type="text"></div>
      <div class="field"><label>API Secret (base64)</label><input id="kr-secret" type="password"></div>
      <div class="actionrow">
        <button id="kr-save">Save &amp; test</button>
        <button id="kr-del" class="ghost danger">Disconnect</button>
        <span id="kr-msg"></span>
      </div>
    </div>
  </div>

  <h2>Safety limits</h2>
  <div id="safety-form" class="grid"></div>
  <div class="actionrow" style="margin-top:8px">
    <button id="safety-save">Save safety limits</button>
    <span id="safety-msg"></span>
  </div>

  <h2>Live balances</h2>
  <div id="balances-out" style="margin-top:8px"></div>

  <h2>What would the executor do right now?</h2>
  <div class="actionrow">
    <button id="preview-btn" class="ghost">Refresh preview</button>
  </div>
  <table id="preview-table"><thead><tr>
    <th>Ticker</th><th>Action</th><th>USD</th><th>Limit price</th><th>Exchange</th><th>Reason</th></tr></thead><tbody></tbody></table>

  <h2>Manual triggers</h2>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="exec-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <button id="exec-dca">Run DCA now</button>
    <button id="exec-rebal" class="ghost">Run rebalance</button>
    <span id="exec-msg"></span>
  </div>

  <h2>Push notifications</h2>
  <div class="card">
    <div class="note">Install this page as an app (Add to Home Screen) for the best mobile experience. Notifications fire when: a long-term alert threshold is crossed, the executor places or blocks an order, or the portfolio circuit breaker trips.</div>
    <div class="actionrow" style="margin-top:8px">
      <button id="push-enable">Enable notifications</button>
      <button id="push-test" class="ghost">Send test push</button>
      <button id="push-disable" class="ghost danger">Disable on this device</button>
      <span id="push-msg"></span>
    </div>
    <div id="push-subs" style="margin-top:8px"></div>
  </div>

  <h2>Execution log</h2>
  <table id="exec-table"><thead><tr>
    <th>When</th><th>Ticker</th><th>Side</th><th>Action</th><th>Reason</th><th>USD</th><th>Limit</th><th>Status</th></tr></thead><tbody></tbody></table>
</section>

<section data-section="strategies" hidden>
  <h2>Strategies</h2>
  <div class="note">A strategy composes DCA cadence + cycle-aware multiplier + optional harvest rules. Backtests run against historical daily bars; public strategies are ranked on a Sharpe-vs-drawdown leaderboard.</div>

  <div class="tabs" style="margin-top:6px;border-bottom-color:var(--card2)">
    <div class="tab active" data-stab="mine">My strategies</div>
    <div class="tab" data-stab="leaderboard">Leaderboard</div>
    <div class="tab" data-stab="marketplace">Marketplace</div>
    <div class="tab" data-stab="edit">New / edit</div>
  </div>

  <div data-sub="mine">
    <h3 style="font-size:1em;margin:14px 0 6px">Live subscriptions</h3>
    <div class="note">Subscribed strategies run automatically every 5 minutes through the safety gauntlet (dry-run defaults still apply).</div>
    <div id="sub-list" style="margin:8px 0 18px"></div>
    <h3 style="font-size:1em;margin:14px 0 6px">My strategies</h3>
    <div id="my-list" style="margin-top:10px"></div>
  </div>

  <div data-sub="leaderboard" hidden>
    <table id="lb-table"><thead><tr>
      <th>Rank</th><th>Name</th><th>Ticker</th><th>Return</th><th>Sharpe</th><th>Sortino</th><th>Max DD</th><th>Trades</th><th>Score</th><th></th></tr></thead><tbody></tbody></table>
  </div>

  <div data-sub="marketplace" hidden>
    <div id="market-list" style="margin-top:10px"></div>
  </div>

  <div data-sub="edit" hidden>
    <input type="hidden" id="st-id" value="">
    <div class="grid">
      <div class="card">
        <h3>Identity</h3>
        <div class="field"><label>Name</label><input id="st-name" type="text"></div>
        <div class="field"><label>Description</label><textarea id="st-desc" rows="3" style="background:var(--card2);color:var(--text);border:1px solid var(--border);padding:7px 10px;border-radius:6px;width:100%"></textarea></div>
        <div class="field"><label>Base ticker</label>
          <select id="st-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
        </div>
        <div class="field"><label>Starting capital (USD)</label><input id="st-cap" type="number" step="100" value="10000"></div>
        <div class="field"><label>Visibility</label>
          <select id="st-vis"><option value="private">Private</option><option value="public">Public (on leaderboard)</option></select>
        </div>
      </div>
      <div class="card">
        <h3>DCA</h3>
        <div class="field"><label><input id="st-dca" type="checkbox" checked> DCA enabled</label></div>
        <div class="field"><label>Amount (USD)</label><input id="st-dca-amt" type="number" step="10" value="100"></div>
        <div class="field"><label>Frequency</label>
          <select id="st-dca-freq"><option value="daily">Daily</option><option value="weekly" selected>Weekly</option><option value="monthly">Monthly</option></select>
        </div>
      </div>
      <div class="card">
        <h3>Cycle multipliers</h3>
        <div class="field"><label>Bullish drawdown threshold</label><input id="st-bdd" type="number" step="0.05" value="-0.40"></div>
        <div class="field"><label>Bullish drawdown multiplier</label><input id="st-bddm" type="number" step="0.1" value="2.0"></div>
        <div class="field"><label>Bearish Mayer threshold</label><input id="st-bmt" type="number" step="0.1" value="2.4"></div>
        <div class="field"><label>Bearish Mayer multiplier</label><input id="st-bmm" type="number" step="0.1" value="0.5"></div>
        <div class="field"><label>Pause-buys Mayer threshold</label><input id="st-pmt" type="number" step="0.1" value="2.7"></div>
      </div>
      <div class="card">
        <h3>Harvest</h3>
        <div class="field"><label><input id="st-harv" type="checkbox"> Harvest losses</label></div>
        <div class="field"><label>Min loss to harvest ($)</label><input id="st-hmin" type="number" step="10" value="100"></div>
        <div class="field"><label>Min age (days)</label><input id="st-hage" type="number" step="1" value="30"></div>
      </div>
    </div>
    <div class="actionrow" style="margin-top:10px">
      <button id="st-save">Save</button>
      <button id="st-backtest" class="ghost">Save &amp; backtest</button>
      <button id="st-delete" class="ghost danger">Delete</button>
      <span id="st-msg"></span>
    </div>
    <div id="st-result" style="margin-top:14px"></div>
  </div>
</section>

<section data-section="tax" hidden>
  <h2>Tax settings</h2>
  <div class="actionrow">
    <div class="field"><label>Jurisdiction</label>
      <select id="tx-jur"><option>US</option><option>UK</option><option>DE</option></select>
    </div>
    <div class="field"><label>Default lot method</label>
      <select id="tx-method">
        <option value="HIFO">HIFO (min gain)</option>
        <option value="FIFO">FIFO (oldest first)</option>
        <option value="LIFO">LIFO (newest first)</option>
        <option value="LOFO">LOFO (max gain)</option>
        <option value="TAX_OPTIMAL">Tax-optimal</option>
      </select>
    </div>
    <div class="field"><label>ST rate</label><input id="tx-st" type="number" step="0.01" min="0" max="0.6"></div>
    <div class="field"><label>LT rate</label><input id="tx-lt" type="number" step="0.01" min="0" max="0.6"></div>
    <div class="field"><label>Harvest min loss ($)</label><input id="tx-hl" type="number" step="10" min="0"></div>
    <div class="field"><label>Harvest min age (days)</label><input id="tx-hd" type="number" step="1" min="0"></div>
    <button id="tx-save">Save</button>
    <span id="tx-set-msg"></span>
  </div>

  <h2>Annual summary</h2>
  <div class="actionrow">
    <div class="field"><label>Year</label><input id="tx-year" type="number" step="1" value="2026" style="width:90px"></div>
    <button id="tx-year-go" class="ghost">Compute</button>
    <a id="tx-export" href="#" style="margin-left:8px;color:var(--blue);text-decoration:none">Download Form 8949 CSV</a>
  </div>
  <div id="tx-summary" style="margin-top:10px"></div>

  <h2>Sell preview</h2>
  <div class="note">Show me what a hypothetical sale would do — no records written. Compare lot methods side-by-side.</div>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="ps-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <div class="field"><label>Quantity</label><input id="ps-qty" type="number" step="any" min="0" placeholder="0.5"></div>
    <div class="field"><label>Method</label>
      <select id="ps-method">
        <option value="HIFO">HIFO</option>
        <option value="FIFO">FIFO</option>
        <option value="LIFO">LIFO</option>
        <option value="LOFO">LOFO</option>
        <option value="TAX_OPTIMAL">Tax-optimal</option>
      </select>
    </div>
    <div class="field"><label>Sell price (USD, blank = market)</label><input id="ps-price" type="number" step="any" min="0"></div>
    <button id="ps-go">Preview</button>
  </div>
  <div id="ps-out" style="margin-top:10px"></div>

  <h2>Harvest opportunities</h2>
  <div id="hv-totals" style="margin:8px 0"></div>
  <div class="actionrow">
    <button id="hv-execute">Harvest all (sells go through safety gauntlet)</button>
    <span id="hv-msg"></span>
  </div>
  <table id="hv-table"><thead><tr>
    <th>Ticker</th><th>Qty</th><th>Cost</th><th>Price</th><th>Loss</th><th>Loss %</th><th>Days</th><th>LT/ST</th><th>Wash risk</th><th>Est. tax save</th></tr></thead><tbody></tbody></table>

  <h2>Open lots</h2>
  <table id="lots-table"><thead><tr>
    <th>Acquired</th><th>Ticker</th><th>Original qty</th><th>Consumed</th><th>Remaining</th><th>Cost basis</th><th>LT eligible at</th></tr></thead><tbody></tbody></table>

  <h2>Dispositions (realised P&amp;L)</h2>
  <div class="actionrow">
    <div class="field"><label>Ticker</label>
      <select id="dp-ticker"><option>BTC</option><option>ETH</option><option>SOL</option><option>DOGE</option><option>XRP</option></select>
    </div>
    <div class="field"><label>Qty</label><input id="dp-qty" type="number" step="any" min="0"></div>
    <div class="field"><label>Sell price</label><input id="dp-price" type="number" step="any" min="0"></div>
    <div class="field"><label>Sell date</label><input id="dp-date" type="date"></div>
    <button id="dp-add">Record sale</button>
    <span id="dp-msg"></span>
  </div>
  <table id="dp-table"><thead><tr>
    <th>Date</th><th>Ticker</th><th>Qty</th><th>Price</th><th>Method</th><th>Realised</th><th>LT</th><th>ST</th><th>Lots</th></tr></thead><tbody></tbody></table>
</section>

<script>
const TICKERS = ["BTC","ETH","SOL","DOGE","XRP"];
const fmt = (v, d=2) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
const pct = (v) => v == null || isNaN(v) ? '—' : (Number(v)*100).toFixed(1)+'%';
const usd = (v) => v == null || isNaN(v) ? '—' : '$'+fmt(v);

async function api(path, opts={}) {
  const r = await fetch(path, {credentials:'include', headers: {'X-Requested-With':'XMLHttpRequest', 'Content-Type':'application/json'}, ...opts});
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  return r.json();
}

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const which = t.dataset.tab;
  document.querySelectorAll('section').forEach(s => s.hidden = s.dataset.section !== which);
  if (which === 'overview') loadOverview();
  if (which === 'indicators') loadIndicators();
  if (which === 'derivatives') loadDerivatives();
  if (which === 'macro') loadMacro();
  if (which === 'backtests') loadBacktests();
  if (which === 'portfolio') loadPortfolio();
  if (which === 'targets') loadTargets();
  if (which === 'dca') loadDCA();
  if (which === 'alerts') loadAlerts();
  if (which === 'execution') loadExecution();
  if (which === 'tax') loadTax();
  if (which === 'strategies') loadStrategies();
});

// Sub-tabs inside the Strategies section
document.querySelectorAll('[data-stab]').forEach(t => t.onclick = () => {
  document.querySelectorAll('[data-stab]').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const w = t.dataset.stab;
  document.querySelectorAll('[data-sub]').forEach(s => s.hidden = s.dataset.sub !== w);
  if (w === 'mine') loadMyStrategies();
  if (w === 'leaderboard') loadLeaderboard();
  if (w === 'marketplace') loadMarketplace();
  if (w === 'edit') resetEditor();
});

const SIG_CLASS = {bullish:'r-calm', bearish:'r-defensive', neutral:'r-neutral', unavailable:'r-neutral'};

async function loadIndicators(){
  const tbody = document.querySelector('#ind-table tbody');
  const comp = document.getElementById('ind-composites');
  tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const d = await api('/api/long-term/indicators');
    tbody.innerHTML = '';
    for (const r of d.indicators) {
      const cls = SIG_CLASS[r.signal] || 'r-neutral';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${r.ticker}</td><td>${r.name}</td>
        <td>${r.value == null ? '—' : fmt(r.value, 4)}</td>
        <td class="${cls}"><b>${r.signal}</b></td>
        <td style="font-size:.85em">${r.description}</td>
        <td style="font-size:.75em;color:var(--muted)">${r.source}</td>`;
      tbody.appendChild(tr);
    }
    // Composite scores for each ticker.
    comp.innerHTML = '';
    for (const tk of TICKERS) {
      try {
        const c = await api('/api/long-term/indicators/composite/'+tk);
        if (c.score == null) continue;
        const lbl = c.label;
        const cls = lbl === 'accumulate' || lbl === 'lean-bullish' ? 'r-calm'
                  : lbl === 'defensive' || lbl === 'lean-bearish' ? 'r-defensive' : 'r-neutral';
        comp.insertAdjacentHTML('beforeend',
          `<span style="margin-right:14px"><b>${tk}</b>: <span class="${cls}">${lbl}</span> (${fmt(c.score,2)})</span>`);
      } catch(e){}
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="6" class="err">${e.message}</td></tr>`; }
}

async function loadDerivatives(){
  const tbody = document.querySelector('#deriv-table tbody');
  tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const d = await api('/api/long-term/derivatives');
    tbody.innerHTML = '';
    for (const row of d.assets) {
      const f = row.funding, o = row.open_interest;
      if (!f && !o) continue;
      const fCls = f ? SIG_CLASS[f.signal] : '';
      const oCls = o ? SIG_CLASS[o.signal] : '';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${row.ticker}</td>
        <td>${f ? fmt(f.current_rate*100,4)+'%' : '—'}</td>
        <td>${f ? fmt(f.annualised*100,1)+'%' : '—'}</td>
        <td>${f ? fmt(f.avg_7d*100,4)+'%' : '—'}</td>
        <td class="${fCls}">${f ? f.signal : '—'}</td>
        <td>${o ? usd(o.current_usd) : '—'}</td>
        <td>${o && o.pct_change_7d!=null ? pct(o.pct_change_7d) : '—'}</td>
        <td class="${oCls}" style="font-size:.85em">${o ? o.description : '—'}</td>`;
      tbody.appendChild(tr);
    }
    const fc = d.funding_composite;
    if (fc && fc.score != null) {
      const cls = fc.label === 'long-crowding' ? 'r-defensive'
                : fc.label === 'capitulation' ? 'r-calm' : 'r-neutral';
      document.getElementById('deriv-composite').innerHTML =
        `<b>Funding composite:</b> <span class="${cls}">${fc.label}</span> (${fmt(fc.score,2)})`;
    } else {
      document.getElementById('deriv-composite').innerHTML = '<span class="note">Funding composite unavailable — needs at least one full refresh cycle.</span>';
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="8" class="err">${e.message}</td></tr>`; }
}

async function loadMacro(){
  const tbody = document.querySelector('#macro-table tbody');
  tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const d = await api('/api/long-term/macro');
    document.getElementById('macro-note').textContent = d.fred_configured
      ? 'Pulling FRED + Stooq.' : 'FRED_API_KEY not set — falling back to Stooq for DXY/Gold. Other series will show "unavailable" until you set a (free) FRED key.';
    tbody.innerHTML = '';
    for (const s of d.series) {
      const cls = s.signal === 'crypto-tailwind' ? 'r-calm'
                : s.signal === 'crypto-headwind' ? 'r-defensive'
                : s.signal === 'unavailable' ? 'r-neutral' : 'r-neutral';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${s.name} <span style="color:var(--muted);font-size:.75em">${s.series_id}</span></td>
        <td>${s.value == null ? '—' : fmt(s.value, 2)}</td>
        <td>${s.pct_change_30d==null ? '—' : pct(s.pct_change_30d)}</td>
        <td>${s.pct_change_365d==null ? '—' : pct(s.pct_change_365d)}</td>
        <td>${s.btc_corr_90d==null ? '—' : fmt(s.btc_corr_90d, 2)}</td>
        <td class="${cls}">${s.signal}</td>
        <td style="font-size:.85em">${s.description}</td>`;
      tbody.appendChild(tr);
    }
    const r = d.regime;
    if (r && r.score != null) {
      const cls = r.label === 'tailwind' || r.label === 'lean-tailwind' ? 'r-calm'
                : r.label === 'headwind' || r.label === 'lean-headwind' ? 'r-defensive' : 'r-neutral';
      document.getElementById('macro-regime').innerHTML =
        `<b>Macro regime:</b> <span class="${cls}">${r.label}</span> (${fmt(r.score,2)})`;
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="7" class="err">${e.message}</td></tr>`; }
}

async function loadBacktests(){
  const tbody = document.querySelector('#bt-table tbody');
  tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const d = await api('/api/long-term/backtests');
    if (!d.results.length) {
      tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">No backtests yet — the first refresh cycle on the box runs them automatically. Click "Re-run" to force.</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    for (const r of d.results) {
      const excessCls = r.median_excess > 0 ? 'gain' : r.median_excess < 0 ? 'loss' : '';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${r.indicator}</td><td>${r.ticker}</td><td>${r.horizon_days}d</td>
        <td>${r.fired_n}</td>
        <td>${r.median_fwd_return==null?'—':pct(r.median_fwd_return)}</td>
        <td>${r.mean_fwd_return==null?'—':pct(r.mean_fwd_return)}</td>
        <td>${r.win_rate==null?'—':pct(r.win_rate)}</td>
        <td>${pct(r.median_baseline)}</td>
        <td class="${excessCls}">${r.median_excess==null?'—':pct(r.median_excess)}</td>
        <td>${r.hit_ratio==null?'—':pct(r.hit_ratio)}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`; }
}

document.getElementById('bt-rerun').onclick = async () => {
  const msg = document.getElementById('bt-msg'); msg.textContent = ' running…';
  try {
    const r = await api('/api/long-term/backtests/run', {method:'POST'});
    msg.textContent = ` done in ${r.elapsed_s}s (${r.computed} computed, ${r.skipped} skipped).`;
    loadBacktests();
  } catch(e) { msg.textContent = ' ' + e.message; }
};

// ── Execution tab ──────────────────────────────────────────────────────────

const SAFETY_FIELDS = [
  {k: 'dry_run', label: 'Dry-run mode (orders logged, never sent)', type: 'checkbox'},
  {k: 'preferred_exchange', label: 'Preferred exchange', type: 'select', options: ['coinbase','kraken']},
  {k: 'max_order_usd', label: 'Max single order (USD)', type: 'number', step: 10},
  {k: 'max_daily_usd', label: 'Max total spend per day (USD)', type: 'number', step: 50},
  {k: 'circuit_breaker_pct', label: 'Pause if portfolio drops by (24h)', type: 'number', step: 0.01, format: 'pct'},
  {k: 'limit_offset_bps', label: 'Limit price offset (basis points below mid)', type: 'number', step: 5},
  {k: 'limit_ttl_seconds', label: 'Cancel unfilled limits after (seconds)', type: 'number', step: 60},
  {k: 'fallback_to_market', label: 'After TTL, fall back to market order', type: 'checkbox'},
];

async function loadExecution(){
  await loadSafetyForm();
  await loadConfiguredExchanges();
  await loadBalances();
  await loadPreview();
  await loadExecLog();
}

async function loadSafetyForm(){
  const form = document.getElementById('safety-form');
  form.innerHTML = '<div style="color:var(--muted)">Loading…</div>';
  try {
    const s = await api('/api/long-term/safety');
    form.innerHTML = '';
    for (const f of SAFETY_FIELDS) {
      const wrap = document.createElement('div'); wrap.className = 'card';
      let inputHtml;
      if (f.type === 'checkbox') {
        inputHtml = `<input type="checkbox" id="sf-${f.k}" ${s[f.k] ? 'checked' : ''}> <span>${f.label}</span>`;
      } else if (f.type === 'select') {
        inputHtml = `<label>${f.label}</label><select id="sf-${f.k}">${
          f.options.map(o => `<option value="${o}" ${o===s[f.k]?'selected':''}>${o}</option>`).join('')
        }</select>`;
      } else {
        const v = f.format === 'pct' ? s[f.k] : s[f.k];
        inputHtml = `<label>${f.label}</label><input type="number" id="sf-${f.k}" step="${f.step||1}" value="${v}">`;
      }
      wrap.innerHTML = inputHtml;
      form.appendChild(wrap);
    }
  } catch(e) { form.innerHTML = `<div class="err">${e.message}</div>`; }
}

document.getElementById('safety-save').onclick = async () => {
  const msg = document.getElementById('safety-msg'); msg.innerHTML = '';
  const payload = {};
  for (const f of SAFETY_FIELDS) {
    const el = document.getElementById('sf-'+f.k);
    if (!el) continue;
    if (f.type === 'checkbox') payload[f.k] = el.checked;
    else if (f.type === 'select') payload[f.k] = el.value;
    else payload[f.k] = parseFloat(el.value);
  }
  try {
    await api('/api/long-term/safety', {method:'POST', body: JSON.stringify(payload)});
    msg.innerHTML = '<span class="ok">Saved.</span>';
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

async function loadConfiguredExchanges(){
  try {
    const r = await api('/api/exchanges/configured');
    const cbConfigured = r.exchanges.includes('coinbase');
    const krConfigured = r.exchanges.includes('kraken');
    document.getElementById('cb-msg').innerHTML = cbConfigured ? '<span class="ok">connected</span>' : '<span class="note">not connected</span>';
    document.getElementById('kr-msg').innerHTML = krConfigured ? '<span class="ok">connected</span>' : '<span class="note">not connected</span>';
  } catch(e) {}
}

document.getElementById('cb-save').onclick = async () => {
  const msg = document.getElementById('cb-msg'); msg.innerHTML = ' testing…';
  try {
    const r = await api('/api/exchanges/credentials', {method:'POST', body: JSON.stringify({
      exchange: 'coinbase',
      api_key: document.getElementById('cb-key').value,
      private_key_pem: document.getElementById('cb-pem').value,
    })});
    msg.innerHTML = '<span class="ok">'+r.message+'</span>';
    loadConfiguredExchanges(); loadBalances();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};
document.getElementById('cb-del').onclick = async () => {
  if (!confirm('Disconnect Coinbase?')) return;
  await api('/api/exchanges/credentials/coinbase', {method:'DELETE'});
  loadConfiguredExchanges(); loadBalances();
};
document.getElementById('kr-save').onclick = async () => {
  const msg = document.getElementById('kr-msg'); msg.innerHTML = ' testing…';
  try {
    const r = await api('/api/exchanges/credentials', {method:'POST', body: JSON.stringify({
      exchange: 'kraken',
      api_key: document.getElementById('kr-key').value,
      secret: document.getElementById('kr-secret').value,
    })});
    msg.innerHTML = '<span class="ok">'+r.message+'</span>';
    loadConfiguredExchanges(); loadBalances();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};
document.getElementById('kr-del').onclick = async () => {
  if (!confirm('Disconnect Kraken?')) return;
  await api('/api/exchanges/credentials/kraken', {method:'DELETE'});
  loadConfiguredExchanges(); loadBalances();
};

async function loadBalances(){
  const out = document.getElementById('balances-out');
  out.innerHTML = '<div style="color:var(--muted)">Loading…</div>';
  try {
    const r = await api('/api/exchanges/balances');
    const names = Object.keys(r.balances);
    if (!names.length) { out.innerHTML = '<div class="note">No exchanges connected.</div>'; return; }
    let html = '';
    for (const name of names) {
      const b = r.balances[name];
      if (b.error) { html += `<div><b>${name}</b>: <span class="err">${b.error}</span></div>`; continue; }
      const rows = b.filter(x => x.total > 0.0001).map(x => `<tr><td>${x.asset}</td><td>${fmt(x.available,8)}</td><td>${fmt(x.total,8)}</td></tr>`).join('');
      html += `<div style="margin-bottom:12px"><b>${name}</b><table style="margin-top:4px"><thead><tr><th>Asset</th><th>Available</th><th>Total</th></tr></thead><tbody>${rows||'<tr><td colspan="3" style="color:var(--muted)">empty</td></tr>'}</tbody></table></div>`;
    }
    out.innerHTML = html;
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

document.getElementById('preview-btn').onclick = loadPreview;
async function loadPreview(){
  const tbody = document.querySelector('#preview-table tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const r = await api('/api/long-term/executions/preview');
    if (!r.preview.length) { tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">No active DCA schedules.</td></tr>'; return; }
    tbody.innerHTML = '';
    for (const p of r.preview) {
      const actCls = p.action === 'dry_run' ? 'r-neutral' : p.action === 'blocked' ? 'r-defensive' : p.action === 'placed' ? 'r-calm' : 'r-watchful';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.ticker}</td><td class="${actCls}">${p.action}</td>
        <td>${usd(p.usd_amount)}</td>
        <td>${p.limit_price==null?'—':usd(p.limit_price)}</td>
        <td>${p.exchange}</td>
        <td style="font-size:.85em">${p.reason}${p.dca_reason ? ' · '+p.dca_reason : ''}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="6" class="err">${e.message}</td></tr>`; }
}

document.getElementById('exec-dca').onclick = async () => {
  const ticker = document.getElementById('exec-ticker').value;
  const msg = document.getElementById('exec-msg'); msg.innerHTML = ' running…';
  try {
    const r = await api('/api/long-term/executions/dca/'+ticker, {method:'POST'});
    msg.innerHTML = ` <span class="${r.action==='placed'?'ok':r.action==='blocked'?'err':'note'}">${r.action}: ${r.reason}</span>`;
    loadExecLog(); loadPreview();
  } catch(e) { msg.innerHTML = ' <span class="err">'+e.message+'</span>'; }
};

document.getElementById('exec-rebal').onclick = async () => {
  if (!confirm('Run rebalance now? Sells are blocked in Phase 2 — only buys will execute.')) return;
  const msg = document.getElementById('exec-msg'); msg.innerHTML = ' running…';
  try {
    const r = await api('/api/long-term/executions/rebalance', {method:'POST'});
    msg.innerHTML = ` <span class="note">${r.decisions.length} legs evaluated</span>`;
    loadExecLog();
  } catch(e) { msg.innerHTML = ' <span class="err">'+e.message+'</span>'; }
};

async function loadExecLog(){
  const tbody = document.querySelector('#exec-table tbody');
  tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const r = await api('/api/long-term/executions');
    if (!r.executions.length) { tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">No execution history yet.</td></tr>'; return; }
    tbody.innerHTML = '';
    for (const e of r.executions) {
      const actCls = e.action === 'placed' ? 'r-calm' : e.action === 'blocked' ? 'r-defensive' : e.action === 'dry_run' ? 'r-neutral' : 'r-watchful';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td style="font-size:.85em">${e.created_at}</td><td>${e.ticker}</td>
        <td>${e.side}</td><td class="${actCls}">${e.action}</td>
        <td style="font-size:.85em">${e.reason||''}</td>
        <td>${e.usd_amount==null?'—':usd(e.usd_amount)}</td>
        <td>${e.limit_price==null?'—':usd(e.limit_price)}</td>
        <td>${e.status}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="8" class="err">${e.message}</td></tr>`; }
}

async function loadOverview(){
  const grid = document.getElementById('overview-grid');
  grid.innerHTML = '<div style="color:var(--muted)">Loading…</div>';
  try {
    const d = await api('/api/long-term/snapshot');
    grid.innerHTML = '';
    for (const a of d.assets) {
      const phase = (a.phase||'warming-up').replace(/-/g,'-');
      const ro = a.risk_off || {};
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `
        <h3>${a.ticker} <span class="pill p-${phase}">${phase}</span></h3>
        <div class="row"><span class="l">Price</span><span class="v">${usd(a.price)}</span></div>
        <div class="row"><span class="l">Mayer (px/200d)</span><span class="v">${a.mayer ?? '—'}</span></div>
        <div class="row"><span class="l">Drawdown</span><span class="v ${a.current_dd<0?'loss':''}">${pct(a.current_dd)}</span></div>
        <div class="row"><span class="l">30d vol (annualised)</span><span class="v">${pct(a.vol_30d)} <span class="legend">(${a.vol_regime})</span></span></div>
        <div class="row"><span class="l">Sharpe (1y)</span><span class="v">${fmt(a.sharpe_1y)}</span></div>
        <div class="row"><span class="l">Sortino (1y)</span><span class="v">${fmt(a.sortino_1y)}</span></div>
        ${a.mvrv != null ? `<div class="row"><span class="l">MVRV</span><span class="v">${fmt(a.mvrv,2)}</span></div>`:''}
        ${a.nvt != null ? `<div class="row"><span class="l">NVT (28d)</span><span class="v">${fmt(a.nvt,1)}</span></div>`:''}
        <hr>
        <div class="row"><span class="l">Risk-off</span><span class="v r-${ro.label||'neutral'}">${(ro.label||'—').toUpperCase()} (${fmt(ro.score,2)})</span></div>
      `;
      grid.appendChild(card);
    }
  } catch(e) { grid.innerHTML = `<div class="err">Failed: ${e.message}</div>`; }
}

async function loadPortfolio(){
  const tbody = document.querySelector('#h-table tbody');
  const lots = document.querySelector('#h-lots tbody');
  tbody.innerHTML = lots.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const d = await api('/api/long-term/holdings');
    tbody.innerHTML = ''; lots.innerHTML = '';
    if (!d.rollup.length) { tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">No lots yet — add one above.</td></tr>'; }
    for (const r of d.rollup) {
      const tr = document.createElement('tr');
      const pnlCls = (r.unrealized_pnl||0) >= 0 ? 'gain' : 'loss';
      tr.innerHTML = `<td>${r.ticker}</td><td>${fmt(r.qty,6)}</td><td>${usd(r.avg_cost_basis)}</td>
        <td>${usd(r.current_price)}</td><td>${usd(r.current_value)}</td>
        <td class="${pnlCls}">${usd(r.unrealized_pnl)}</td><td>${r.lots}</td>`;
      tbody.appendChild(tr);
    }
    for (const l of d.lots) {
      const tr = document.createElement('tr');
      const pnlCls = (l.unrealized_pnl||0) >= 0 ? 'gain' : 'loss';
      tr.innerHTML = `<td>${l.acquired_at}</td><td>${l.ticker}</td><td>${fmt(l.qty,6)}</td>
        <td>${usd(l.cost_basis)}</td><td class="${pnlCls}">${usd(l.unrealized_pnl)}</td>
        <td>${l.long_term_eligible ? '✓ ('+l.held_days+'d)' : (l.held_days||'?')+'d'}</td>
        <td><button class="ghost danger" data-del="${l.id}">×</button></td>`;
      lots.appendChild(tr);
    }
    lots.querySelectorAll('button[data-del]').forEach(b => b.onclick = async () => {
      await api('/api/long-term/holdings/'+b.dataset.del, {method:'DELETE'});
      loadPortfolio();
    });
  } catch(e) { tbody.innerHTML = `<tr><td colspan="7" class="err">${e.message}</td></tr>`; }
}

document.getElementById('h-add').onclick = async () => {
  const msg = document.getElementById('h-msg'); msg.innerHTML='';
  try {
    await api('/api/long-term/holdings', {method:'POST', body: JSON.stringify({
      ticker: document.getElementById('h-ticker').value,
      qty: document.getElementById('h-qty').value,
      cost_basis: document.getElementById('h-cb').value,
      acquired_at: document.getElementById('h-date').value || undefined,
    })});
    msg.innerHTML = '<div class="ok">Lot added.</div>';
    loadPortfolio();
  } catch(e) { msg.innerHTML = '<div class="err">'+e.message+'</div>'; }
};

async function loadTargets(){
  const rows = document.getElementById('t-rows'); rows.innerHTML='';
  let existing = {};
  try { (await api('/api/long-term/targets')).targets.forEach(t => existing[t.ticker] = t); } catch(e){}
  for (const tk of TICKERS) {
    const cur = existing[tk] || {weight:0, drift_band:0.05};
    rows.insertAdjacentHTML('beforeend', `<div class="actionrow">
      <div class="field" style="min-width:60px"><label>${tk}</label></div>
      <div class="field"><label>Weight</label><input class="t-w" data-ticker="${tk}" type="number" step="0.01" min="0" max="1" value="${cur.weight||0}"></div>
      <div class="field"><label>Drift band</label><input class="t-b" data-ticker="${tk}" type="number" step="0.01" min="0" max="0.5" value="${cur.drift_band||0.05}"></div>
    </div>`);
  }
  await refreshRebalance();
}

document.getElementById('t-save').onclick = async () => {
  const msg = document.getElementById('t-msg'); msg.innerHTML='';
  const targets = TICKERS.map(tk => ({
    ticker: tk,
    weight: parseFloat(document.querySelector(`.t-w[data-ticker="${tk}"]`).value || 0),
    drift_band: parseFloat(document.querySelector(`.t-b[data-ticker="${tk}"]`).value || 0.05),
  }));
  try {
    const r = await api('/api/long-term/targets', {method:'POST', body: JSON.stringify({targets})});
    msg.innerHTML = `<div class="ok">Saved (sum=${fmt(r.total_weight,2)}).</div>`;
    refreshRebalance();
  } catch(e) { msg.innerHTML = '<div class="err">'+e.message+'</div>'; }
};

document.getElementById('t-refresh-rebalance').onclick = refreshRebalance;

async function refreshRebalance(){
  const out = document.getElementById('rebalance-out');
  out.innerHTML = '<div style="color:var(--muted)">Computing…</div>';
  try {
    const d = await api('/api/long-term/rebalance');
    if (d.error) { out.innerHTML = `<div class="note">${d.error}</div>`; return; }
    let html = `<div class="row"><span class="l">Portfolio total</span><span class="v">${usd(d.total_usd)}</span></div>
      <div class="row"><span class="l">Max drift</span><span class="v">${pct(d.max_drift)}</span></div>
      <div class="row"><span class="l">Action required?</span><span class="v">${d.rebalance_required ? '<span class="r-watchful">Yes</span>' : '<span class="r-calm">No</span>'}</span></div>
      <table style="margin-top:10px"><thead><tr><th>Ticker</th><th>Current</th><th>Target</th><th>Drift</th><th>Action</th><th>Notional</th></tr></thead><tbody>`;
    for (const leg of d.legs) {
      const cls = leg.action === 'buy' ? 'gain' : leg.action === 'sell' ? 'loss' : '';
      html += `<tr><td>${leg.ticker}</td><td>${pct(leg.current_weight)}</td><td>${pct(leg.target_weight)}</td>
        <td>${pct(leg.drift)}</td><td class="${cls}">${leg.action.toUpperCase()}</td><td>${usd(leg.notional_usd)}</td></tr>`;
    }
    html += '</tbody></table>';
    out.innerHTML = html;
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

async function loadDCA(){
  const tbody = document.querySelector('#d-table tbody'); tbody.innerHTML='';
  let recs = {};
  try { (await api('/api/long-term/dca/recommendations')).recommendations.forEach(r => recs[r.ticker]=r); } catch(e){}
  let schedules = [];
  try { schedules = (await api('/api/long-term/dca')).schedules; } catch(e){}
  if (!schedules.length) { tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">No DCA schedules yet.</td></tr>'; return; }
  for (const s of schedules) {
    const r = recs[s.ticker] || {};
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${s.ticker}</td><td>${s.frequency}</td><td>${usd(s.base_amount_usd)}</td>
      <td>${fmt(r.multiplier,2)}×</td><td><b>${usd(r.suggested_amount_usd)}</b></td>
      <td><span class="pill p-${(r.phase||'neutral').replace(/-/g,'-')}">${r.phase||'—'}</span></td>
      <td style="font-size:.8em;color:var(--muted)">${r.reason||''}</td>
      <td><button class="ghost danger" data-del="${s.ticker}">×</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('button[data-del]').forEach(b => b.onclick = async () => {
    await api('/api/long-term/dca/'+b.dataset.del, {method:'DELETE'});
    loadDCA();
  });
}

document.getElementById('d-add').onclick = async () => {
  const msg = document.getElementById('d-msg'); msg.innerHTML='';
  try {
    await api('/api/long-term/dca', {method:'POST', body: JSON.stringify({
      ticker: document.getElementById('d-ticker').value,
      frequency: document.getElementById('d-freq').value,
      base_amount_usd: document.getElementById('d-amt').value,
      use_multiplier: document.getElementById('d-mult').checked,
    })});
    msg.innerHTML = '<div class="ok">Saved.</div>';
    loadDCA();
  } catch(e) { msg.innerHTML = '<div class="err">'+e.message+'</div>'; }
};

async function loadAlerts(){
  const tbody = document.querySelector('#a-table tbody'); tbody.innerHTML='';
  try {
    const d = await api('/api/long-term/alerts');
    if (!d.alerts.length) { tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">No alerts yet.</td></tr>'; return; }
    for (const a of d.alerts) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${a.ticker}</td><td>${a.alert_type}</td><td>${a.threshold ?? '—'}</td>
        <td>${a.last_fired_at || 'never'}</td>
        <td><button class="ghost danger" data-tk="${a.ticker}" data-tp="${a.alert_type}">×</button></td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll('button[data-tk]').forEach(b => b.onclick = async () => {
      await api(`/api/long-term/alerts/${b.dataset.tk}/${b.dataset.tp}`, {method:'DELETE'});
      loadAlerts();
    });
  } catch(e) { tbody.innerHTML = `<tr><td colspan="5" class="err">${e.message}</td></tr>`; }
}

document.getElementById('a-add').onclick = async () => {
  const msg = document.getElementById('a-msg'); msg.innerHTML='';
  try {
    await api('/api/long-term/alerts', {method:'POST', body: JSON.stringify({
      ticker: document.getElementById('a-ticker').value,
      alert_type: document.getElementById('a-type').value,
      threshold: document.getElementById('a-thresh').value || null,
    })});
    msg.innerHTML = '<div class="ok">Alert saved.</div>';
    loadAlerts();
  } catch(e) { msg.innerHTML = '<div class="err">'+e.message+'</div>'; }
};

// ── Taxes tab ──────────────────────────────────────────────────────────────

async function loadTax(){
  await loadTaxSettings();
  await loadLots();
  await loadHarvest();
  await loadDispositions();
  await refreshExportLink();
}

async function loadTaxSettings(){
  try {
    const s = await api('/api/long-term/tax/settings');
    document.getElementById('tx-jur').value = s.jurisdiction;
    document.getElementById('tx-method').value = s.default_lot_method;
    document.getElementById('tx-st').value = s.st_rate;
    document.getElementById('tx-lt').value = s.lt_rate;
    document.getElementById('tx-hl').value = s.harvest_min_loss_usd;
    document.getElementById('tx-hd').value = s.harvest_min_age_days;
    document.getElementById('ps-method').value = s.default_lot_method;
  } catch(e) { console.error(e); }
}

document.getElementById('tx-save').onclick = async () => {
  const msg = document.getElementById('tx-set-msg'); msg.innerHTML = '';
  try {
    await api('/api/long-term/tax/settings', {method:'POST', body: JSON.stringify({
      jurisdiction: document.getElementById('tx-jur').value,
      default_lot_method: document.getElementById('tx-method').value,
      st_rate: parseFloat(document.getElementById('tx-st').value),
      lt_rate: parseFloat(document.getElementById('tx-lt').value),
      harvest_min_loss_usd: parseFloat(document.getElementById('tx-hl').value),
      harvest_min_age_days: parseInt(document.getElementById('tx-hd').value),
    })});
    msg.innerHTML = '<span class="ok">Saved.</span>';
    loadHarvest();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

document.getElementById('tx-year-go').onclick = async () => {
  const year = parseInt(document.getElementById('tx-year').value);
  const out = document.getElementById('tx-summary');
  out.innerHTML = '<div style="color:var(--muted)">Computing…</div>';
  try {
    const s = await api('/api/long-term/tax/summary/'+year);
    const totalCls = s.total_realized >= 0 ? 'gain' : 'loss';
    out.innerHTML = `
      <div class="card">
        <div class="row"><span class="l">Dispositions in ${year}</span><span class="v">${s.dispositions}</span></div>
        <div class="row"><span class="l">Total proceeds</span><span class="v">${usd(s.total_proceeds)}</span></div>
        <div class="row"><span class="l">Short-term realised</span><span class="v ${s.short_term_realized>=0?'gain':'loss'}">${usd(s.short_term_realized)}</span></div>
        <div class="row"><span class="l">Long-term realised</span><span class="v ${s.long_term_realized>=0?'gain':'loss'}">${usd(s.long_term_realized)}</span></div>
        <div class="row"><span class="l"><b>Total realised P&amp;L</b></span><span class="v ${totalCls}"><b>${usd(s.total_realized)}</b></span></div>
        <div class="row"><span class="l">Estimated tax</span><span class="v">${usd(s.estimated_tax)}</span></div>
        ${s.loss_carryforward_to_next_year > 0 ? `<div class="row"><span class="l">Loss carryforward to ${year+1}</span><span class="v">${usd(s.loss_carryforward_to_next_year)}</span></div>` : ''}
      </div>`;
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
  refreshExportLink();
};

function refreshExportLink(){
  const year = parseInt(document.getElementById('tx-year').value) || new Date().getFullYear();
  document.getElementById('tx-export').href = '/api/long-term/tax/export/' + year;
}

document.getElementById('ps-go').onclick = async () => {
  const ticker = document.getElementById('ps-ticker').value;
  const qty = document.getElementById('ps-qty').value;
  const method = document.getElementById('ps-method').value;
  const priceStr = document.getElementById('ps-price').value;
  const out = document.getElementById('ps-out');
  out.innerHTML = '<div style="color:var(--muted)">Computing…</div>';
  let url = `/api/long-term/tax/preview-sell?ticker=${ticker}&qty=${qty}&method=${method}`;
  if (priceStr) url += `&sell_price=${priceStr}`;
  try {
    const p = await api(url);
    if (!p.filled_qty) { out.innerHTML = `<div class="note">${p.note || 'no fillable qty'}</div>`; return; }
    const totalCls = p.total_realized >= 0 ? 'gain' : 'loss';
    let html = `<div class="card">
      <div class="row"><span class="l">Filled</span><span class="v">${fmt(p.filled_qty,8)} ${p.ticker} @ ${usd(p.sell_price)}</span></div>
      <div class="row"><span class="l">Total proceeds</span><span class="v">${usd(p.total_proceeds)}</span></div>
      <div class="row"><span class="l">Total cost basis</span><span class="v">${usd(p.total_cost_basis)}</span></div>
      <div class="row"><span class="l"><b>Realised gain/loss</b></span><span class="v ${totalCls}"><b>${usd(p.total_realized)}</b></span></div>
      <div class="row"><span class="l">  ↳ short-term</span><span class="v ${p.st_realized>=0?'gain':'loss'}">${usd(p.st_realized)}</span></div>
      <div class="row"><span class="l">  ↳ long-term</span><span class="v ${p.lt_realized>=0?'gain':'loss'}">${usd(p.lt_realized)}</span></div>
      ${p.shortfall > 0 ? `<div class="note">Shortfall: ${fmt(p.shortfall,8)} (not enough open lots)</div>` : ''}
      <table style="margin-top:10px"><thead><tr><th>Acquired</th><th>Qty</th><th>Cost/u</th><th>Realised</th><th>LT/ST</th><th>Days</th></tr></thead><tbody>`;
    for (const pk of p.picks) {
      const cls = pk.realized_gain >= 0 ? 'gain' : 'loss';
      html += `<tr><td>${pk.acquired_at}</td><td>${fmt(pk.consumed_qty,8)}</td><td>${usd(pk.cost_basis)}</td>
        <td class="${cls}">${usd(pk.realized_gain)}</td><td>${pk.classification}</td><td>${pk.days_held}</td></tr>`;
    }
    html += `</tbody></table></div>`;
    out.innerHTML = html;
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
};

async function loadLots(){
  const tbody = document.querySelector('#lots-table tbody');
  tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const r = await api('/api/long-term/tax/lots');
    if (!r.lots.length) { tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">No open lots.</td></tr>'; return; }
    tbody.innerHTML = '';
    for (const l of r.lots) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${l.acquired_at}</td><td>${l.ticker}</td>
        <td>${fmt(l.qty_original,8)}</td><td>${fmt(l.qty_consumed,8)}</td>
        <td><b>${fmt(l.qty_remaining,8)}</b></td>
        <td>${usd(l.cost_basis)}</td>
        <td style="font-size:.85em">${l.long_term_at}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="7" class="err">${e.message}</td></tr>`; }
}

async function loadHarvest(){
  const tbody = document.querySelector('#hv-table tbody');
  const tot = document.getElementById('hv-totals');
  tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">Scanning…</td></tr>';
  tot.innerHTML = '';
  try {
    const r = await api('/api/long-term/tax/harvest');
    if (!r.opportunities.length) {
      tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">No harvest opportunities meeting your thresholds.</td></tr>';
      return;
    }
    tot.innerHTML = `<b>Total unrealised loss available:</b> <span class="loss">${usd(r.total_unrealized_loss_usd)}</span> · <b>Est. tax savings:</b> <span class="gain">${usd(r.estimated_total_tax_save_usd)}</span>`;
    tbody.innerHTML = '';
    for (const o of r.opportunities) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${o.ticker}</td><td>${fmt(o.qty_remaining,8)}</td>
        <td>${usd(o.cost_basis)}</td><td>${usd(o.current_price)}</td>
        <td class="loss"><b>${usd(o.unrealized_loss_usd)}</b></td>
        <td class="loss">${pct(o.unrealized_loss_pct)}</td>
        <td>${o.days_held}</td><td>${o.classification}</td>
        <td>${o.wash_sale_risk ? '<span class="r-defensive">⚠ yes</span>' : '<span class="r-calm">no</span>'}</td>
        <td class="gain">${usd(o.estimated_tax_save_usd)}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`; }
}

document.getElementById('hv-execute').onclick = async () => {
  const msg = document.getElementById('hv-msg'); msg.innerHTML = '';
  if (!confirm('Place sell orders for ALL current harvest opportunities? They will be evaluated through the safety gauntlet (dry-run mode applies). Tax dispositions are created automatically on fill.')) return;
  try {
    const r = await api('/api/long-term/tax/harvest/execute', {method:'POST', body: JSON.stringify({})});
    const placed = r.decisions.filter(d => d.action === 'placed').length;
    const dry = r.decisions.filter(d => d.action === 'dry_run').length;
    const blocked = r.decisions.filter(d => d.action === 'blocked' || d.action === 'skipped').length;
    msg.innerHTML = `<span class="ok">${placed} placed, ${dry} dry-run, ${blocked} blocked/skipped.</span>`;
    loadHarvest(); loadExecLog && loadExecLog();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

async function loadDispositions(){
  const tbody = document.querySelector('#dp-table tbody');
  tbody.innerHTML = '<tr><td colspan="9" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const r = await api('/api/long-term/tax/dispositions');
    if (!r.dispositions.length) { tbody.innerHTML = '<tr><td colspan="9" style="color:var(--muted)">No dispositions yet.</td></tr>'; return; }
    tbody.innerHTML = '';
    for (const d of r.dispositions) {
      const realCls = d.realized_gain >= 0 ? 'gain' : 'loss';
      const lots = d.consumption ? d.consumption.length : 0;
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${d.sell_date}</td><td>${d.ticker}</td><td>${fmt(d.qty,8)}</td>
        <td>${usd(d.sell_price)}</td><td>${d.method}</td>
        <td class="${realCls}">${usd(d.realized_gain)}</td>
        <td class="${d.lt_gain>=0?'gain':'loss'}">${usd(d.lt_gain)}</td>
        <td class="${d.st_gain>=0?'gain':'loss'}">${usd(d.st_gain)}</td>
        <td>${lots}</td>`;
      tbody.appendChild(tr);
    }
  } catch(e) { tbody.innerHTML = `<tr><td colspan="9" class="err">${e.message}</td></tr>`; }
}

document.getElementById('dp-add').onclick = async () => {
  const msg = document.getElementById('dp-msg'); msg.innerHTML = '';
  try {
    await api('/api/long-term/tax/dispositions', {method:'POST', body: JSON.stringify({
      ticker: document.getElementById('dp-ticker').value,
      qty: document.getElementById('dp-qty').value,
      sell_price: document.getElementById('dp-price').value,
      sell_date: document.getElementById('dp-date').value || undefined,
    })});
    msg.innerHTML = '<span class="ok">Recorded.</span>';
    loadDispositions(); loadLots(); loadHarvest();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

// ── Strategies ──────────────────────────────────────────────────────────────

function readEditor() {
  return {
    name: document.getElementById('st-name').value || 'untitled',
    description: document.getElementById('st-desc').value,
    base_ticker: document.getElementById('st-ticker').value,
    starting_capital_usd: parseFloat(document.getElementById('st-cap').value || 10000),
    dca_enabled: document.getElementById('st-dca').checked,
    dca_amount_usd: parseFloat(document.getElementById('st-dca-amt').value || 100),
    dca_frequency: document.getElementById('st-dca-freq').value,
    bullish_dd_threshold: parseFloat(document.getElementById('st-bdd').value),
    bullish_dd_multiplier: parseFloat(document.getElementById('st-bddm').value),
    bearish_mayer_threshold: parseFloat(document.getElementById('st-bmt').value),
    bearish_mayer_multiplier: parseFloat(document.getElementById('st-bmm').value),
    pause_mayer_threshold: parseFloat(document.getElementById('st-pmt').value),
    harvest_enabled: document.getElementById('st-harv').checked,
    harvest_min_loss_usd: parseFloat(document.getElementById('st-hmin').value || 100),
    harvest_min_age_days: parseInt(document.getElementById('st-hage').value || 30),
  };
}

function writeEditor(s) {
  document.getElementById('st-id').value = s.id || '';
  document.getElementById('st-name').value = s.name || '';
  document.getElementById('st-desc').value = s.description || '';
  document.getElementById('st-ticker').value = s.rules?.base_ticker || s.base_ticker || 'BTC';
  document.getElementById('st-cap').value = s.rules?.starting_capital_usd || s.starting_capital_usd || 10000;
  document.getElementById('st-vis').value = s.visibility || 'private';
  const r = s.rules || {};
  document.getElementById('st-dca').checked = r.dca_enabled !== false;
  document.getElementById('st-dca-amt').value = r.dca_amount_usd ?? 100;
  document.getElementById('st-dca-freq').value = r.dca_frequency || 'weekly';
  document.getElementById('st-bdd').value = r.bullish_dd_threshold ?? -0.40;
  document.getElementById('st-bddm').value = r.bullish_dd_multiplier ?? 2.0;
  document.getElementById('st-bmt').value = r.bearish_mayer_threshold ?? 2.4;
  document.getElementById('st-bmm').value = r.bearish_mayer_multiplier ?? 0.5;
  document.getElementById('st-pmt').value = r.pause_mayer_threshold ?? 2.7;
  document.getElementById('st-harv').checked = r.harvest_enabled === true;
  document.getElementById('st-hmin').value = r.harvest_min_loss_usd ?? 100;
  document.getElementById('st-hage').value = r.harvest_min_age_days ?? 30;
}

function resetEditor() {
  writeEditor({rules: {}, visibility: 'private'});
  document.getElementById('st-result').innerHTML = '';
  document.getElementById('st-msg').innerHTML = '';
}

function loadStrategies() { loadMyStrategies(); loadSubscriptions(); }

async function loadSubscriptions(){
  const out = document.getElementById('sub-list');
  out.innerHTML = '<div style="color:var(--muted);font-size:.85em">Loading…</div>';
  try {
    const r = await api('/api/long-term/subscriptions');
    if (!r.subscriptions.length) { out.innerHTML = '<div class="note">No live subscriptions yet — subscribe from the Leaderboard.</div>'; return; }
    out.innerHTML = '';
    for (const s of r.subscriptions) {
      const card = document.createElement('div'); card.className = 'card';
      const status = s.paused ? '<span class="r-watchful">paused</span>' : s.active ? '<span class="r-calm">active</span>' : '<span class="r-neutral">inactive</span>';
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:start;gap:10px">
          <div>
            <h3 style="margin:0 0 4px">${s.strategy_name} ${status}</h3>
            <div style="font-size:.85em;color:var(--muted)">${s.base_ticker} · last run: ${s.last_run_at || 'never'} · next: ${s.next_run_at || 'asap'}</div>
            ${s.last_action ? `<div style="font-size:.85em;margin-top:4px">Last action: ${s.last_action}</div>` : ''}
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">
            <button class="ghost" data-run="${s.id}">Run now</button>
            <button class="ghost" data-pause="${s.id}" data-paused="${s.paused}">${s.paused ? 'Resume' : 'Pause'}</button>
            <button class="ghost danger" data-delsub="${s.id}">Unsubscribe</button>
          </div>
        </div>`;
      out.appendChild(card);
    }
    out.querySelectorAll('button[data-run]').forEach(b => b.onclick = async () => {
      const r = await api('/api/long-term/subscriptions/'+b.dataset.run+'/run-now', {method:'POST'});
      alert('Ran. ' + r.decisions.map(d => `${d.action}: ${d.reason}`).join(' | '));
      loadSubscriptions();
    });
    out.querySelectorAll('button[data-pause]').forEach(b => b.onclick = async () => {
      const paused = b.dataset.paused === 'true' || b.dataset.paused === '1';
      await api('/api/long-term/subscriptions/'+b.dataset.pause+'/pause', {method:'POST', body: JSON.stringify({paused: !paused})});
      loadSubscriptions();
    });
    out.querySelectorAll('button[data-delsub]').forEach(b => b.onclick = async () => {
      if (!confirm('Unsubscribe?')) return;
      await api('/api/long-term/subscriptions/'+b.dataset.delsub, {method:'DELETE'});
      loadSubscriptions();
    });
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

async function loadMyStrategies(){
  const out = document.getElementById('my-list');
  out.innerHTML = '<div style="color:var(--muted)">Loading…</div>';
  try {
    const r = await api('/api/long-term/strategies');
    if (!r.strategies.length) { out.innerHTML = '<div class="note">No strategies yet — switch to "New / edit".</div>'; return; }
    out.innerHTML = '';
    for (const s of r.strategies) {
      const bt = s.latest_backtest;
      const ret = bt ? pct(bt.total_return_pct) : '—';
      const sharpe = bt && bt.sharpe != null ? fmt(bt.sharpe, 2) : '—';
      const card = document.createElement('div'); card.className = 'card';
      card.innerHTML = `
        <h3>${s.name} <span class="pill ${s.visibility==='public'?'p-expansion':'p-neutral'}">${s.visibility}</span></h3>
        <div class="row"><span class="l">Asset</span><span class="v">${s.base_ticker}</span></div>
        <div class="row"><span class="l">Starting capital</span><span class="v">${usd(s.starting_capital_usd)}</span></div>
        <div class="row"><span class="l">Latest backtest return</span><span class="v">${ret}</span></div>
        <div class="row"><span class="l">Sharpe</span><span class="v">${sharpe}</span></div>
        <div class="actionrow" style="margin-top:8px">
          <button class="ghost" data-edit="${s.id}">Edit</button>
          <button class="ghost" data-backtest="${s.id}">Backtest</button>
        </div>`;
      out.appendChild(card);
    }
    out.querySelectorAll('button[data-edit]').forEach(b => b.onclick = () => editStrategy(parseInt(b.dataset.edit)));
    out.querySelectorAll('button[data-backtest]').forEach(b => b.onclick = () => runBacktest(parseInt(b.dataset.backtest)));
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

async function loadLeaderboard(){
  const tbody = document.querySelector('#lb-table tbody');
  tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">Loading…</td></tr>';
  try {
    const r = await api('/api/long-term/strategies/leaderboard');
    if (!r.leaderboard.length) { tbody.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">No backtested public strategies yet.</td></tr>'; return; }
    tbody.innerHTML = '';
    r.leaderboard.forEach((row, i) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${i+1}</td><td>${row.name}</td><td>${row.base_ticker}</td>
        <td class="${row.total_return_pct>=0?'gain':'loss'}">${pct(row.total_return_pct)}</td>
        <td>${fmt(row.sharpe,2)}</td><td>${fmt(row.sortino,2)}</td>
        <td class="loss">${pct(row.max_drawdown_pct)}</td>
        <td>${row.trade_count}</td>
        <td><b>${fmt(row.score,2)}</b></td>
        <td><button class="ghost" data-fork="${row.id}">Fork</button>
            <button data-sub="${row.id}">Subscribe</button></td>`;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll('button[data-fork]').forEach(b => b.onclick = () => forkStrategy(parseInt(b.dataset.fork)));
    tbody.querySelectorAll('button[data-sub]').forEach(b => b.onclick = () => subscribeStrategy(parseInt(b.dataset.sub)));
  } catch(e) { tbody.innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`; }
}

async function subscribeStrategy(id){
  if (!confirm('Subscribe to this strategy?\n\nIt will run every 5 min through the safety gauntlet (dry-run mode applies until you explicitly turn it off).')) return;
  try {
    await api('/api/long-term/strategies/'+id+'/subscribe', {method:'POST'});
    alert('Subscribed. Check the "My strategies" sub-tab for the live subscription card.');
    loadMyStrategies();
  } catch(e) { alert(e.message); }
}

async function loadMarketplace(){
  const out = document.getElementById('market-list');
  out.innerHTML = '<div style="color:var(--muted)">Loading…</div>';
  try {
    const r = await api('/api/long-term/strategies/marketplace');
    if (!r.strategies.length) { out.innerHTML = '<div class="note">No public strategies yet.</div>'; return; }
    out.innerHTML = '';
    for (const s of r.strategies) {
      const bt = s.latest_backtest;
      const card = document.createElement('div'); card.className = 'card';
      card.innerHTML = `
        <h3>${s.name}</h3>
        <div class="note">${s.description || ''}</div>
        <div class="row"><span class="l">Asset · capital</span><span class="v">${s.base_ticker} · ${usd(s.starting_capital_usd)}</span></div>
        ${bt ? `
        <div class="row"><span class="l">Return</span><span class="v ${bt.total_return_pct>=0?'gain':'loss'}">${pct(bt.total_return_pct)}</span></div>
        <div class="row"><span class="l">Sharpe / Max DD</span><span class="v">${fmt(bt.sharpe,2)} / ${pct(bt.max_drawdown_pct)}</span></div>
        ` : '<div class="note">No backtest yet</div>'}
        <div class="actionrow" style="margin-top:8px">
          <button class="ghost" data-fork="${s.id}">Fork to my library</button>
        </div>`;
      out.appendChild(card);
    }
    out.querySelectorAll('button[data-fork]').forEach(b => b.onclick = () => forkStrategy(parseInt(b.dataset.fork)));
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

async function editStrategy(id){
  try {
    const s = await api('/api/long-term/strategies/'+id);
    writeEditor(s);
    document.querySelectorAll('[data-stab]').forEach(x => x.classList.remove('active'));
    document.querySelector('[data-stab="edit"]').classList.add('active');
    document.querySelectorAll('[data-sub]').forEach(x => x.hidden = x.dataset.sub !== 'edit');
    // If there's a recent backtest, render it.
    if (s.latest_backtest) renderBacktest(s.latest_backtest);
  } catch(e) { alert(e.message); }
}

async function forkStrategy(id){
  try {
    const r = await api('/api/long-term/strategies/'+id+'/fork', {method:'POST', body: JSON.stringify({})});
    alert('Forked to your library (id ' + r.id + ').');
    loadMyStrategies();
  } catch(e) { alert(e.message); }
}

document.getElementById('st-save').onclick = async () => {
  const msg = document.getElementById('st-msg'); msg.innerHTML = '';
  const id = document.getElementById('st-id').value;
  const body = {rules: readEditor(), visibility: document.getElementById('st-vis').value};
  try {
    if (id) {
      await api('/api/long-term/strategies/'+id, {method:'PUT', body: JSON.stringify(body)});
      msg.innerHTML = '<span class="ok">Updated.</span>';
    } else {
      const r = await api('/api/long-term/strategies', {method:'POST', body: JSON.stringify(body)});
      document.getElementById('st-id').value = r.id;
      msg.innerHTML = '<span class="ok">Created (id '+r.id+').</span>';
    }
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

document.getElementById('st-backtest').onclick = async () => {
  const msg = document.getElementById('st-msg'); msg.innerHTML = ' saving + running…';
  document.getElementById('st-save').onclick();  // not awaited but fires save flow
  let id = document.getElementById('st-id').value;
  if (!id) {
    // Save first to get an id.
    const body = {rules: readEditor(), visibility: document.getElementById('st-vis').value};
    try {
      const r = await api('/api/long-term/strategies', {method:'POST', body: JSON.stringify(body)});
      id = r.id;
      document.getElementById('st-id').value = id;
    } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; return; }
  }
  try {
    const result = await api('/api/long-term/strategies/'+id+'/backtest', {method:'POST'});
    msg.innerHTML = '<span class="ok">Backtest done.</span>';
    renderBacktest(result);
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

document.getElementById('st-delete').onclick = async () => {
  const id = document.getElementById('st-id').value;
  if (!id) { resetEditor(); return; }
  if (!confirm('Delete this strategy and its backtests?')) return;
  try {
    await api('/api/long-term/strategies/'+id, {method:'DELETE'});
    resetEditor();
    loadMyStrategies();
  } catch(e) { document.getElementById('st-msg').innerHTML = '<span class="err">'+e.message+'</span>'; }
};

async function runBacktest(id){
  try {
    const result = await api('/api/long-term/strategies/'+id+'/backtest', {method:'POST'});
    // Switch to edit tab with this strategy and render the result.
    const s = await api('/api/long-term/strategies/'+id);
    writeEditor(s);
    document.querySelectorAll('[data-stab]').forEach(x => x.classList.remove('active'));
    document.querySelector('[data-stab="edit"]').classList.add('active');
    document.querySelectorAll('[data-sub]').forEach(x => x.hidden = x.dataset.sub !== 'edit');
    renderBacktest(result);
  } catch(e) { alert(e.message); }
}

function renderBacktest(r){
  if (!r) return;
  const isLatestRow = r.start_date && r.final_value_usd != null;
  const totalRet = r.total_return_pct;
  const sharpe = r.sharpe;
  const sortino = r.sortino;
  const maxDD = r.max_drawdown_pct;

  // SVG sparkline of the equity curve.
  let svg = '';
  if (r.equity_curve && r.equity_curve.length > 1) {
    const w = 600, h = 140, p = 8;
    const vals = r.equity_curve.map(x => x[1]);
    const min = Math.min(...vals), max = Math.max(...vals);
    const range = (max - min) || 1;
    const dx = (w - 2*p) / (vals.length - 1);
    const pts = vals.map((v,i) => `${(p + i*dx).toFixed(1)},${(h - p - ((v - min)/range)*(h-2*p)).toFixed(1)}`).join(' ');
    svg = `<svg viewBox="0 0 ${w} ${h}" width="100%" height="160" style="background:var(--card2);border-radius:8px;margin-top:10px">
      <polyline fill="none" stroke="${totalRet>=0?'#22c55e':'#ef4444'}" stroke-width="2" points="${pts}"/>
      <text x="${p}" y="${p+10}" fill="#7d8a99" font-size="10">${usd(min)}</text>
      <text x="${p}" y="${h-2}" fill="#7d8a99" font-size="10">${r.start_date || r.equity_curve[0][0]}</text>
      <text x="${w-p}" y="${p+10}" fill="#7d8a99" font-size="10" text-anchor="end">${usd(max)}</text>
      <text x="${w-p}" y="${h-2}" fill="#7d8a99" font-size="10" text-anchor="end">${r.end_date || r.equity_curve[r.equity_curve.length-1][0]}</text>
    </svg>`;
  }
  const cls = (v) => v == null ? '' : (v >= 0 ? 'gain' : 'loss');
  document.getElementById('st-result').innerHTML = `
    <div class="card">
      <h3>Backtest result</h3>
      <div class="row"><span class="l">Window</span><span class="v">${r.start_date} → ${r.end_date} (${r.days||'—'} days)</span></div>
      <div class="row"><span class="l">Final value</span><span class="v">${usd(r.final_value_usd)}</span></div>
      <div class="row"><span class="l">Total return</span><span class="v ${cls(totalRet)}"><b>${pct(totalRet)}</b></span></div>
      <div class="row"><span class="l">Sharpe / Sortino</span><span class="v">${fmt(sharpe,2)} / ${fmt(sortino,2)}</span></div>
      <div class="row"><span class="l">Max drawdown</span><span class="v loss">${pct(maxDD)}</span></div>
      <div class="row"><span class="l">Trades</span><span class="v">${r.trade_count} (${r.buys||0} buys, ${r.sells||0} sells)</span></div>
      ${r.final_qty != null ? `<div class="row"><span class="l">Final position</span><span class="v">${fmt(r.final_qty,6)} ${r.ticker || ''}, ${usd(r.final_cash_usd)} cash</span></div>` : ''}
      ${svg}
    </div>`;
}

// ── Push notifications + PWA ───────────────────────────────────────────────

function urlBase64ToUint8Array(b64){
  const padding = '='.repeat((4 - b64.length % 4) % 4);
  const base64 = (b64 + padding).replace(/-/g,'+').replace(/_/g,'/');
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i=0; i<raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function registerServiceWorker(){
  if (!('serviceWorker' in navigator)) return null;
  try {
    return await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
  } catch(e) { console.warn('SW register failed:', e); return null; }
}

async function pushEnable(){
  const msg = document.getElementById('push-msg');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    msg.innerHTML = '<span class="err">Push not supported in this browser.</span>';
    return;
  }
  msg.innerHTML = ' requesting permission…';
  try {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { msg.innerHTML = '<span class="err">Permission denied.</span>'; return; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const {key} = await api('/api/push/vapid-key');
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
    }
    await api('/api/push/subscribe', {method:'POST', body: JSON.stringify({subscription: sub})});
    msg.innerHTML = '<span class="ok">Enabled on this device.</span>';
    loadPushSubs();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
}
document.getElementById('push-enable').onclick = pushEnable;

document.getElementById('push-test').onclick = async () => {
  const msg = document.getElementById('push-msg'); msg.innerHTML = ' sending…';
  try {
    const r = await api('/api/push/test', {method:'POST'});
    msg.innerHTML = `<span class="ok">Sent to ${r.sent} device(s).</span>`;
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

document.getElementById('push-disable').onclick = async () => {
  const msg = document.getElementById('push-msg');
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api('/api/push/subscribe', {method:'DELETE', body: JSON.stringify({endpoint: sub.endpoint})});
      await sub.unsubscribe();
    }
    msg.innerHTML = '<span class="ok">Disabled on this device.</span>';
    loadPushSubs();
  } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
};

async function loadPushSubs(){
  const out = document.getElementById('push-subs');
  try {
    const r = await api('/api/push/subscriptions');
    if (!r.subscriptions.length) { out.innerHTML = '<div class="note">No devices subscribed yet.</div>'; return; }
    out.innerHTML = '<div class="note"><b>Subscribed devices:</b></div>' + r.subscriptions.map(s =>
      `<div style="font-size:.85em;color:var(--muted);margin:3px 0">${s.user_agent || 'unknown'} — since ${s.created_at}</div>`
    ).join('');
  } catch(e) { out.innerHTML = `<div class="err">${e.message}</div>`; }
}

// Extend loadExecution to also load push state
const _prevLoadExecution = loadExecution;
loadExecution = async function(){
  await _prevLoadExecution();
  await loadPushSubs();
};

// Register SW eagerly so the cache primes while the user browses.
registerServiceWorker();

// ── Onboarding wizard ──────────────────────────────────────────────────────

const ONB_TITLES = {
  welcome: 'Welcome',
  jurisdiction: 'Tax setup',
  exchange: 'Connect an exchange',
  targets: 'Target weights',
  strategy: 'Pick a strategy',
  push: 'Notifications',
  done: 'All set',
};

async function loadOnboarding(){
  try {
    const state = await api('/api/onboarding/state');
    if (state.completed) return;  // skip wizard if already done
    renderOnboarding(state);
  } catch(e) { /* not authenticated or other issue — just skip */ }
}

function renderOnboarding(state){
  const overlay = document.getElementById('onboarding-overlay');
  overlay.hidden = false;
  const steps = state.steps;
  const idx = steps.indexOf(state.step);
  // Progress bar
  const prog = document.getElementById('onb-progress');
  prog.innerHTML = '';
  steps.forEach((s, i) => {
    const seg = document.createElement('div');
    seg.style.cssText = `flex:1;height:4px;border-radius:2px;background:${i <= idx ? 'var(--blue)' : 'var(--card2)'}`;
    prog.appendChild(seg);
  });

  const content = document.getElementById('onb-content');
  if (state.step === 'welcome') {
    content.innerHTML = `
      <p>CryptoEdge is a long-term holding workbench: cycle-aware DCA, on-chain analytics, tax-optimal selling, and live execution on Coinbase / Kraken.</p>
      <p>This 5-minute setup wires the basics. Everything starts in <b>dry-run</b> — real orders never go out until you explicitly turn that off.</p>
      <div class="actionrow" style="margin-top:14px"><button id="onb-next">Get started</button></div>`;
    document.getElementById('onb-next').onclick = () => onbAdvance('welcome', {});
    return;
  }

  if (state.step === 'jurisdiction') {
    content.innerHTML = `
      <p>Your jurisdiction picks the default lot-selection method (HIFO for US, Section 104 pool for UK, FIFO for DE) and the long-term capital-gains threshold.</p>
      <div class="actionrow">
        <div class="field"><label>Jurisdiction</label>
          <select id="onb-jur"><option value="US">US</option><option value="UK">UK</option><option value="DE">DE</option></select>
        </div>
        <div class="field"><label>Default lot method</label>
          <select id="onb-method">
            <option value="HIFO">HIFO (minimise gain)</option>
            <option value="FIFO">FIFO</option>
            <option value="LIFO">LIFO</option>
            <option value="POOL">Section 104 pool (UK)</option>
            <option value="TAX_OPTIMAL">Tax-optimal heuristic</option>
          </select>
        </div>
      </div>
      <div class="actionrow" style="margin-top:14px"><button id="onb-next">Continue</button></div>`;
    document.getElementById('onb-next').onclick = () => onbAdvance('jurisdiction', {
      jurisdiction: document.getElementById('onb-jur').value,
      default_lot_method: document.getElementById('onb-method').value,
    });
    return;
  }

  if (state.step === 'exchange') {
    content.innerHTML = `
      <p>Connecting an exchange enables auto-execution. You can skip and configure later in the Execution tab.</p>
      <p class="note">Use API keys with <b>view + trade only</b> — never enable withdraw.</p>
      <div class="actionrow">
        <button id="onb-next">I'll do this later (skip)</button>
        <button id="onb-go-exchange" class="ghost">Open Execution tab</button>
      </div>`;
    document.getElementById('onb-next').onclick = () => onbAdvance('exchange', {connected: false});
    document.getElementById('onb-go-exchange').onclick = () => {
      onbAdvance('exchange', {connected: false}).then(() => {
        document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
        document.querySelector('[data-tab="execution"]').classList.add('active');
        document.querySelectorAll('section').forEach(s => s.hidden = s.dataset.section !== 'execution');
        loadExecution();
      });
    };
    return;
  }

  if (state.step === 'targets') {
    content.innerHTML = `
      <p>Set portfolio target weights — the rebalance recommender will keep your holdings within drift bands. The defaults below are a "majors-only" allocation; tweak any row to 0 to drop an asset.</p>
      <div id="onb-targets" class="grid"></div>
      <div class="actionrow" style="margin-top:14px"><button id="onb-next">Continue</button></div>`;
    const defaults = [['BTC', 0.50], ['ETH', 0.30], ['SOL', 0.15], ['DOGE', 0.0], ['XRP', 0.05]];
    const grid = document.getElementById('onb-targets');
    defaults.forEach(([tk, w]) => {
      const card = document.createElement('div'); card.className = 'card';
      card.innerHTML = `<div class="field"><label>${tk} weight</label><input class="onb-w" data-ticker="${tk}" type="number" step="0.01" min="0" max="1" value="${w}"></div>`;
      grid.appendChild(card);
    });
    document.getElementById('onb-next').onclick = () => {
      const targets = [...document.querySelectorAll('.onb-w')].map(el => ({
        ticker: el.dataset.ticker, weight: parseFloat(el.value || 0),
      }));
      const total = targets.reduce((a, b) => a + b.weight, 0);
      if (total > 1.01) { alert(`Weights sum to ${total.toFixed(2)}, must be ≤ 1.0`); return; }
      onbAdvance('targets', {targets});
    };
    return;
  }

  if (state.step === 'strategy') {
    content.innerHTML = `<p>Pick a starter strategy from the leaderboard, or skip to author your own later.</p><div id="onb-lb" style="margin:10px 0"></div>
      <div class="actionrow"><button id="onb-skip-strat">Skip</button></div>`;
    document.getElementById('onb-skip-strat').onclick = () => onbAdvance('strategy', {});
    api('/api/long-term/strategies/leaderboard').then(r => {
      const lb = document.getElementById('onb-lb');
      if (!r.leaderboard.length) { lb.innerHTML = '<div class="note">No public strategies yet — skip and build your own.</div>'; return; }
      lb.innerHTML = '';
      for (const s of r.leaderboard.slice(0, 5)) {
        const card = document.createElement('div'); card.className = 'card';
        card.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div><b>${s.name}</b> — ${s.base_ticker}<br>
          <span style="font-size:.85em;color:var(--muted)">Return ${pct(s.total_return_pct)} · Sharpe ${fmt(s.sharpe,2)} · MaxDD ${pct(s.max_drawdown_pct)}</span></div>
          <button data-onb-pick="${s.id}">Subscribe</button></div>`;
        lb.appendChild(card);
      }
      lb.querySelectorAll('button[data-onb-pick]').forEach(b => b.onclick = () => {
        onbAdvance('strategy', {strategy_id: parseInt(b.dataset.onbPick)});
      });
    }).catch(() => {});
    return;
  }

  if (state.step === 'push') {
    content.innerHTML = `
      <p>Push notifications fire on cycle-indicator alerts, DCA fills, and the portfolio circuit breaker.</p>
      <div class="actionrow"><button id="onb-push-yes">Enable push</button><button id="onb-push-no" class="ghost">Skip</button></div>
      <div id="onb-push-msg" style="margin-top:8px"></div>`;
    document.getElementById('onb-push-yes').onclick = async () => {
      const msg = document.getElementById('onb-push-msg');
      msg.textContent = ' requesting permission…';
      try {
        await pushEnable();
        msg.textContent = '';
        onbAdvance('push', {enabled: true});
      } catch(e) { msg.innerHTML = '<span class="err">'+e.message+'</span>'; }
    };
    document.getElementById('onb-push-no').onclick = () => onbAdvance('push', {enabled: false});
    return;
  }

  if (state.step === 'done') {
    content.innerHTML = `<p>You're set up. The dashboard is loaded below this card. A few things to know:</p>
      <ul>
        <li><b>Dry-run is on by default</b> — flip it off in the Execution tab when you're ready.</li>
        <li>The cycle-aware DCA recommender uses Mayer multiple + drawdown. Pause threshold is at Mayer 2.7.</li>
        <li>The Strategies tab has a backtester. Public strategies appear on the leaderboard.</li>
      </ul>
      <div class="actionrow"><button id="onb-close">Open dashboard</button></div>`;
    document.getElementById('onb-close').onclick = () => {
      document.getElementById('onboarding-overlay').hidden = true;
    };
  }
}

async function onbAdvance(step, payload){
  try {
    const r = await api('/api/onboarding/advance', {method:'POST', body: JSON.stringify({step, payload})});
    if (r.completed) {
      document.getElementById('onboarding-overlay').hidden = true;
      return;
    }
    renderOnboarding({...r, steps: ['welcome','jurisdiction','exchange','targets','strategy','push','done']});
  } catch(e) { alert(e.message); }
}

document.getElementById('onb-skip').onclick = async () => {
  await api('/api/onboarding/skip', {method:'POST'});
  document.getElementById('onboarding-overlay').hidden = true;
};

loadOnboarding();
loadOverview();
</script>
</div></body></html>
"""


# ===================================================================
# CROSS-DASHBOARD SHARE ENDPOINT (localhost-only, for sibling services)
# ===================================================================

@app.get("/api/share/snapshot")
async def share_snapshot(request: Request):
    """Lightweight crypto summary for cross-dashboard integration."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    signals = _get_bot_signals()
    summary = {}
    for ticker, sig in signals.items():
        summary[ticker] = {
            "ticker": ticker,
            "price": sig.get("price", 0),
            "volatility": sig.get("volatility_label", "UNKNOWN"),
            "rsi": round(sig.get("rsi", 50), 1),
            "win_rate": round(sig.get("hist_win_rate", 50), 1),
            "gain_loss_ratio": round(sig.get("gain_loss_ratio", 0), 3),
            "momentum_decay": round(sig.get("momentum_decay", 1), 3),
            "current_delta": round(sig.get("current_delta", 0), 4),
            "pct_gaining": round(sig.get("pct_seconds_gaining", 50), 1),
        }
    return {"assets": summary, "count": len(summary)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
