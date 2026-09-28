"""`kestrel-pair` / `python -m kestrel.pair` - the CLI that prints a
one-time pairing link so the token reaches a device without ever being baked
into the web bundle (docs/design.md §11a) or riding in a cookie.

The one thing worth pinning down at the unit level: the token travels in the
URL *fragment* (`#pair=<token>`), never a query parameter - a fragment is
never sent in the HTTP request, so it never reaches an access log. Everything
else (finding/creating the on-disk token, the CLI's argument handling) is
exercised through `main()` against a temp `Config`.
"""

import pytest

from kestrel.pair import main, pairing_url

TOKEN = "abc123-not-a-real-token"
BASE = "https://kestrel-pi.tailnet-1234.ts.net"


def test_pairing_url_puts_the_token_in_the_fragment_not_the_query():
    url = pairing_url(BASE, TOKEN)
    assert url == f"{BASE}/#pair={TOKEN}"
    # Not anywhere a query-string parser would find it, and not logged by
    # anything that only ever sees the request line/path.
    assert "?" not in url or url.index("#") < url.index("?")
    path_and_query = url.split("#", 1)[0]
    assert TOKEN not in path_and_query


def test_pairing_url_strips_a_trailing_slash_on_the_base():
    assert pairing_url(f"{BASE}/", TOKEN) == f"{BASE}/#pair={TOKEN}"


def test_main_prints_a_fragment_link_and_creates_the_token_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KESTREL_DATA", str(tmp_path))
    main(["--base", BASE, "--no-qr"])

    out = capsys.readouterr().out
    assert "#pair=" in out
    # The printed link's token matches whatever load_or_create_token wrote to
    # disk - not a value invented separately by the CLI.
    token = (tmp_path / "token").read_text().strip()
    assert f"{BASE}/#pair={token}" in out
    # Never printed as a bare query-string form anywhere in the output.
    assert f"?token={token}" not in out


def test_main_reuses_an_existing_token_rather_than_minting_a_new_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KESTREL_DATA", str(tmp_path))
    (tmp_path).mkdir(exist_ok=True)
    token_path = tmp_path / "token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("already-on-disk")

    main(["--base", BASE, "--no-qr"])

    out = capsys.readouterr().out
    assert f"{BASE}/#pair=already-on-disk" in out
    assert token_path.read_text().strip() == "already-on-disk"


def test_main_requires_a_base_url(monkeypatch, capsys):
    monkeypatch.delenv("KESTREL_PUBLIC_URL", raising=False)
    with pytest.raises(SystemExit):
        main([])
    assert "base URL" in capsys.readouterr().err
