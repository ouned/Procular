import subprocess

from procular import _version


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a")
    return tmp_path


def commit(repo):
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b")


def test_normalize():
    assert _version._normalize("v1.2.3-0-gabc1234") == "1.2.3"
    assert _version._normalize("v1.2.3-0-gabc1234-dirty") == "1.2.3-dirty"
    assert _version._normalize("1.2.3-4-gabc1234-dirty") == "1.2.3-4-gabc1234-dirty"
    assert _version._normalize("abc1234") == "0.0.0-gabc1234"
    assert _version._normalize("1234567-dirty") == "0.0.0-g1234567-dirty"
    assert _version._normalize("") is None


def test_git_tag_exact_and_past(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "tag", "v1.2.3")
    assert _version._from_git(str(repo)) == "1.2.3"
    commit(repo)
    assert _version._from_git(str(repo)).startswith("1.2.3-1-g")


def test_git_ignores_unrelated_tags(tmp_path):
    repo = make_repo(tmp_path)
    git(repo, "tag", "v1.0.0")
    commit(repo)
    git(repo, "tag", "nightly")
    assert _version._from_git(str(repo)).startswith("1.0.0-1-g")


def test_git_without_tags(tmp_path):
    repo = make_repo(tmp_path)
    for _ in range(5):  # hashes starting with a digit must not look like tags
        assert _version._from_git(str(repo)).startswith("0.0.0-g")
        commit(repo)


def test_not_a_checkout(tmp_path):
    assert _version._from_git(str(tmp_path)) is None


def test_file(tmp_path):
    path = tmp_path / "_version.txt"
    path.write_text("2.0.0\n")
    assert _version._from_file(str(path)) == "2.0.0"
    assert _version._from_file(str(tmp_path / "missing")) is None
