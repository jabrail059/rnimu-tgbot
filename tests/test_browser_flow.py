"""Optional browser acceptance test using a locally installed Firefox + geckodriver.

No browser downloads or real Telegram/YooKassa calls. Skips if binaries are absent.
"""
import asyncio
import base64
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import Response

from test_library_api import project, signed_data, photo_bytes
from test_documents import pdf_bytes
from app.web import create_app


def binary(name, fallback):
    configured = os.environ.get(name)
    if configured:
        return configured
    return fallback if Path(fallback).is_file() else None


FIREFOX = binary("FIREFOX_BINARY", "/snap/firefox/current/usr/lib/firefox/firefox") or shutil.which("firefox")
GECKO = binary("GECKODRIVER_BINARY", "/snap/firefox/current/usr/lib/firefox/geckodriver") or shutil.which("geckodriver")
pytestmark = pytest.mark.skipif(not FIREFOX or not GECKO, reason="Local Firefox/geckodriver are required")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_admin_reader_gallery_and_privacy(project, tmp_path):
    _, db, settings = project
    destination_id = asyncio.run(db.save_category("Другой раздел"))
    app_port, driver_port = free_port(), free_port()
    app = create_app(replace(settings, public_base_url=f"http://127.0.0.1:{app_port}"), db)
    html = Path("web/index.html").read_text()

    @app.get("/test-shell")
    def shell(uid: int = 1):
        return Response(html.replace("https://telegram.org/js/telegram-web-app.js", f"/test-sdk.js?uid={uid}"), media_type="text/html")

    @app.get("/test-sdk.js")
    def sdk(uid: int = 1):
        import json
        return Response("window.Telegram = {WebApp: {initData: " + json.dumps(signed_data(uid)) + """,
          ready() {}, expand() {}, close() {}, isActive: true, isFullscreen: false, events: {},
          requestFullscreen() {this.isFullscreen=true; this.events.fullscreenChanged?.()},
          exitFullscreen() {this.isFullscreen=false; this.events.fullscreenChanged?.()},
          isVersionAtLeast() {return true}, enableClosingConfirmation() {}, disableClosingConfirmation() {},
          BackButton: {show() {}, hide() {}, onClick() {}}, onEvent(name, callback) {this.events[name]=callback}
        }}; window.testErrors = []; window.addEventListener('error', e => window.testErrors.push(e.message));
        """, media_type="application/javascript")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=app_port, log_level="error"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    driver_log = (tmp_path / "geckodriver.log").open("w")
    process = subprocess.Popen([GECKO, "--host", "127.0.0.1", "--port", str(driver_port)], stdout=driver_log, stderr=driver_log)
    client = httpx.Client(base_url=f"http://127.0.0.1:{driver_port}", timeout=45, trust_env=False)
    session_id = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if client.get("/status").status_code == 200 and server.started:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(.1)
        response = client.post("/session", json={"capabilities": {"alwaysMatch": {
            "browserName": "firefox", "moz:firefoxOptions": {"binary": FIREFOX, "args": ["-headless"]}
        }}})
        assert response.status_code == 200, response.text
        session_id = response.json()["value"]["sessionId"]

        def command(method, path, data=None):
            result = client.request(method, f"/session/{session_id}{path}", json=data)
            assert result.status_code == 200, result.text
            return result.json()["value"]

        def js(script, *args):
            return command("POST", "/execute/sync", {"script": script, "args": list(args)})

        def wait_for(script):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if js("return " + script):
                    return
                time.sleep(.1)
            pytest.fail(f"Browser condition failed: {script}; errors={js('return window.testErrors')}; UI={js('return document.body.innerText')}")

        def click(element_id):
            js("document.getElementById(arguments[0]).click()", element_id)

        def fill(element_id, value):
            js("const el=document.getElementById(arguments[0]); el.value=arguments[1]; el.dispatchEvent(new Event('input', {bubbles:true}))", element_id, value)

        def idle():
            wait_for("!document.getElementById('app').hasAttribute('aria-busy')")

        command("POST", "/window/rect", {"width": 390, "height": 844})
        command("POST", "/url", {"url": f"http://127.0.0.1:{app_port}/test-shell"})
        wait_for("!document.getElementById('catalog').hidden"); idle()
        click("admin-toggle"); idle(); click("add-category"); idle()
        fill("category-name", "Воспаление")
        js("document.getElementById('category-form').requestSubmit()")
        wait_for("!document.getElementById('materials').hidden"); idle()
        click("add-material"); idle()
        fill("material-title", "Острое воспаление")
        fill("material-text", "Учебный текст\n<script>window.stolen = true</script>")
        js("document.getElementById('material-form').requestSubmit()")
        wait_for("!document.getElementById('editor-images').hidden"); idle()
        photo_path = tmp_path / "sample.jpg"; photo_path.write_bytes(photo_bytes())
        element = command("POST", "/element", {"using": "css selector", "value": "#image-files"})
        element_id = next(iter(element.values()))
        command("POST", f"/element/{element_id}/value", {"text": str(photo_path) + "\n" + str(photo_path)})
        wait_for("document.querySelectorAll('.image-row').length === 2"); idle()
        pdf_path = tmp_path / "Лекция.pdf"; pdf_path.write_bytes(pdf_bytes(page_count=8))
        # Returning from the native file picker may overlap the access refresh.
        # The chosen files must wait for it instead of being silently discarded.
        js("run(() => new Promise(resolve => setTimeout(resolve, 400)))")
        element = command("POST", "/element", {"using": "css selector", "value": "#pdf-files"})
        element_id = next(iter(element.values()))
        command("POST", f"/element/{element_id}/value", {"text": str(pdf_path)})
        wait_for("document.querySelectorAll('.document-row').length === 1"); idle()
        fill("material-category", str(destination_id))
        js("document.getElementById('material-form').requestSubmit()")
        idle()
        moved_materials = asyncio.run(db.materials(destination_id))
        assert len(moved_materials) == 1
        first_image_id = asyncio.run(db.material(moved_materials[0]["id"]))["images"][0]["id"]
        js("window.firstPhotoFetches=0;window.firstPageFetches=0;const trackedPhoto=arguments[0];const originalFetch=window.fetch;window.fetch=(...args)=>{const path=String(args[0]);if(path===trackedPhoto)window.firstPhotoFetches++;if(/\\/api\\/documents\\/\\d+\\/pages\\/1$/.test(path))window.firstPageFetches++;return originalFetch(...args)}", f"/api/images/{first_image_id}")
        click("preview-material")
        wait_for("(document.querySelector('.photo-page canvas')?.width || 0) > 1"); idle()
        assert js("return document.querySelector('.photo-page canvas').width") == 800
        assert js("return document.querySelectorAll('.photo-page').length") == 2
        assert not js("return Boolean(window.stolen)")
        assert js("return document.getElementById('photo-stage') === null")
        js("window.dispatchEvent(new Event('blur'))")
        assert js("return !document.getElementById('privacy-shield').hidden")
        assert js("return document.getElementById('article-content').textContent") == ""
        assert js("return document.querySelectorAll('#gallery canvas').length") == 0
        click("resume"); wait_for("document.getElementById('privacy-shield').hidden")
        wait_for("(document.querySelector('.photo-page canvas')?.width || 0) > 1")
        assert js("return document.documentElement.scrollWidth <= window.innerWidth")
        Path("/tmp/rnimu-miniapp-reader.png").write_bytes(base64.b64decode(command("GET", "/screenshot")))
        click("fullscreen")
        assert js("return window.Telegram.WebApp.isFullscreen && document.getElementById('fullscreen').textContent === 'Свернуть экран'")
        click("fullscreen")
        assert not js("return window.Telegram.WebApp.isFullscreen")
        js("document.querySelectorAll('.pdf-page')[0].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[0].width > 1")
        assert js("return document.querySelectorAll('.pdf-page').length") == 8
        assert js("return document.querySelectorAll('#documents iframe, #documents embed, #documents a').length") == 0
        js("document.querySelectorAll('.pdf-page')[7].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[7].width > 1")
        wait_for("document.querySelectorAll('.pdf-page canvas')[0].width === 1")
        fetches = js("return window.firstPageFetches")
        js("document.querySelectorAll('.pdf-page')[0].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[0].width > 1")
        assert js("return window.firstPageFetches") == fetches
        js("document.querySelectorAll('.pdf-page')[7].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[7].width > 1")
        wait_for("document.querySelector('.photo-page canvas').width === 1")
        photo_fetches = js("return window.firstPhotoFetches")
        js("document.querySelector('.photo-page').scrollIntoView()")
        wait_for("document.querySelector('.photo-page canvas').width > 1")
        assert js("return window.firstPhotoFetches") == photo_fetches
        js("document.querySelectorAll('.pdf-page')[7].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[7].width > 1")
        scroll_before = js("return window.scrollY")
        js("window.dispatchEvent(new Event('blur'))")
        assert js("return document.querySelectorAll('#documents canvas').length") == 0
        click("resume"); wait_for("document.getElementById('privacy-shield').hidden")
        wait_for("document.querySelectorAll('.pdf-page canvas')[7].width > 1")
        assert abs(js("return window.scrollY") - scroll_before) < 3
        assert js("return document.documentElement.scrollWidth <= window.innerWidth")
        Path("/tmp/rnimu-miniapp-pdf-mobile.png").write_bytes(base64.b64decode(command("GET", "/screenshot")))
        command("POST", "/window/rect", {"width": 1440, "height": 1000})
        js("document.querySelectorAll('.pdf-page')[0].scrollIntoView()")
        wait_for("document.querySelectorAll('.pdf-page canvas')[0].width > 1")
        assert js("return document.getElementById('article').getBoundingClientRect().width") > 1200
        Path("/tmp/rnimu-miniapp-pdf-desktop.png").write_bytes(base64.b64decode(command("GET", "/screenshot")))
        assert js("return window.testErrors") == []

        # Subscriber has reader access, with no management controls.
        command("POST", "/url", {"url": f"http://127.0.0.1:{app_port}/test-shell?uid=2"})
        wait_for("!document.getElementById('catalog').hidden"); idle()
        assert js("return document.getElementById('admin-toggle').hidden")
        js("document.querySelector('#category-list button').click()"); wait_for("!document.getElementById('materials').hidden"); idle()
        js("document.querySelector('#material-list button').click()"); wait_for("(document.querySelector('.photo-page canvas')?.width || 0) > 1"); idle()
        assert not js("return Boolean(window.stolen)")
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute("UPDATE users SET subscription_end=NULL WHERE user_id=2")
        js("window.dispatchEvent(new Event('blur'))")
        click("resume")
        wait_for("!document.getElementById('paywall').hidden && document.getElementById('privacy-shield').hidden")
        assert js("return document.getElementById('article-content').textContent") == ""
        assert js("return document.querySelectorAll('#gallery canvas').length") == 0
        assert js("return document.querySelectorAll('#documents canvas').length") == 0
        # A non-subscriber gets no library entries.
        command("POST", "/url", {"url": f"http://127.0.0.1:{app_port}/test-shell?uid=3"})
        wait_for("!document.getElementById('paywall').hidden")
        assert js("return document.getElementById('category-list').childElementCount") == 0
    finally:
        if session_id:
            client.delete(f"/session/{session_id}")
        client.close()
        process.terminate()
        process.wait(timeout=10)
        driver_log.close()
        server.should_exit = True
        server_thread.join(timeout=10)
