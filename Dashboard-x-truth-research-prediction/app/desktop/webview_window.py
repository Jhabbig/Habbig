from __future__ import annotations
import webview

def open_webview(url: str):
    window = webview.create_window(title="Polymarket Signal Dashboard", url=url, width=1400, height=900, min_size=(900, 600), resizable=True, on_top=False, frameless=False)
    webview.start(gui="cocoa", debug=False, http_server=False)
