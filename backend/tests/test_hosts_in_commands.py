"""Which names of a command are taken for hosts. Reported with a picture of the permission box: a command that
ran a JavaScript program listed globalthis.http, h.clientrequest, object.keys, h.buffer.prototype.bytelength,
h.buffer.prototype.write, h.buffer and process.exitcode as hosts, each "not resolved yet", and asked for them
although the chat allowed the tool.

A name was a host when its last part was two letters or more and no file ending of a short list. That is every
property of every object in a program, and every file name with another ending. Each one that is not in the
target's scope makes the call ask, whatever the chat allows."""
import pytest

from hunterai.tools import resolve


def hosts(command):
    return {h.host for h in resolve.extract_hosts(command)}


REPORTED = """node -e '
const h = globalThis.http || require("http");
const seen = [];
const send = h.ClientRequest.prototype.end;
for (const k of Object.keys(h)) seen.push(k);
const n = h.Buffer.prototype.byteLength, w = h.Buffer.prototype.write, B = h.Buffer;
if (!seen.length) process.exitCode = 1;
console.log(seen.join(","));
'"""


def test_the_command_that_was_reported_names_no_host():
    assert hosts(REPORTED) == set()


@pytest.mark.parametrize("command, found", [
    ("curl evil.com", {"evil.com"}),
    ("nmap -sV -p- scanme.nmap.org", {"scanme.nmap.org"}),
    ("ssh -p 2222 bastion.corp.example.net", {"bastion.corp.example.net"}),
    ("dig +short a.b.co.uk && host mail.acme.io", {"a.b.co.uk", "mail.acme.io"}),
    ("httpx -u target.htb -silent", {"target.htb"}),
    ("wget intranet.local/x && nc db.internal 5432 && ping -c1 nas.lan", {"intranet.local", "db.internal", "nas.lan"}),
    ("curl -H 'Host: evil.com' 10.0.0.1", {"evil.com", "10.0.0.1"}),
    ('echo "evil.com" | httpx', {"evil.com"}),
    ('for h in a.com b.org; do curl "$h"; done', {"a.com", "b.org"}),
    ('bash -c "curl evil.com | grep -c x"', {"evil.com"}),
    ("FFUF_HOST=shop.acme.dev ffuf -u https://shop.acme.dev/FUZZ -w words.txt", {"shop.acme.dev"}),
    ("nmap -sV 10.0.0.5 && curl https://a.acme.test:8443/x -o out.txt; cat notes.md", {"10.0.0.5", "a.acme.test"}),
    ("nc dc01.megacorp.lcl:445", {"dc01.megacorp.lcl"}),                     # an ending nobody registered, with a port: a host
])
def test_a_host_named_in_a_command_is_found(command, found):
    assert hosts(command) == found


@pytest.mark.parametrize("command", [
    "convert shot.png -resize 50% small.jpg && openssl x509 -in cert.pem -noout -text",
    "java -jar app.jar --config app.toml && sqlite3 data.sqlite .tables",
    "systemctl status nginx.service && journalctl -u hunterai.service -n 20",
    "tar czf backup.tgz src/ && sha256sum backup.tgz > backup.sha256",
    "python3 manage.py migrate && pytest tests/test_api.py::test_login -q",
    "cat package.json | jq '.dependencies' && ls src/index.tsx",
])
def test_a_file_name_is_no_host(command):
    assert hosts(command) == set()


@pytest.mark.parametrize("command", [
    'python3 -c "import os, sys; print(os.name, sys.platform)"',
    "python3 -c 'import subprocess; r = subprocess.run([\"id\"]); print(r.returncode)'",
    "node -p 'process.version + os.name'",
    "node -e 'const xs = [1, 2].map(x => x * 2); console.log(xs.length, Date.now())'",
    "python3 - <<'EOF'\nimport re, datetime\nclass A:\n    def __init__(self):\n        self.name = 'x'\n        self.id = 1\na = A()\nprint(a.name, a.id, re.search('x', a.name), datetime.datetime.now())\nEOF",
    "cat > app.js <<EOF\nconst user = { name: 'a', id: 1 };\nmodule.exports = { name: user.name, id: user.id, host: user.host };\nEOF\nnode app.js",
    "node - <<EOF\nconst s = { live: null, chat: { id: 1 } };\nconsole.log(s.live, s.chat.id, e.target);\nEOF",
    "perl -e 'print $ENV{HOME}; my %h = (name => 1); print $h{name}, obj.name'",
    "python3 -c 'import os; h = os.uname(); print(f\"{h.sysname} on {h.nodename}: {os.name}\")'",
    "node -e 'const u = { name: 1, id: 2 }; console.log(`${u.name} has ${u.id}`)'",
    "python3 -u - <<'EOF'\nimport os\nprint(os.name)\nEOF",
    "node --no-warnings -e 'console.log(os.name)'",
    "perl -MData::Dumper -lne 'print $h.name if $x.id'",
    "deno eval 'console.log(Deno.build.os, self.name)'",
])
def test_the_names_of_a_program_are_no_hosts(command):
    assert hosts(command) == set()


@pytest.mark.parametrize("command, found", [
    ("python3 -c \"import socket; s = socket.create_connection(('evil.com', 80)); print(s.recv(100))\"", {"evil.com"}),
    ("node -e 'require(\"http\").get({ host: \"evil.com\", path: \"/\" }, r => console.log(r.statusCode))'", {"evil.com"}),
    ("python3 - <<'EOF'\nimport requests\nHOST = \"api.acme.io\"\nprint(requests.get('https://shop.acme.io/x').status_code, HOST, self.name)\nEOF",
     {"api.acme.io", "shop.acme.io"}),
    ("cat > poc.py <<EOF\nimport socket\ns = socket.socket()\ns.connect((\"10.0.0.7\", 4444))\nt = 'cdn.evil.org'\nEOF", {"10.0.0.7", "cdn.evil.org"}),
    ("cat > hosts.txt <<EOF\nevil.com\nother.org\nEOF\nnuclei -l hosts.txt", {"evil.com", "other.org"}),      # a list, not a program
    ("cat <<EOF | httpx -silent\nshop.acme.dev\nadmin.acme.dev\nEOF", {"shop.acme.dev", "admin.acme.dev"}),
    ("python3 -c 'import requests, json; print(requests.post(u, data=json.dumps(d), headers={\"Origin\": \"evil.com\"}).text)'", {"evil.com"}),
    ("python3 - <<'EOF'\nimport requests\nr = requests.post(u, data='{\"callback\": \"cb.evil.org\"}')\nprint(r.text)\nEOF", {"cb.evil.org"}),
    ("curl {a.acme.io,b.acme.io}/robots.txt", {"a.acme.io", "b.acme.io"}),                                      # braces of the shell
    # A script has switches and input of its own: what is quoted behind its -c, and what it is fed, is not program text.
    ('python3 exploit.py -c "evil.com" -p "8443"', {"evil.com"}),
    ("python3 dirsearch.py -e 'php,html' -u 'shop.acme.dev'", {"shop.acme.dev"}),
    ("python3 resolve.py <<EOF\nevil.com\nother.org\nEOF", {"evil.com", "other.org"}),
    ("sed -i 's/old.acme.io/new.acme.io/' hosts.txt", {"old.acme.io", "new.acme.io"}),                           # a substitution is no program
])
def test_a_host_a_program_names_in_quotes_is_found(command, found):
    assert hosts(command) == found


def test_quotes_inside_a_quoted_program_are_read_as_the_shell_hands_them_on():
    """Inside double quotes of the shell a program's own double quotes are written with a backslash."""
    assert hosts('python3 -c "import socket; socket.create_connection((\\"evil.com\\", 80)); print(os.name)"') == {"evil.com"}
    assert hosts('node -e "console.log(\\"x\\", os.name, user.id)"') == set()


def test_an_apostrophe_in_a_comment_does_not_turn_the_rest_inside_out():
    program = "python3 - <<'EOF'\n# don't resolve anything here\nimport os\nprint(os.name, self.id)\nHOST = 'cb.evil.org'\nEOF"
    assert hosts(program) == {"cb.evil.org"}


def test_a_url_in_front_of_a_program_does_not_move_where_the_program_stands():
    assert hosts("curl -s https://shop.acme.io/a/long/path?with=a&query=1; python3 -c 'print(os.name, self.id)'") == {"shop.acme.io"}
    assert hosts("curl -s https://shop.acme.io/x; python3 -c 'print(\"cb.evil.org\", os.name)'") == {"shop.acme.io", "cb.evil.org"}


def test_what_is_called_is_no_host():
    """A name with a bracket right behind it is a call, in a program and in a shell alike."""
    assert hosts("awk '{ print length($0), tolower($1) }' names.csv") == set()
    assert hosts("echo $(date +%s) && curl api.acme.io") == {"api.acme.io"}
    assert hosts('grep -rn "re.search(pattern" src/') == set()                  # one call does not make a text a program


def test_a_quoted_text_is_a_program_by_what_it_does():
    """No interpreter in front of it and no source file behind it: it calls two things, or has an arrow function."""
    assert hosts("echo 'x = load(a); save(x); print(user.name)' > run.txt") == set()
    assert hosts("echo 'xs.filter(x => x.id > 1)' | node") == set()
    assert hosts("echo 'ask user.name for the host' > note.txt") == {"user.name"}        # no program: a name that ends like a domain counts


def test_the_list_of_endings_is_the_one_the_internet_has():
    assert {"com", "org", "io", "dev", "app", "uk", "ph"} <= resolve.TLDS and len(resolve.TLDS) > 1000
    assert not {"png", "pdf", "keys", "http", "service", "sqlite", "prototype"} & resolve.TLDS
    assert {"local", "internal", "lan", "test", "htb", "onion"} <= resolve.PRIVATE_ENDINGS
