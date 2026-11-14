import os
import stat

from caplog.checkpoint import Writer, generate_key, key_id, load_private_key, load_public_key, message


def test_sign_and_detect_tamper(tmp_path):
    private_pem, public_pem = generate_key()
    key = tmp_path / "k.pem"
    pub = tmp_path / "k.pem.pub"
    key.write_bytes(private_pem)
    pub.write_bytes(public_pem)
    private = load_private_key(key)
    public = load_public_key(pub)
    writer = Writer(str(tmp_path / "c.jsonl"), private)
    item = writer.write(3, "ab" * 32)
    assert item is not None
    assert item.verify_sig(public)
    assert item.key_id == key_id(public)
    item.hash = "cd" * 32
    assert not item.verify_sig(public)
    assert message(3, "ab" * 32, item.time).startswith(b"caplog-checkpoint-v1\n")


def test_keygen_refuses_overwrite(tmp_path, capsys):
    from caplog.cli import main

    target = tmp_path / "caplog_signing.key"
    assert main(["keygen", "-out", str(target)]) == 0
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600
    assert (tmp_path / "caplog_signing.key.pub").exists()
    again = main(["keygen", "-out", str(target)])
    assert again == 1
    assert "refusing to overwrite" in capsys.readouterr().err
