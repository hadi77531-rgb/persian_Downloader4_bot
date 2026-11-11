import time

from bot.handlers.common import PendingStore


def test_pending_store_roundtrip_and_pop():
    store = PendingStore()
    store.add("a", ("v", 1))
    assert store.get("a") == ("v", 1)
    assert store.pop("a") == ("v", 1)
    assert store.get("a") is None
    assert len(store) == 0


def test_pending_store_expires_by_ttl():
    store = PendingStore(ttl=0.05)
    store.add("a", "value")
    assert store.get("a") == "value"
    time.sleep(0.06)
    assert store.get("a") is None


def test_pending_store_is_bounded():
    store = PendingStore(capacity=3)
    for i in range(10):
        store.add(f"j{i}", i)
    assert len(store) == 3
    assert store.get("j0") is None  # oldest evicted
    assert store.get("j9") == 9
