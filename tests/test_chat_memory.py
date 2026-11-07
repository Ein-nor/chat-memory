"""Tests for chat_memory.py. Run with: python3 -m unittest discover tests"""

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "skills", "chat-memory", "chat_memory.py")

_spec = importlib.util.spec_from_file_location("chat_memory", SCRIPT)
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)


class MaskSecretsTest(unittest.TestCase):
    def assertMasked(self, text, secret):
        masked = cm.mask_secrets(text)
        self.assertNotIn(secret, masked)
        self.assertIn(cm.MASK, masked)

    def test_api_keys_and_tokens(self):
        cases = [
            "sk-ant-api03-" + "a" * 40,
            "sk-proj-" + "b" * 40,
            "ghp_" + "c" * 36,
            "github_pat_" + "d" * 40,
            "glpat-" + "e" * 20,
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "xoxb-1234567890-abcdefghij",
            "AIza" + "f" * 35,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ]
        for secret in cases:
            with self.subTest(secret=secret[:12]):
                self.assertMasked("value " + secret + " end", secret)

    def test_key_value_pairs_keep_the_key(self):
        for line, secret in [
            ("password: hunter2geheim", "hunter2geheim"),
            ("DB_PASSWORD=s3cr3t!", "s3cr3t!"),
            ('export API_KEY="abc123def456"', "abc123def456"),
            ("client_secret = 'xyz987'", "xyz987"),
        ]:
            with self.subTest(line=line):
                masked = cm.mask_secrets(line)
                self.assertNotIn(secret, masked)
                self.assertEqual(masked.split("=")[0].split(":")[0], line.split("=")[0].split(":")[0])

    def test_bearer_url_credentials_and_private_keys(self):
        self.assertMasked("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnopqrstuvwxyz0123")
        self.assertMasked("git clone https://me:hunter2@github.com/x/y", "hunter2")
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2E\n-----END OPENSSH PRIVATE KEY-----"
        self.assertMasked("here:\n" + key, "AAAAB3NzaC1yc2E")

    def test_german_keys_auth_schemes_and_cut_keys(self):
        # German key names on purpose: users write "Passwort" and "Kennwort".
        for text, secret in [
            ("Passwort: hunter2geheim", "hunter2geheim"),
            ("Root-Kennwort=geheim123", "geheim123"),
            ("Authorization: token 0123456789abcdef0123456789abcdef01234567",
             "0123456789abcdef0123456789abcdef01234567"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Njc4", "dXNlcjpwYXNzd29yZDEyMzQ1Njc4"),
            ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\n(cut off)", "MIIEvQIBADANBgkqhkiG9w0B"),
        ]:
            with self.subTest(text=text[:20]):
                self.assertMasked(text, secret)

    def test_normal_text_is_untouched(self):
        for text in [
            # German prose on purpose: must not trip the German key names or "token <word>".
            "Wir haben über Passwörter und Tokens gesprochen.",
            "The password policy needs 12 characters.",
            "Run git clone https://github.com/x/y",
            "sk-learn is a Python library",
            "Das Token wird über die Zugriffsberechtigungen geprüft.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(cm.mask_secrets(text), text)


def entry(kind, uuid, text=None, content=None, **extra):
    data = {"type": kind, "uuid": uuid, "sessionId": extra.pop("session", "s1"),
            "timestamp": extra.pop("ts", "2026-01-01T10:00:00Z"), "cwd": "/home/me/projects/demo"}
    data["message"] = {"role": kind, "content": content if content is not None else text}
    data.update(extra)
    return data


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "claude", "projects", "-home-me-projects-demo")
        os.makedirs(self.projects)
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"),
                        CHAT_MEMORY_DATA_DIR=os.path.join(self.tmp, "data"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, entries):
        with open(os.path.join(self.projects, name), "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, SCRIPT] + list(args), env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def indexed_texts(self):
        db = os.path.join(self.tmp, "data", "index.db")
        return [r[0] for r in sqlite3.connect(db).execute("SELECT text FROM messages")]

    def test_only_conversation_text_is_indexed(self):
        self.write("s1.jsonl", [
            {"type": "ai-title", "aiTitle": "Demo-Session", "sessionId": "s1"},
            entry("user", "u1", "How do I build a Packer image?"),
            entry("assistant", "a1", content=[
                {"type": "thinking", "thinking": "SECRET THOUGHTS"},
                {"type": "text", "text": "With an HCL template."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat .env"}},
            ]),
            entry("user", "u2", content=[{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "TOOL OUTPUT API_KEY=abc"}]),
            entry("user", "u3", "<system-reminder>internal note</system-reminder>"),
            entry("user", "u4", "<task-notification>done</task-notification>"),
            entry("user", "u5", "side chain text", isSidechain=True),
            entry("user", "u6", "My token: ghp_" + "x" * 36),
        ])
        self.run_cli("stats")
        texts = " ".join(self.indexed_texts())
        self.assertIn("Packer image", texts)
        self.assertIn("HCL template", texts)
        for leaked in ["SECRET THOUGHTS", "TOOL OUTPUT", "cat .env", "internal note",
                       "task-notification", "side chain", "ghp_xxx"]:
            self.assertNotIn(leaked, texts)
        self.assertIn("Demo-Session", self.run_cli("sessions"))

    def test_search_show_and_accents(self):
        self.write("s1.jsonl", [
            entry("user", "u1", "Please update the résumé and grow the partition."),
            entry("assistant", "a1", "The partition is now 40 GB.", ts="2026-01-01T10:01:00Z"),
        ])
        out = self.run_cli("search", "partition", "resume")  # no accent needed
        self.assertIn("»résumé«", out)
        self.assertIn("No matches.", self.run_cli("search", "kubernetes"))
        shown = self.run_cli("show", "s1", "--match", "40 GB", "--context", "0")
        body = shown.split("\n\n", 1)[1]  # skip the header, whose title is the first message
        self.assertIn("40 GB", body)
        self.assertNotIn("résumé", body)

    def test_rewound_copy_is_counted_once(self):
        first = [entry("user", "u1", "First question about Docker"),
                 entry("assistant", "a1", "Answer about Docker")]
        self.write("s1.jsonl", first)
        os.utime(os.path.join(self.projects, "s1.jsonl"), (1, 1))
        copy = [dict(e, sessionId="s2") for e in first] + [
            entry("user", "u9", "New question after the rewind", session="s2")]
        self.write("s2.jsonl", copy)
        self.run_cli("stats")
        texts = self.indexed_texts()
        self.assertEqual(sum("Docker" in t for t in texts), 2)
        self.assertEqual(len(texts), 3)

    def test_copy_takes_over_when_original_is_deleted(self):
        first = [entry("user", "u1", "Question about Traefik"), entry("assistant", "a1", "Answer about Traefik")]
        self.write("s1.jsonl", first)
        os.utime(os.path.join(self.projects, "s1.jsonl"), (1, 1))
        self.write("s2.jsonl", [dict(e, sessionId="s2") for e in first] +
                   [entry("user", "u9", "Going on after resuming", session="s2")])
        self.run_cli("stats")
        os.remove(os.path.join(self.projects, "s1.jsonl"))  # Claude Code cleans up old transcripts
        self.run_cli("stats")
        self.assertEqual(sum("Traefik" in t for t in self.indexed_texts()), 2)
        self.assertIn("s2", self.run_cli("search", "traefik"))

    def test_forgotten_text_does_not_come_back_through_a_copy(self):
        first = [entry("user", "u1", "Secret project Omega")]
        self.write("s1.jsonl", first)
        os.utime(os.path.join(self.projects, "s1.jsonl"), (1, 1))
        self.write("s2.jsonl", [dict(e, sessionId="s2") for e in first] +
                   [entry("user", "u9", "Something else", session="s2")])
        self.run_cli("stats")
        self.run_cli("forget", "s1")
        self.write("s1.jsonl", first + [entry("user", "u2", "Addendum", ts="2026-01-02T10:00:00Z")])
        self.run_cli("stats")
        self.write("s2.jsonl", [dict(e, sessionId="s2") for e in first] +
                   [entry("user", "u9", "Something else", session="s2"),
                    entry("user", "u10", "Even more", session="s2")])
        self.assertIn("No matches.", self.run_cli("search", "omega"))

    def test_since_must_be_a_date(self):
        result = subprocess.run([sys.executable, SCRIPT, "search", "x", "--since", "last week"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_updates_and_forget(self):
        self.write("s1.jsonl", [entry("user", "u1", "Alpha topic here")])
        self.assertIn("Alpha", self.run_cli("search", "alpha"))
        self.write("s1.jsonl", [entry("user", "u1", "Alpha topic here"),
                                entry("assistant", "a1", "Beta answer here")])
        self.assertIn("Beta", self.run_cli("search", "beta"))
        self.run_cli("forget", "s1")
        self.assertIn("No matches.", self.run_cli("search", "alpha"))
        self.write("s1.jsonl", [entry("user", "u1", "Alpha topic here"),
                                entry("user", "u2", "Gamma later")])
        self.assertIn("No matches.", self.run_cli("search", "gamma"))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_index_is_private(self):
        self.write("s1.jsonl", [entry("user", "u1", "hello world text")])
        self.run_cli("stats")
        db = os.path.join(self.tmp, "data", "index.db")
        self.assertEqual(os.stat(db).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(db)).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
