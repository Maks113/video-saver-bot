"""Read resolved Compose JSON on stdin; report policy names, never env values."""
import json
import sys


def check(config):
    services = config["services"]
    api, bot, worker, media = (services[name] for name in ("telegram-bot-api", "bot", "downloader", "metube"))
    assert [arg for arg in api["command"] if arg.startswith("--http-stat-ip-address=")] == [
        "--http-stat-ip-address=127.0.0.1"], "Statistics must bind only to loopback"
    assert not api.get("ports"), "Bot API must have no published ports"
    for other in (worker, media):
        assert not other.get("network_mode"), "Untrusted service must use its own network"
        assert set(other["networks"]).isdisjoint(api["networks"]), "Untrusted service shares API network"
    assert not worker.get("pid"), "Downloader must have a private PID namespace"
    assert worker["user"].split(":")[0] not in {bot["user"].split(":")[0], "0", "root"}, "Downloader UID is not isolated"
    assert not {"TG_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"} & set(worker.get("environment", {})), "Telegram secrets present in downloader"
    mounts = lambda service: {mount["target"]: mount for mount in service.get("volumes", [])}
    w, b, a = mounts(worker), mounts(bot), mounts(api)
    assert not {"/uploads", "/state", "/var/run/docker.sock"} & w.keys(), "Downloader mounts private bot data"
    private = {b["/uploads"]["source"], b["/state"]["source"], a["/var/lib/telegram-bot-api"]["source"]}
    assert not private & {mount["source"] for mount in w.values()}, "Private data exposed under another target"
    assert w["/requests"].get("read_only") and b["/results"].get("read_only"), "Queue directions must be read-only"
    assert b["/downloads"].get("read_only") and a["/uploads"].get("read_only"), "Video mounts must be read-only"
    assert b["/uploads"]["source"] == a["/uploads"]["source"], "Trusted upload paths do not match"
    assert "/downloads" not in a, "API must never read downloader-controlled files"
    assert worker.get("read_only") and "ALL" in worker.get("cap_drop", []), "Downloader rootfs/capabilities are not restricted"


if __name__ == "__main__":
    check(json.load(sys.stdin))
    print("Compose isolation verified; no secrets printed.")
