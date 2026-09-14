import time
import sqlite3
from dataclasses import replace

from fastapi.testclient import TestClient

from app.web import create_app
from protected_client import Device
from test_library_api import auth, create_material, photo_bytes, project, signed_data


def open_session(raw, device, user_id, *, init_data=None):
    data = init_data or signed_data(user_id)
    response = raw.post("/api/session", json={"init_data": data},
                        headers={"DPoP": device.proof("POST", "/api/session")})
    if response.status_code == 200:
        device.token = response.json()["access_token"]
    return response, data


def test_requests_require_non_replayable_device_signature(project):
    _, db, settings = project
    with TestClient(create_app(settings, db)) as raw:
        device = Device()
        response, init_data = open_session(raw, device, 2)
        assert response.status_code == 200
        assert "access_token" in response.json()
        # The old Telegram header and a copied opaque token are insufficient.
        assert raw.get("/api/categories", headers=auth(2)).status_code == 401
        attacker = Device(); attacker.token = device.token
        assert raw.get("/api/categories", headers=attacker.headers("GET", "/api/categories")).status_code == 401

        proof = device.proof("GET", "/api/categories", token=device.token)
        headers = {"Authorization": f"DPoP {device.token}", "DPoP": proof}
        assert raw.get("/api/categories", headers=headers).status_code == 200
        assert raw.get("/api/categories", headers=headers).status_code == 401
        assert raw.get("/api/categories", headers=device.headers("POST", "/api/categories")).status_code == 401
        assert raw.get("/api/categories", headers=device.headers("GET", "/api/materials/1")).status_code == 401
        assert raw.get("/api/categories", headers=device.headers("GET", "/api/categories", iat=int(time.time()) - 61)).status_code == 401

        # Reusing Telegram launch data on another device is rejected, while the
        # original device can rotate its memory-only session token.
        assert open_session(raw, Device(), 2, init_data=init_data)[0].status_code == 401
        old_token = device.token
        assert open_session(raw, device, 2, init_data=init_data)[0].status_code == 200
        stale = Device(key=device.key); stale.token = old_token
        assert raw.get("/api/categories", headers=stale.headers("GET", "/api/categories")).status_code == 401


def test_page_tickets_are_scoped_short_lived_and_single_use(project):
    admin, db, settings = project
    _, first_material = create_material(admin)
    first_image = admin.post(f"/api/admin/materials/{first_material}/images", headers=auth(1), content=photo_bytes()).json()["id"]
    _, second_material = create_material(admin)
    second_image = admin.post(f"/api/admin/materials/{second_material}/images", headers=auth(1), content=photo_bytes()).json()["id"]

    with TestClient(create_app(settings, db)) as raw:
        device = Device(); assert open_session(raw, device, 2)[0].status_code == 200
        material_path = f"/api/materials/{first_material}"
        material = raw.get(material_path, headers=device.headers("GET", material_path)).json()
        view = material["view_token"]
        ticket_response = raw.post("/api/reader/ticket", headers=device.headers("POST", "/api/reader/ticket"),
                                   json={"view": view, "kind": "image", "resource_id": first_image, "page": 1})
        assert ticket_response.status_code == 200
        ticket = ticket_response.json()["ticket"]
        image_path = f"/api/images/{first_image}"
        headers = {**device.headers("GET", image_path), "X-Read-View": view, "X-Page-Ticket": ticket}
        assert raw.get(image_path, headers=headers).status_code == 200
        # The ticket was atomically consumed and cannot be replayed.
        assert raw.get(image_path, headers={**device.headers("GET", image_path),
                                            "X-Read-View": view, "X-Page-Ticket": ticket}).status_code == 403
        assert raw.get(image_path, headers=device.headers("GET", image_path)).status_code == 403
        assert raw.post("/api/reader/ticket", headers=device.headers("POST", "/api/reader/ticket"),
                        json={"view": view, "kind": "image", "resource_id": second_image, "page": 1}).status_code == 404


def test_distinct_page_limits_persist_across_app_restart(project):
    admin, db, settings = project
    _, material_id = create_material(admin)
    image_ids = [admin.post(f"/api/admin/materials/{material_id}/images", headers=auth(1), content=photo_bytes()).json()["id"] for _ in range(3)]
    limited = replace(settings, read_pages_per_minute=3, read_pages_per_hour=20, read_pages_per_day=50)

    def request_image(raw, device, view, image_id):
        ticket = raw.post("/api/reader/ticket", headers=device.headers("POST", "/api/reader/ticket"),
                          json={"view": view, "kind": "image", "resource_id": image_id, "page": 1})
        if ticket.status_code != 200:
            return ticket
        path = f"/api/images/{image_id}"
        return raw.get(path, headers={**device.headers("GET", path), "X-Read-View": view,
                                      "X-Page-Ticket": ticket.json()["ticket"]})

    device = Device()
    with TestClient(create_app(limited, db)) as raw:
        assert open_session(raw, device, 2)[0].status_code == 200
        path = f"/api/materials/{material_id}"
        view = raw.get(path, headers=device.headers("GET", path)).json()["view_token"]
        assert request_image(raw, device, view, image_ids[0]).status_code == 200
        assert request_image(raw, device, view, image_ids[0]).status_code == 200  # same page remains readable
        assert request_image(raw, device, view, image_ids[1]).status_code == 200
        blocked = request_image(raw, device, view, image_ids[2])
        assert blocked.status_code == 429 and blocked.headers["x-reading-limited"] == "1"

    # A new FastAPI instance sees the persisted block and audit event.
    with TestClient(create_app(limited, db)) as raw:
        blocked = raw.get("/api/categories", headers=device.headers("GET", "/api/categories"))
        assert blocked.status_code == 200  # catalog remains usable
        with sqlite3.connect(db.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM reader_blocks WHERE user_id=2").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM reader_audit WHERE user_id=2 AND event='reading_limit_60'").fetchone()[0] == 1
