"""Test suite. Run with: python -m unittest discover tests"""

import os
import re
import unittest

from frobai.ai.llm import LLMError
from frobai.ai.parser import AIParser, is_parser_failure
from frobai.session import Session
from frobai.zmachine import HALTED, WAIT_LINE, Host, ZMachine
from frobai.zmachine import quetzal

HERE = os.path.dirname(__file__)
STORIES = os.path.join(HERE, "stories")
ZORK1 = os.environ.get("FROBAI_ZORK1", os.path.expanduser("~/github/zork1/COMPILED/zork1.z3"))


def load(name):
    with open(os.path.join(STORIES, name), "rb") as f:
        return f.read()


def run_script(machine, commands):
    out = [machine.screen.take_output() if machine.run() else ""]
    commands = list(commands)
    while machine.state == WAIT_LINE and commands:
        machine.provide_line(commands.pop(0))
        machine.run()
        out.append(machine.screen.take_output())
    return "".join(out)


class ComplianceTests(unittest.TestCase):
    def test_czech(self):
        for v in (3, 4, 5, 8):
            with self.subTest(version=v):
                out = run_script(ZMachine(load("czech.z%d" % v)), [])
                m = re.search(r"Failed: (\d+)", out)
                self.assertIsNotNone(m, out[-500:])
                self.assertEqual(m.group(1), "0", out[-2000:])

    def test_praxix(self):
        out = run_script(ZMachine(load("praxix.z5"), seed=1), ["all"])
        self.assertIn("All tests passed", out, out[-2000:])


class MemoryHost(Host):
    def __init__(self):
        self.data = None

    def save_game(self, data):
        self.data = data
        return True

    def restore_game(self):
        return self.data


@unittest.skipUnless(os.path.exists(ZORK1), "Zork I story file not available")
class ZorkTests(unittest.TestCase):
    def setUp(self):
        with open(ZORK1, "rb") as f:
            self.story = f.read()

    def test_snapshot_rollback(self):
        m = ZMachine(self.story, seed=1)
        run_script(m, [])
        snap = m.snapshot()
        m.provide_line("open mailbox")
        m.run()
        self.assertIn("leaflet", m.screen.take_output())
        m.restore_snapshot(snap)
        m.provide_line("open mailbox")
        m.run()
        self.assertIn("reveals a leaflet", m.screen.take_output())

    def test_quetzal_roundtrip(self):
        host = MemoryHost()
        m = ZMachine(self.story, host=host, seed=1)
        run_script(m, ["open mailbox", "take leaflet", "save", "drop leaflet", "restore"])
        out = run_script(m, ["inventory"])
        self.assertIn("leaflet", out)
        self.assertTrue(host.data.startswith(b"FORM"))
        # The file must also restore into a fresh machine.
        m2 = ZMachine(self.story, host=host)
        run_script(m2, ["restore"])
        self.assertIn("leaflet", run_script(m2, ["inventory"]))
        self.assertRaises(quetzal.QuetzalError, quetzal.restore, m2, b"FORM\0\0\0\4XXXX")

    def test_lookup_words(self):
        m = ZMachine(self.story)
        words = dict(m.lookup_words("take the lantern, xyzzyplugh"))
        self.assertTrue(words["lantern"])   # matched on the first six letters
        self.assertEqual(words["xyzzyplugh"], 0)


class FakeClient:
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def chat_json(self, system, user, schema):
        self.prompts.append(user)
        if not self.answers:
            raise LLMError("no more answers")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, dict) else {"commands": answer, "answer": ""}


class RecordingUI:
    def __init__(self, lines):
        self.lines = list(lines)
        self.text = []
        self.notices = []
        self.echoes = []
        self.narrations = []

    def command_echo(self, cmd):
        self.echoes.append(cmd)

    def narrate(self, text):
        self.narrations.append(text)

    def show(self, text):
        self.text.append(text)

    def notice(self, text):
        self.notices.append(text)

    def read_line(self):
        return self.lines.pop(0) if self.lines else None

    def read_key(self):
        return None

    def show_upper(self, machine):
        pass

    def thinking(self, on):
        pass

    def flush(self):
        pass

    def reprompt(self):
        pass

    debug = notice


class ParserTests(unittest.TestCase):
    def test_failure_detection(self):
        for text in ('I don\'t know the word "paper".\n\n>', "That's not a verb I recognise.\n>",
                     "You can't see any such thing.", "I beg your pardon?\n>"):
            self.assertTrue(is_parser_failure(text), text)
        for text in ("Opening the small mailbox reveals a leaflet.\n\n>", "Taken.\n>",
                     "What do you want to take?\n>"):
            self.assertFalse(is_parser_failure(text), text)


@unittest.skipUnless(os.path.exists(ZORK1), "Zork I story file not available")
class SessionTests(unittest.TestCase):
    def play(self, lines, answers):
        with open(ZORK1, "rb") as f:
            m = ZMachine(f.read(), seed=1)
        client = FakeClient(answers)
        ui = RecordingUI(lines)
        ai = AIParser(m, client)
        Session(m, ui, ai).play()
        return m, ui, client, "".join(ui.text)

    def test_known_command_skips_ai(self):
        _, ui, client, out = self.play(["open mailbox"], [])
        self.assertEqual(client.prompts, [])
        self.assertIn("reveals a leaflet", out)

    def test_unknown_words_rewritten(self):
        _, ui, client, out = self.play(["grab the paper out of the box"], [["open mailbox", "take leaflet"]])
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("paper", client.prompts[0])
        self.assertEqual(ui.echoes, ["open mailbox", "take leaflet"])
        self.assertIn("Taken", out)

    def test_failed_rewrite_is_rolled_back_and_retried(self):
        m, ui, client, out = self.play(["open the letterbox"], [["with mailbox"], ["open mailbox"]])
        self.assertEqual(len(client.prompts), 2)
        self.assertIn(">with mailbox", client.prompts[1])     # the model is told what failed
        self.assertEqual(ui.echoes, ["open mailbox"])
        self.assertNotIn("no verb", out)
        self.assertIn("reveals a leaflet", out)

    def test_rewrite_with_unknown_words_is_retried_in_call(self):
        m, ui, client, out = self.play(["open the gizmo"], [["open gizmo"], ["open mailbox"]])
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("NOT in the game vocabulary: gizmo", client.prompts[1])
        self.assertIn("reveals a leaflet", out)

    def test_parser_rejection_triggers_ai(self):
        # Every word is known, but the sentence isn't one Zork accepts.
        _, ui, client, out = self.play(["the mailbox"], [["open mailbox"]])
        self.assertEqual(len(client.prompts), 1)
        self.assertNotIn("There was no verb", out)
        self.assertIn("reveals a leaflet", out)

    def test_ai_gives_up_shows_original_response(self):
        m, ui, client, out = self.play(["flibber the gnorp"], [[]])
        self.assertIn("don't know the word", out)
        self.assertEqual(m.state, WAIT_LINE) if m.state != HALTED else None

    def test_question_answered_without_taking_a_turn(self):
        m, ui, client, out = self.play(
            ["open mailbox", "where am i?", "and what's in the mailbox"],
            [{"commands": [], "answer": "You're west of a white house."},
             {"commands": [], "answer": "A leaflet, last time you looked."}])
        self.assertEqual(ui.narrations, ["You're west of a white house.", "A leaflet, last time you looked."])
        self.assertEqual(m.word(m.globals + 4), 1)                # moves: only "open mailbox" counted
        self.assertIn("Status line: West of House", client.prompts[0])
        self.assertIn("question or remark", client.prompts[0])
        self.assertIn("[frobai] You're west of a white house.", client.prompts[1])  # remembers its answers
        self.assertNotIn("don't know the word", out)

    def test_question_prefix_and_answer_with_command(self):
        m, ui, client, out = self.play(["?what am I carrying"],
                                       [{"commands": ["inventory"], "answer": "Let's check your pockets."}])
        self.assertEqual(ui.narrations, ["Let's check your pockets."])
        self.assertEqual(ui.echoes, ["inventory"])
        self.assertIn('Player typed: "what am I carrying"', client.prompts[0])
        self.assertIn("empty-handed", out)

    def test_unparseable_input_gets_explanation(self):
        _, ui, client, out = self.play(["flibber the gnorp"],
                                       [{"commands": [], "answer": "The game has no idea what a gnorp is."}])
        self.assertEqual(ui.narrations, ["The game has no idea what a gnorp is."])
        self.assertNotIn("don't know the word", out)

    def test_conversational_detection(self):
        from frobai.ai.parser import looks_conversational
        for s in ("where am I", "What am I doing?", "any ideas", "is there a way out", "take lamp?"):
            self.assertTrue(looks_conversational(s), s)
        for s in ("take lamp", "open mailbox", "ask troll about sword", "help", "look"):
            self.assertFalse(looks_conversational(s), s)

    def test_llm_unavailable_falls_back(self):
        _, ui, client, out = self.play(["flibber"], [])
        self.assertTrue(any("unavailable" in n for n in ui.notices))
        self.assertIn("don't know the word", out)


if __name__ == "__main__":
    unittest.main()
