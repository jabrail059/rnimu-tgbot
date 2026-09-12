import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_library_api import project
from app.yookassa import YooKassaPayment


def load_bot(monkeypatch, project):
    _, db, settings = project
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    module = importlib.import_module("main")
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "database", db)
    return module


def test_menu_has_subscription_author_and_payment_buttons(project, monkeypatch):
    module = load_bot(monkeypatch, project)
    message = SimpleNamespace(from_user=SimpleNamespace(id=2, username="reader"), chat=SimpleNamespace(type="private"), answer=AsyncMock())
    asyncio.run(module.start(message))
    text = message.answer.call_args.args[0]
    assert "Подписка активна" in text and "Действует до:" in text and "@eucliris" in text
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(button.callback_data == "buy" for button in buttons)
    assert any(button.callback_data == "course_info" for button in buttons)
    assert any(button.web_app for button in buttons)
    asyncio.run(module.show_user_id(message))
    assert "Telegram ID: 2" in message.answer.call_args.args[0]


def test_checkout_keeps_yookassa_flow_and_updated_label(project, monkeypatch):
    _, db, settings = project
    module = load_bot(monkeypatch, project)
    payment = YooKassaPayment("provider-test", "pending", "https://yookassa.ru/test-checkout", settings.subscription_price, "RUB", {})
    create = AsyncMock(return_value=payment)
    monkeypatch.setattr(module.yookassa, "create_payment", create)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=3, username="reader"), message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())
    asyncio.run(module.buy(callback))
    assert create.await_count == 1
    keyboard = callback.message.answer.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате"
    assert keyboard.inline_keyboard[0][0].url == payment.confirmation_url
    assert asyncio.run(db.subscription_end(3)) is None
